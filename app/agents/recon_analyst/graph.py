"""Bounded LangGraph pipeline for the reconciliation analyst.

The optional ``agents`` dependency group installs ``langgraph==1.2.8``. The
lockfile already carries the compatibility-spike ``langgraph-sdk`` dependency,
which keeps ``websockets`` constrained below 16 via the spike resolution.

The guard grounds cited row IDs and vetted mutation payload fields only:
transaction, statement-line, category, and account IDs; merchants; and
subscription decisions. Payload free-text fields are limited to ``notes`` and
``rationale``. Narrative prose in ``period_summary``, finding ``detail``, and
action ``rationale`` is not numerically verified and must be treated as prose.
The default payload spec for unknown action kinds strips mutation fields and
keeps only those free-text fields when no ID-shaped key is present.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field as dataclass_field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.repo_budgets import SUBSCRIPTION_WATCHLIST_DECISIONS
from app.llm.gate import llm_gate

from . import prompts
from .schemas import (
    CoverageSummary,
    Evidence,
    LLMProposedAction,
    LLMReviewFinding,
    ProposedAction,
    ReconciliationReview,
    ReviewFinding,
    StatementRun,
)
from .tools import (
    IdAllowlist,
    planning_analyst_tool,
    receipt_matcher_tool,
    recurring_analyst_tool,
    statement_auditor_tool,
)


class AnalystOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[LLMReviewFinding] = Field(default_factory=list)
    proposed_actions: list[LLMProposedAction] = Field(default_factory=list)

    @field_validator("findings", "proposed_actions", mode="before")
    @classmethod
    def _base_models_as_dicts(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
        return value


class SynthesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_summary: str
    findings: list[LLMReviewFinding] = Field(default_factory=list)
    proposed_actions: list[LLMProposedAction] = Field(default_factory=list)

    @field_validator("findings", "proposed_actions", mode="before")
    @classmethod
    def _base_models_as_dicts(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
        return value


class ReconGraphState(TypedDict, total=False):
    conn: sqlite3.Connection
    llm: Any
    agent_run_id: str
    statement_run: StatementRun
    coverage: CoverageSummary
    evidence_bundle: dict[str, Any]
    allowed_ids: IdAllowlist
    candidate_findings: list[ReviewFinding]
    candidate_actions: list[ProposedAction]
    period_summary: str
    review: ReconciliationReview


@dataclass(frozen=True)
class PayloadSpec:
    allowed: frozenset[str]
    required: frozenset[str] = frozenset()
    id_fields: dict[str, str] = dataclass_field(default_factory=dict)
    merchant_fields: frozenset[str] = frozenset()
    decision_fields: frozenset[str] = frozenset()


FREE_TEXT_PAYLOAD_KEYS = frozenset({"notes", "rationale"})
DEFAULT_PAYLOAD_SPEC = PayloadSpec(allowed=FREE_TEXT_PAYLOAD_KEYS)
PAYLOAD_SPECS: dict[str, PayloadSpec] = {
    "confirm_match": PayloadSpec(
        allowed=frozenset({"statement_line_id", "transaction_id", *FREE_TEXT_PAYLOAD_KEYS}),
        required=frozenset({"statement_line_id", "transaction_id"}),
        id_fields={"statement_line_id": "statement_line", "transaction_id": "transaction"},
    ),
    "review_unmatched": PayloadSpec(
        allowed=frozenset({"statement_line_id", "statement_line_ids", "transaction_ids", *FREE_TEXT_PAYLOAD_KEYS}),
        required=frozenset({"statement_line_id"}),
        id_fields={
            "statement_line_id": "statement_line",
            "statement_line_ids": "statement_line",
            "transaction_ids": "transaction",
        },
    ),
    "add_subscription": PayloadSpec(
        allowed=frozenset({"merchant", "account_id", "decision", *FREE_TEXT_PAYLOAD_KEYS}),
        required=frozenset({"merchant", "account_id"}),
        id_fields={"account_id": "account"},
        merchant_fields=frozenset({"merchant"}),
        decision_fields=frozenset({"decision"}),
    ),
    "adjust_budget": PayloadSpec(
        allowed=frozenset({"category_id", "month", *FREE_TEXT_PAYLOAD_KEYS}),
        required=frozenset({"category_id"}),
        id_fields={"category_id": "category"},
    ),
    "categorize": PayloadSpec(
        allowed=frozenset({"transaction_id", "category_id", *FREE_TEXT_PAYLOAD_KEYS}),
        required=frozenset({"transaction_id", "category_id"}),
        id_fields={"transaction_id": "transaction", "category_id": "category"},
    ),
}

_FIRST_CAP_RE = re.compile("(.)([A-Z][a-z]+)")
_CAMEL_RE = re.compile("([a-z0-9])([A-Z])")
_NON_KEY_RE = re.compile("[^A-Za-z0-9]+")


def _normalize_payload_key(key: str) -> str:
    with_words = _FIRST_CAP_RE.sub(r"\1_\2", key)
    with_words = _CAMEL_RE.sub(r"\1_\2", with_words)
    return _NON_KEY_RE.sub("_", with_words).strip("_").lower()


_ID_KEY_ALIASES: dict[str, tuple[str, str]] = {
    "transaction_id": ("transaction_id", "transaction"),
    "transaction_ids": ("transaction_ids", "transaction"),
    "transaction_id_list": ("transaction_ids", "transaction"),
    "statement_line_id": ("statement_line_id", "statement_line"),
    "statement_line_ids": ("statement_line_ids", "statement_line"),
    "statement_line_id_list": ("statement_line_ids", "statement_line"),
    "line_id": ("statement_line_id", "statement_line"),
    "line_ids": ("statement_line_ids", "statement_line"),
    "line_id_list": ("statement_line_ids", "statement_line"),
    "category_id": ("category_id", "category"),
    "category_ids": ("category_ids", "category"),
    "category_id_list": ("category_ids", "category"),
    "account_id": ("account_id", "account"),
    "account_ids": ("account_ids", "account"),
    "account_id_list": ("account_ids", "account"),
    "id": ("id", "ambiguous"),
    "ids": ("ids", "ambiguous"),
}


def _canonical_id_key(normalized_key: str) -> tuple[str, str] | None:
    if normalized_key in _ID_KEY_ALIASES:
        return _ID_KEY_ALIASES[normalized_key]
    if normalized_key.endswith("_id") or normalized_key.endswith("_ids") or normalized_key.endswith("_id_list"):
        return normalized_key, "unknown"
    return None


def _is_many_id_key(canonical_key: str) -> bool:
    return canonical_key.endswith("_ids")


def _id_allowed_set(kind: str, allowed: IdAllowlist) -> set[int]:
    if kind == "transaction":
        return allowed.transaction_ids
    if kind == "statement_line":
        return allowed.statement_line_ids
    if kind == "category":
        return allowed.category_ids
    if kind == "account":
        return allowed.account_ids
    return set()


def _payload_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _iter_payload_keys(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            next_path = (*path, key_text)
            yield next_path, item
            yield from _iter_payload_keys(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                yield from _iter_payload_keys(item, (*path, f"[{index}]"))


def _id_shape_violation(payload: dict[str, Any], spec: PayloadSpec) -> str | None:
    for path, _value in _iter_payload_keys(payload):
        normalized = _normalize_payload_key(path[-1])
        id_key = _canonical_id_key(normalized)
        if id_key is None:
            continue
        canonical_key, id_kind = id_key
        if len(path) > 1:
            return f"payload {_payload_path(path)} nested id key unsupported"
        if id_kind in {"ambiguous", "unknown"}:
            return f"payload {path[-1]} is an ambiguous id reference"
        if canonical_key not in spec.allowed:
            return f"payload {path[-1]} is not allowed for this action kind"
    return None


def _aliases_id_vetted_concept(normalized_key: str, spec: PayloadSpec) -> bool:
    if not normalized_key.endswith("_name"):
        return False
    concepts = {
        "category": {"category_id", "category_ids"},
        "account": {"account_id", "account_ids"},
        "transaction": {"transaction_id", "transaction_ids"},
        "line": {"statement_line_id", "statement_line_ids"},
    }
    for concept, id_keys in concepts.items():
        if concept in normalized_key and any(key in spec.id_fields for key in id_keys):
            return True
    return False


def _canonical_payload_key(key: str) -> str:
    normalized = _normalize_payload_key(key)
    id_key = _canonical_id_key(normalized)
    if id_key is not None and id_key[1] not in {"ambiguous", "unknown"}:
        return id_key[0]
    return normalized


def _coerce_payload_ints(value: Any) -> tuple[list[int], list[Any]]:
    raw_items = value if isinstance(value, list) else [value]
    valid: list[int] = []
    invalid: list[Any] = []
    seen: set[int] = set()
    for item in raw_items:
        if isinstance(item, bool) or isinstance(item, (dict, list)):
            invalid.append(item)
            continue
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            invalid.append(item)
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        valid.append(parsed)
    return valid, invalid


def _repair_id_payload_value(
    *,
    key: str,
    value: Any,
    id_kind: str,
    allowed: IdAllowlist,
) -> tuple[Any | None, str | None, str | None]:
    ids, uncoercible = _coerce_payload_ints(value)
    allowed_set = _id_allowed_set(id_kind, allowed)
    valid = [item for item in ids if item in allowed_set]
    invalid = [item for item in ids if item not in allowed_set]
    if _is_many_id_key(key):
        note = None
        if uncoercible or invalid:
            note = f"payload {key} removed invalid ids {invalid + uncoercible!r}"
        return valid if valid else None, note, None
    if uncoercible:
        return None, None, f"payload {key} {uncoercible[0]!r} is not an integer id"
    if invalid:
        return None, None, f"payload {key} {invalid[0]} not in evidence allowlist"
    if len(valid) != 1:
        return None, None, f"payload {key} must contain exactly one vetted id"
    return valid[0], None, None


def _canonical_merchant(value: Any, allowed: IdAllowlist) -> str | None:
    if not isinstance(value, str):
        return None
    target = value.strip().casefold()
    if not target:
        return None
    for merchant in sorted(allowed.merchants):
        if merchant.casefold() == target:
            return merchant
    return None


def _is_scalar_payload_value(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _empty_required_value(value: Any) -> bool:
    return value is None or value == "" or value == []


def _run_months_for_guard(statement_run: StatementRun) -> set[str]:
    return set(statement_run.months or [statement_run.month])


async def _invoke_structured(llm: Any, schema: type[BaseModel], system_prompt: str, payload: dict[str, Any]) -> BaseModel:
    messages = [
        ("system", system_prompt),
        ("human", json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)),
    ]
    async with llm_gate():
        try:
            raw = llm.with_structured_output(schema, method="json_schema").invoke(messages)
        except Exception:
            raw = llm.with_structured_output(schema, method="function_calling").invoke(messages)
    if isinstance(raw, schema):
        return raw
    return schema.model_validate(raw)


def _base_payload(state: ReconGraphState, evidence: BaseModel | dict[str, Any]) -> dict[str, Any]:
    allowed = state.get("allowed_ids", IdAllowlist())
    evidence_payload = evidence.model_dump(mode="json") if isinstance(evidence, BaseModel) else evidence
    return {
        "agent_run_id": state["agent_run_id"],
        "statement_run": state["statement_run"].model_dump(mode="json"),
        "allowed_ids": allowed.as_prompt_dict(),
        "evidence": evidence_payload,
    }


def _append_output(state: ReconGraphState, output: AnalystOutput) -> dict[str, Any]:
    return {
        "candidate_findings": state.get("candidate_findings", []) + output.findings,
        "candidate_actions": state.get("candidate_actions", []) + output.proposed_actions,
    }


def _merge_allowed(state: ReconGraphState, ids: IdAllowlist) -> IdAllowlist:
    return state.get("allowed_ids", IdAllowlist()).merge(ids)


async def statement_auditor_node(state: ReconGraphState) -> dict[str, Any]:
    result = statement_auditor_tool(state["conn"], state["statement_run"])
    allowed = _merge_allowed(state, result.allowed_ids)
    next_state = {
        **state,
        "coverage": result.coverage,
        "allowed_ids": allowed,
        "evidence_bundle": {**state.get("evidence_bundle", {}), "statement_audit": result.model_dump(mode="json")},
    }
    output = await _invoke_structured(
        state["llm"],
        AnalystOutput,
        prompts.STATEMENT_AUDITOR_PROMPT,
        _base_payload(next_state, result),
    )
    return {**_append_output(state, output), "coverage": result.coverage, "allowed_ids": allowed, "evidence_bundle": next_state["evidence_bundle"]}


async def receipt_matcher_node(state: ReconGraphState) -> dict[str, Any]:
    result = receipt_matcher_tool(state["conn"], state["statement_run"])
    allowed = _merge_allowed(state, result.allowed_ids)
    next_state = {
        **state,
        "allowed_ids": allowed,
        "evidence_bundle": {**state.get("evidence_bundle", {}), "receipt_matches": result.model_dump(mode="json")},
    }
    output = await _invoke_structured(
        state["llm"],
        AnalystOutput,
        prompts.RECEIPT_MATCHER_PROMPT,
        _base_payload(next_state, result),
    )
    return {**_append_output(state, output), "allowed_ids": allowed, "evidence_bundle": next_state["evidence_bundle"]}


async def recurring_analyst_node(state: ReconGraphState) -> dict[str, Any]:
    result = recurring_analyst_tool(state["conn"], state["statement_run"])
    allowed = _merge_allowed(state, result.allowed_ids)
    next_state = {
        **state,
        "allowed_ids": allowed,
        "evidence_bundle": {**state.get("evidence_bundle", {}), "recurring": result.model_dump(mode="json")},
    }
    output = await _invoke_structured(
        state["llm"],
        AnalystOutput,
        prompts.RECURRING_ANALYST_PROMPT,
        _base_payload(next_state, result),
    )
    return {**_append_output(state, output), "allowed_ids": allowed, "evidence_bundle": next_state["evidence_bundle"]}


async def planning_analyst_node(state: ReconGraphState) -> dict[str, Any]:
    result = planning_analyst_tool(state["conn"], state["statement_run"])
    allowed = _merge_allowed(state, result.allowed_ids)
    next_state = {
        **state,
        "allowed_ids": allowed,
        "evidence_bundle": {**state.get("evidence_bundle", {}), "planning": result.model_dump(mode="json")},
    }
    output = await _invoke_structured(
        state["llm"],
        AnalystOutput,
        prompts.PLANNING_ANALYST_PROMPT,
        _base_payload(next_state, result),
    )
    return {**_append_output(state, output), "allowed_ids": allowed, "evidence_bundle": next_state["evidence_bundle"]}


async def synthesizer_node(state: ReconGraphState) -> dict[str, Any]:
    payload = {
        "agent_run_id": state["agent_run_id"],
        "statement_run": state["statement_run"].model_dump(mode="json"),
        "coverage": state["coverage"].model_dump(mode="json") if state.get("coverage") else None,
        "allowed_ids": state.get("allowed_ids", IdAllowlist()).as_prompt_dict(),
        "evidence_bundle": state.get("evidence_bundle", {}),
        "candidate_findings": [finding.model_dump(mode="json") for finding in state.get("candidate_findings", [])],
        "candidate_actions": [action.model_dump(mode="json") for action in state.get("candidate_actions", [])],
    }
    output = await _invoke_structured(
        state["llm"],
        SynthesisOutput,
        prompts.SYNTHESIZER_PROMPT,
        payload,
    )
    return {
        "period_summary": output.period_summary,
        "candidate_findings": output.findings,
        "candidate_actions": output.proposed_actions,
    }


def _repair_evidence(evidence: Evidence, allowed: IdAllowlist) -> Evidence:
    return Evidence(
        transaction_ids=[txn_id for txn_id in evidence.transaction_ids if txn_id in allowed.transaction_ids],
        statement_line_ids=[line_id for line_id in evidence.statement_line_ids if line_id in allowed.statement_line_ids],
        category_ids=[category_id for category_id in evidence.category_ids if category_id in allowed.category_ids],
    )


def _has_invalid_evidence(evidence: Evidence, allowed: IdAllowlist) -> bool:
    return (
        any(txn_id not in allowed.transaction_ids for txn_id in evidence.transaction_ids)
        or any(line_id not in allowed.statement_line_ids for line_id in evidence.statement_line_ids)
        or any(category_id not in allowed.category_ids for category_id in evidence.category_ids)
    )


def _repair_payload(
    action: ProposedAction,
    allowed: IdAllowlist,
    statement_run: StatementRun,
) -> tuple[dict[str, Any], list[str], str | None]:
    spec = PAYLOAD_SPECS.get(action.kind, DEFAULT_PAYLOAD_SPEC)
    payload = action.payload if isinstance(action.payload, dict) else {}
    notes: list[str] = []
    violation = _id_shape_violation(payload, spec)
    if violation is not None:
        return {}, notes, violation

    repaired: dict[str, Any] = {}
    for raw_key, value in payload.items():
        normalized_key = _normalize_payload_key(raw_key)
        canonical_key = _canonical_payload_key(raw_key)
        if _aliases_id_vetted_concept(normalized_key, spec):
            notes.append(f"stripped payload {raw_key}: name alias must use vetted id")
            continue
        if canonical_key not in spec.allowed:
            notes.append(f"stripped payload {raw_key}: not allowed for {action.kind}")
            continue
        if canonical_key in spec.id_fields:
            repaired_value, note, drop_reason = _repair_id_payload_value(
                key=canonical_key,
                value=value,
                id_kind=spec.id_fields[canonical_key],
                allowed=allowed,
            )
            if drop_reason is not None:
                return {}, notes, drop_reason
            if note is not None:
                notes.append(note)
            if repaired_value is not None:
                repaired[canonical_key] = repaired_value
            continue
        if canonical_key in spec.merchant_fields:
            merchant = _canonical_merchant(value, allowed)
            if merchant is None:
                return {}, notes, f"payload {canonical_key} {value!r} not in evidence merchant allowlist"
            repaired[canonical_key] = merchant
            continue
        if canonical_key in spec.decision_fields:
            decision = str(value).strip().lower() if isinstance(value, str) else ""
            if decision not in SUBSCRIPTION_WATCHLIST_DECISIONS:
                return {}, notes, f"payload {canonical_key} {value!r} is not a known subscription decision"
            repaired[canonical_key] = decision
            continue
        if canonical_key == "month":
            month = str(value).strip() if isinstance(value, str) else ""
            if month not in _run_months_for_guard(statement_run):
                return {}, notes, f"payload {canonical_key} {value!r} is not in statement run months"
            repaired[canonical_key] = month
            continue
        if not _is_scalar_payload_value(value):
            notes.append(f"stripped payload {raw_key}: value is not scalar")
            continue
        repaired[canonical_key] = value

    missing = [key for key in sorted(spec.required) if key not in repaired or _empty_required_value(repaired[key])]
    if missing:
        return {}, notes, f"payload missing required {', '.join(missing)}"
    return repaired, notes, None


def _note_repaired_evidence(label: str, original: Evidence, repaired: Evidence) -> str | None:
    removed_txns = [item for item in original.transaction_ids if item not in repaired.transaction_ids]
    removed_lines = [item for item in original.statement_line_ids if item not in repaired.statement_line_ids]
    removed_categories = [item for item in original.category_ids if item not in repaired.category_ids]
    parts: list[str] = []
    if removed_txns:
        parts.append(f"transaction_ids {removed_txns!r}")
    if removed_lines:
        parts.append(f"statement_line_ids {removed_lines!r}")
    if removed_categories:
        parts.append(f"category_ids {removed_categories!r}")
    if not parts:
        return None
    return f"repaired {label}: removed ids not in evidence allowlist ({'; '.join(parts)})"


def guard_review(
    *,
    agent_run_id: str,
    statement_run: StatementRun,
    coverage: CoverageSummary | None,
    period_summary: str,
    findings: list[ReviewFinding],
    proposed_actions: list[ProposedAction],
    allowed_ids: IdAllowlist,
) -> ReconciliationReview:
    guarded_findings: list[ReviewFinding] = []
    guard_notes: list[str] = []
    for finding in findings:
        try:
            evidence = _repair_evidence(finding.evidence, allowed_ids)
            note = _note_repaired_evidence(f"finding {finding.finding_id}", finding.evidence, evidence)
            if evidence.is_empty():
                guard_notes.append(f"dropped finding {finding.finding_id}: no cited ids survived evidence allowlist")
                continue
            if note is not None:
                guard_notes.append(note)
            guarded_findings.append(finding.model_copy(update={"evidence": evidence}))
        except Exception as exc:
            guard_notes.append(f"dropped finding {getattr(finding, 'finding_id', '<unknown>')}: guard error {type(exc).__name__}")

    guarded_actions: list[ProposedAction] = []
    for action in proposed_actions:
        try:
            if _has_invalid_evidence(action.evidence, allowed_ids):
                guard_notes.append(f"dropped action {action.kind}: evidence ids not in evidence allowlist")
                continue
            evidence = _repair_evidence(action.evidence, allowed_ids)
            if evidence.is_empty():
                guard_notes.append(f"dropped action {action.kind}: no cited ids survived evidence allowlist")
                continue
            payload, payload_notes, drop_reason = _repair_payload(action, allowed_ids, statement_run)
            if drop_reason is not None:
                guard_notes.extend(f"repaired action {action.kind}: {note}" for note in payload_notes)
                guard_notes.append(f"dropped action {action.kind}: {drop_reason}")
                continue
            guard_notes.extend(f"repaired action {action.kind}: {note}" for note in payload_notes)
            guarded_actions.append(
                action.model_copy(
                    update={
                        "evidence": evidence,
                        "payload": payload,
                        "agent_run_id": agent_run_id,
                    }
                )
            )
        except Exception as exc:
            guard_notes.append(f"dropped action {getattr(action, 'kind', '<unknown>')}: guard error {type(exc).__name__}")

    guarded_findings.sort(key=lambda finding: (finding.priority, finding.finding_id))
    return ReconciliationReview(
        agent_run_id=agent_run_id,
        statement_run=statement_run,
        period_summary=period_summary,
        findings=guarded_findings,
        proposed_actions=guarded_actions,
        coverage=coverage,
        guard_notes=guard_notes,
    )


def guard_node(state: ReconGraphState) -> dict[str, Any]:
    review = guard_review(
        agent_run_id=state["agent_run_id"],
        statement_run=state["statement_run"],
        coverage=state.get("coverage"),
        period_summary=state.get("period_summary") or "",
        findings=state.get("candidate_findings", []),
        proposed_actions=state.get("candidate_actions", []),
        allowed_ids=state.get("allowed_ids", IdAllowlist()),
    )
    return {"review": review}


def build_reconciliation_graph():
    graph = StateGraph(ReconGraphState)
    graph.add_node("statement_auditor", statement_auditor_node)
    graph.add_node("receipt_matcher", receipt_matcher_node)
    graph.add_node("recurring_analyst", recurring_analyst_node)
    graph.add_node("planning_analyst", planning_analyst_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("guard", guard_node)
    graph.add_edge(START, "statement_auditor")
    graph.add_edge("statement_auditor", "receipt_matcher")
    graph.add_edge("receipt_matcher", "recurring_analyst")
    graph.add_edge("recurring_analyst", "planning_analyst")
    graph.add_edge("planning_analyst", "synthesizer")
    graph.add_edge("synthesizer", "guard")
    graph.add_edge("guard", END)
    return graph.compile()
