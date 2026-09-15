"""Deterministic evals for reconciliation insight agent output."""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.actions import get_handler
from app.db import engine, repo_actions

_RECON_DIR = Path(__file__).resolve().parents[1] / "agents" / "recon_analyst"
_EVAL_RECON_PACKAGE = "_finn_nancy_eval_recon_analyst"


def _load_recon_module(name: str) -> types.ModuleType:
    """Load langgraph-free recon submodules without importing package __init__."""
    package = sys.modules.get(_EVAL_RECON_PACKAGE)
    if package is None:
        package = types.ModuleType(_EVAL_RECON_PACKAGE)
        package.__path__ = [str(_RECON_DIR)]  # type: ignore[attr-defined]
        package.__package__ = _EVAL_RECON_PACKAGE
        sys.modules[_EVAL_RECON_PACKAGE] = package

    fullname = f"{_EVAL_RECON_PACKAGE}.{name}"
    existing = sys.modules.get(fullname)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(fullname, _RECON_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load reconciliation analyst module {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    return module


_schemas = _load_recon_module("schemas")
_tools = _load_recon_module("tools")
_evidence = _load_recon_module("evidence")

CoverageSummary = _schemas.CoverageSummary
EvidenceBundle = _evidence.EvidenceBundle
IdAllowlist = _tools.IdAllowlist
PlanningAnalystResult = _tools.PlanningAnalystResult
ReceiptMatcherResult = _tools.ReceiptMatcherResult
RecurringAnalystResult = _tools.RecurringAnalystResult
StatementAuditResult = _tools.StatementAuditResult
StatementRun = _schemas.StatementRun
gather_all_evidence = _evidence.gather_all_evidence
planning_analyst_tool = _tools.planning_analyst_tool
receipt_matcher_tool = _tools.receipt_matcher_tool
recurring_analyst_tool = _tools.recurring_analyst_tool
resolve_statement_run = _evidence.resolve_statement_run
statement_auditor_tool = _tools.statement_auditor_tool

_RECURRING_FINDING_KINDS = {
    "recurring_change",
    "recurring_price_increase",
    "recurring_price_decrease",
}
_RECURRING_DIRECTION_BY_KIND = {
    "recurring_price_increase": "increase",
    "recurring_price_decrease": "decrease",
}
_IMPLEMENTED_ACTION_KINDS = {"recategorization", "subscription_label"}
_STUB_ACTION_KINDS = {"receipt_match", "budget_update", "recurring_series"}
_UNROUTED_INFORMATIONAL_PAYLOAD_KEYS = {"notes", "rationale"}


@dataclass(frozen=True)
class EvalResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeTrace:
    node: str
    evidence_key: str | None
    finding_ids: list[str] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)
    action_kinds: list[str] = field(default_factory=list)
    evidence: dict[str, Any] | None = None


def _result(violations: list[str], details: dict[str, Any] | None = None) -> EvalResult:
    return EvalResult(ok=not violations, violations=violations, details=details or {})


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


def _evidence_ids(evidence: Any) -> tuple[set[int], set[int], set[int]]:
    if evidence is None:
        return set(), set(), set()
    return (
        {int(item) for item in getattr(evidence, "transaction_ids", [])},
        {int(item) for item in getattr(evidence, "statement_line_ids", [])},
        {int(item) for item in getattr(evidence, "category_ids", [])},
    )


def _action_label(action: Any, index: int) -> str:
    return f"action {index} {getattr(action, 'kind', '<unknown>')}"


def _finding_label(finding: Any) -> str:
    return f"finding {getattr(finding, 'finding_id', '<unknown>')}"


def coverage_ground_truth(conn: sqlite3.Connection, run: Any) -> Any:
    """Compute statement coverage from base statement_lines rows."""
    source_document_id = getattr(run, "source_document_id", None)
    if source_document_id is not None:
        rows = conn.execute(
            """
            SELECT line.id, line.source_document_id, line.account_id,
                   line.posted_on, line.amount_cents, line.match_status,
                   line.matched_transaction_id,
                   transaction_row.flow_kind AS transaction_flow_kind,
                   flow_status.semantic_status
            FROM statement_lines line
            LEFT JOIN transactions transaction_row
              ON transaction_row.id=line.matched_transaction_id
            LEFT JOIN v_transaction_flow_status flow_status
              ON flow_status.transaction_id=transaction_row.id
            WHERE line.source_document_id = ?
              AND line.review_disposition = 'active'
            ORDER BY line.id
            """,
            (source_document_id,),
        ).fetchall()
    else:
        month = str(getattr(run, "month"))
        rows = [
            row
            for row in conn.execute(
                """
                SELECT line.id, line.source_document_id, line.account_id,
                       line.posted_on, line.amount_cents, line.match_status,
                       line.matched_transaction_id,
                       transaction_row.flow_kind AS transaction_flow_kind,
                       flow_status.semantic_status
                FROM statement_lines line
                LEFT JOIN transactions transaction_row
                  ON transaction_row.id=line.matched_transaction_id
                LEFT JOIN v_transaction_flow_status flow_status
                  ON flow_status.transaction_id=transaction_row.id
                WHERE line.review_disposition = 'active'
                ORDER BY line.id
                """
            ).fetchall()
            if str(row["posted_on"])[:7] == month
        ]

    matched_transaction_ids = sorted(
        {
            int(row["matched_transaction_id"])
            for row in rows
            if row["matched_transaction_id"] is not None
        }
    )
    uncategorized_transaction_ids: set[int] = set()
    if matched_transaction_ids:
        placeholders = ",".join("?" for _ in matched_transaction_ids)
        uncategorized_transaction_ids = {
            int(row["transaction_id"])
            for row in conn.execute(
                f"""
                SELECT DISTINCT ts.transaction_id
                FROM transaction_splits ts
                JOIN categories c ON c.id = ts.category_id
                WHERE c.name = ?
                  AND ts.transaction_id IN ({placeholders})
                """,
                ["Uncategorized", *matched_transaction_ids],
            ).fetchall()
        }

    summary = {
        "line_count": 0,
        "statement_spend_cents": 0,
        "covered_spend_cents": 0,
        "unmatched_spend_cents": 0,
        "ignored_spend_cents": 0,
        "income_cents": 0,
        "attention_spend_cents": 0,
    }
    for row in rows:
        amount_cents = int(row["amount_cents"] or 0)
        match_status = str(row["match_status"] or "")
        spend_cents = abs(amount_cents) if amount_cents < 0 else 0
        flow_kind = str(row["transaction_flow_kind"] or "")
        semantic_status = str(row["semantic_status"] or "unknown")
        report_eligible = semantic_status == "complete"
        income_cents = (
            amount_cents
            if (
                amount_cents > 0
                and match_status in ("matched", "promoted")
                and report_eligible
                and flow_kind in ("income", "interest")
            )
            else 0
        )
        matched_transaction_id = row["matched_transaction_id"]

        if income_cents:
            bucket = "income"
        elif (
            amount_cents > 0
            and match_status in ("matched", "promoted")
            and report_eligible
        ):
            bucket = "resolved_positive"
        elif amount_cents > 0:
            bucket = "flow_review"
        elif (
            match_status in ("matched", "promoted")
            and not report_eligible
        ):
            bucket = "unmatched"
        elif (
            match_status in ("matched", "promoted")
            and flow_kind in ("internal_transfer", "card_payment")
        ):
            bucket = "ignored"
        elif match_status == "ignored":
            bucket = "ignored"
        elif match_status in ("matched", "promoted"):
            bucket = "covered"
        else:
            bucket = "unmatched"

        summary["line_count"] += 1
        summary["statement_spend_cents"] += spend_cents
        summary["income_cents"] += income_cents
        if bucket == "covered":
            summary["covered_spend_cents"] += spend_cents
        elif bucket == "unmatched":
            summary["unmatched_spend_cents"] += spend_cents
        elif bucket == "ignored":
            summary["ignored_spend_cents"] += spend_cents

        if amount_cents >= 0 or bucket == "ignored":
            continue
        needs_attention = (
            row["account_id"] is None
            or match_status in ("unmatched", "needs_review")
            or (
                match_status in ("matched", "promoted")
                and matched_transaction_id is not None
                and int(matched_transaction_id) in uncategorized_transaction_ids
            )
        )
        if needs_attention:
            summary["attention_spend_cents"] += spend_cents

    denominator = int(summary["covered_spend_cents"]) + int(summary["unmatched_spend_cents"])
    summary["coverage_pct"] = (
        round(100.0 * int(summary["covered_spend_cents"]) / denominator, 1)
        if denominator
        else 0.0
    )
    return CoverageSummary.model_validate(summary)


def check_coverage(review: Any, conn: sqlite3.Connection) -> EvalResult:
    expected = coverage_ground_truth(conn, review.statement_run)
    actual = getattr(review, "coverage", None)
    if actual is None:
        return _result(
            ["coverage missing"],
            {"expected": expected.model_dump(mode="json"), "actual": None},
        )

    fields = [
        "line_count",
        "statement_spend_cents",
        "covered_spend_cents",
        "unmatched_spend_cents",
        "ignored_spend_cents",
        "income_cents",
        "attention_spend_cents",
        "coverage_pct",
    ]
    violations: list[str] = []
    for item in fields:
        expected_value = getattr(expected, item)
        actual_value = getattr(actual, item)
        if actual_value != expected_value:
            violations.append(
                f"coverage {item} expected {expected_value!r} got {actual_value!r}"
            )
    return _result(
        violations,
        {
            "expected": expected.model_dump(mode="json"),
            "actual": actual.model_dump(mode="json") if hasattr(actual, "model_dump") else dict(actual),
        },
    )


def _existing_ids(conn: sqlite3.Connection, table: str, ids: set[int]) -> set[int]:
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE id IN ({placeholders})",
        sorted(ids),
    ).fetchall()
    return {int(row["id"]) for row in rows}


def _check_evidence_rows(
    conn: sqlite3.Connection,
    *,
    label: str,
    evidence: Any,
) -> list[str]:
    violations: list[str] = []
    txns, lines, categories = _evidence_ids(evidence)
    if not (txns or lines or categories):
        violations.append(f"{label} has empty evidence")

    missing_txns = sorted(txns - _existing_ids(conn, "transactions", txns))
    missing_lines = sorted(lines - _existing_ids(conn, "statement_lines", lines))
    missing_categories = sorted(categories - _existing_ids(conn, "categories", categories))
    violations.extend(f"{label} transaction_id {item} not found" for item in missing_txns)
    violations.extend(f"{label} statement_line_id {item} not found" for item in missing_lines)
    violations.extend(f"{label} category_id {item} not found" for item in missing_categories)
    return violations


def check_citations(review: Any, conn: sqlite3.Connection) -> EvalResult:
    violations: list[str] = []
    for finding in review.findings:
        violations.extend(
            _check_evidence_rows(
                conn,
                label=_finding_label(finding),
                evidence=finding.evidence,
            )
        )
    for index, action in enumerate(review.proposed_actions):
        violations.extend(
            _check_evidence_rows(
                conn,
                label=_action_label(action, index),
                evidence=action.evidence,
            )
        )
    return _result(violations)


def _recurring_change_ids(change: Any) -> tuple[set[int], set[int]]:
    transaction_ids = {
        *[int(item) for item in getattr(change, "current_transaction_ids", [])],
        *[int(item) for item in getattr(change, "previous_transaction_ids", [])],
    }
    line_ids = {
        *[int(item) for item in getattr(change, "current_statement_line_ids", [])],
        *[int(item) for item in getattr(change, "previous_statement_line_ids", [])],
    }
    return transaction_ids, line_ids


def expected_recurring_merchants(evidence_bundle: Any) -> set[str]:
    return {change.merchant for change in evidence_bundle.recurring.changes}


def _recurring_change_label(change: Any) -> str:
    return f"{change.merchant} {change.previous_month}->{change.month}"


def _recurring_finding_overlaps_change(
    finding: Any,
    change_txns: set[int],
    change_lines: set[int],
) -> bool:
    finding_txns, finding_lines, _ = _evidence_ids(finding.evidence)
    return bool(finding_txns & change_txns or finding_lines & change_lines)


def check_recurring_insights(review: Any, evidence_bundle: Any) -> EvalResult:
    changes = list(evidence_bundle.recurring.changes)
    indexed_changes = [
        (change, *_recurring_change_ids(change))
        for change in changes
    ]
    violations: list[str] = []
    matched: dict[str, str] = {}
    recurring_findings = [
        finding
        for finding in review.findings
        if finding.kind in _RECURRING_FINDING_KINDS
    ]
    for finding in recurring_findings:
        finding_txns, finding_lines, _ = _evidence_ids(finding.evidence)
        match = next(
            (
                change
                for change, change_txns, change_lines in indexed_changes
                if finding_txns & change_txns or finding_lines & change_lines
            ),
            None,
        )
        if match is None:
            violations.append(
                f"recurring finding {finding.finding_id} cites no meaningful recurring change"
            )
            continue
        claimed_direction = _RECURRING_DIRECTION_BY_KIND.get(finding.kind)
        if claimed_direction is not None and match.direction != claimed_direction:
            violations.append(
                f"recurring finding {finding.finding_id} claims {claimed_direction} "
                f"but matched change {_recurring_change_label(match)} direction is {match.direction}"
            )
        matched[finding.finding_id] = match.merchant

    for change, change_txns, change_lines in indexed_changes:
        if any(
            _recurring_finding_overlaps_change(finding, change_txns, change_lines)
            for finding in recurring_findings
        ):
            continue
        violations.append(
            f"meaningful recurring change {_recurring_change_label(change)} not represented in findings"
        )

    return _result(
        violations,
        {
            "expected_merchants": sorted(expected_recurring_merchants(evidence_bundle)),
            "matched_findings": matched,
        },
    )


def _candidate_ids(candidate: Any) -> tuple[set[int], set[int]]:
    return (
        {int(item) for item in getattr(candidate, "transaction_ids", [])},
        {int(item) for item in getattr(candidate, "statement_line_ids", [])},
    )


def _subscription_evidence_overlaps_candidate(candidate: Any, evidence: Any) -> bool:
    evidence_txns, evidence_lines, _ = _evidence_ids(evidence)
    candidate_txns, candidate_lines = _candidate_ids(candidate)
    return bool(evidence_txns & candidate_txns or evidence_lines & candidate_lines)


def _subscription_payload_matches_candidate(candidate: Any, payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return True
    merchant = str(payload.get("merchant") or "").strip()
    account_id = payload.get("account_id")
    if not merchant or account_id in (None, ""):
        return False
    try:
        account_id_int = int(account_id)
    except (TypeError, ValueError):
        return False
    return (
        account_id_int == int(candidate.account_id)
        and merchant.casefold() == str(candidate.merchant).casefold()
    )


def _subscription_candidate_for_payload(
    candidates: list[Any],
    payload: dict[str, Any] | None,
) -> Any | None:
    payload = payload or {}
    merchant = str(payload.get("merchant") or "").strip()
    account_id = payload.get("account_id")
    if not merchant or account_id in (None, ""):
        return None
    try:
        account_id_int = int(account_id)
    except (TypeError, ValueError):
        return None
    return next(
        (
            candidate
            for candidate in candidates
            if account_id_int == int(candidate.account_id)
            and merchant.casefold() == str(candidate.merchant).casefold()
        ),
        None,
    )


def _matches_subscription_candidate(candidate: Any, *, evidence: Any, payload: dict[str, Any] | None = None) -> bool:
    return (
        _subscription_evidence_overlaps_candidate(candidate, evidence)
        and _subscription_payload_matches_candidate(candidate, payload)
    )


def check_subscriptions(review: Any, evidence_bundle: Any) -> EvalResult:
    candidates = list(evidence_bundle.planning.subscription_candidates)
    violations: list[str] = []
    matched: dict[str, str] = {}
    subscription_findings = [
        finding
        for finding in review.findings
        if finding.kind == "new_subscription"
    ]
    subscription_actions = [
        (index, action)
        for index, action in enumerate(review.proposed_actions)
        if action.kind == "add_subscription"
    ]

    for finding in subscription_findings:
        match = next(
            (
                candidate
                for candidate in candidates
                if _matches_subscription_candidate(candidate, evidence=finding.evidence)
            ),
            None,
        )
        if match is None:
            violations.append(
                f"subscription finding {finding.finding_id} does not match a subscription candidate"
            )
            continue
        matched[f"finding:{finding.finding_id}"] = match.merchant

    for index, action in subscription_actions:
        match = next(
            (
                candidate
                for candidate in candidates
                if _matches_subscription_candidate(
                    candidate,
                    evidence=action.evidence,
                    payload=action.payload,
                )
            ),
            None,
        )
        if match is None:
            payload_candidate = _subscription_candidate_for_payload(candidates, action.payload)
            if payload_candidate is not None:
                violations.append(
                    f"subscription action {index} add_subscription evidence does not cite "
                    f"candidate rows for {payload_candidate.merchant} account {payload_candidate.account_id}"
                )
            else:
                violations.append(
                    f"subscription action {index} add_subscription does not match a subscription candidate"
                )
            continue
        matched[f"action:{index}"] = match.merchant

    for candidate in candidates:
        represented = any(
            _matches_subscription_candidate(candidate, evidence=finding.evidence)
            for finding in subscription_findings
        ) or any(
            _matches_subscription_candidate(
                candidate,
                evidence=action.evidence,
                payload=action.payload,
            )
            for _, action in subscription_actions
        )
        if represented:
            continue
        violations.append(
            f"subscription candidate {candidate.merchant} account {candidate.account_id} "
            "not represented in findings or actions"
        )

    return _result(
        violations,
        {
            "expected_candidates": [
                {"merchant": item.merchant, "account_id": item.account_id}
                for item in candidates
            ],
            "matched": matched,
        },
    )


def adapt_action(action: Any) -> tuple[str, dict[str, Any]] | None:
    # Candidate for promotion to production routing when analyst actions leave evals.
    payload = dict(getattr(action, "payload", {}) or {})
    if action.kind == "categorize":
        if "transaction_id" not in payload or "category_id" not in payload:
            return "recategorization", payload
        return (
            "recategorization",
            {
                "transaction_id": payload["transaction_id"],
                "to_category_id": payload["category_id"],
            },
        )
    if action.kind == "add_subscription":
        return (
            "subscription_label",
            {
                "merchant": payload.get("merchant"),
                "account_id": payload.get("account_id"),
                "decision": payload.get("decision") or "subscription",
            },
        )
    if action.kind == "confirm_match":
        return "receipt_match", payload
    if action.kind == "review_unmatched":
        return "receipt_match", payload
    if action.kind == "adjust_budget":
        return "budget_update", payload
    return None


def _unrouted_structured_payload_keys(action: Any) -> list[str]:
    payload = dict(getattr(action, "payload", {}) or {})
    return sorted(set(payload) - _UNROUTED_INFORMATIONAL_PAYLOAD_KEYS)


def enqueue_review_actions(conn: sqlite3.Connection, review: Any) -> list[int]:
    """Enqueue routed review actions.

    Unknown analyst kinds are skipped when their payload has only guard-legal
    free-text keys. Unknown kinds with structured payload keys still raise,
    because that means an unroutable action reached this layer with mutation
    intent attached.
    """
    proposal_ids: list[int] = []
    for action in review.proposed_actions:
        adapted = adapt_action(action)
        if adapted is None:
            structured_keys = _unrouted_structured_payload_keys(action)
            if not structured_keys:
                continue
            raise ValueError(
                f"unknown analyst action kind {action.kind} with structured payload keys: "
                f"{structured_keys}"
            )
        kind, payload = adapted
        proposal_ids.append(
            repo_actions.enqueue_proposal(
                conn,
                kind=kind,
                payload=payload,
                evidence=_model_dump(action.evidence),
                confidence=action.confidence,
                rationale=action.rationale,
                agent_run_id=action.agent_run_id,
            )
        )
    return proposal_ids


def _sqlite_backup(source_db_path: str | Path, target_db_path: str | Path) -> None:
    source = sqlite3.connect(f"file:{source_db_path}?mode=ro", uri=True)
    target = sqlite3.connect(str(target_db_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def db_snapshot(db_path: str | Path) -> tuple[tuple[str, tuple[str, ...], tuple[tuple[Any, ...], ...]], ...]:
    """Return an ordered dump of every non-SQLite table."""
    with engine.read_conn(db_path) as conn:
        tables = [
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        out: list[tuple[str, tuple[str, ...], tuple[tuple[Any, ...], ...]]] = []
        for table in tables:
            columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
            ]
            quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
            order_by = ", ".join(_quote_identifier(column) for column in columns)
            rows = conn.execute(
                f"SELECT {quoted_columns} FROM {_quote_identifier(table)} ORDER BY {order_by}"
            ).fetchall()
            out.append(
                (
                    table,
                    tuple(columns),
                    tuple(tuple(row[column] for column in columns) for row in rows),
                )
            )
        return tuple(out)


def _snapshot_tables(
    snapshot: tuple[tuple[str, tuple[str, ...], tuple[tuple[Any, ...], ...]], ...]
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]]:
    return {table: (columns, rows) for table, columns, rows in snapshot}


def _changed_tables(
    before: tuple[tuple[str, tuple[str, ...], tuple[tuple[Any, ...], ...]], ...],
    after: tuple[tuple[str, tuple[str, ...], tuple[tuple[Any, ...], ...]], ...],
) -> list[str]:
    before_tables = _snapshot_tables(before)
    after_tables = _snapshot_tables(after)
    return [
        table
        for table in sorted(set(before_tables) | set(after_tables))
        if before_tables.get(table) != after_tables.get(table)
    ]


def check_emission_read_only(
    db_path: str | Path,
    before_snapshot: tuple[tuple[str, tuple[str, ...], tuple[tuple[Any, ...], ...]], ...],
) -> EvalResult:
    after_snapshot = db_snapshot(db_path)
    changed = _changed_tables(before_snapshot, after_snapshot)
    return _result(
        [f"db mutated during emission: {table}" for table in changed],
        {"changed_tables": changed},
    )


def _rows_for_query(
    conn: sqlite3.Connection,
    query: str,
    args: tuple[Any, ...],
) -> tuple[tuple[tuple[str, Any], ...], ...]:
    rows = conn.execute(query, args).fetchall()
    return tuple(tuple((key, row[key]) for key in row.keys()) for row in rows)


def _affected_snapshot(conn: sqlite3.Connection, kind: str, payload: dict[str, Any]) -> tuple[Any, ...]:
    if kind == "recategorization":
        transaction_id = int(payload["transaction_id"])
        return (
            _rows_for_query(
                conn,
                "SELECT * FROM transactions WHERE id=? ORDER BY id",
                (transaction_id,),
            ),
            _rows_for_query(
                conn,
                "SELECT * FROM transaction_splits WHERE transaction_id=? ORDER BY id",
                (transaction_id,),
            ),
        )
    if kind == "subscription_label":
        return (
            _rows_for_query(
                conn,
                """
                SELECT *
                FROM subscription_watchlist_decisions
                WHERE merchant=? AND account_id=?
                ORDER BY id
                """,
                (payload["merchant"], int(payload["account_id"])),
            ),
        )
    return ()


def _exercise_implemented_action(
    conn: sqlite3.Connection,
    *,
    proposal_id: int,
    kind: str,
    payload: dict[str, Any],
    label: str,
) -> list[str]:
    violations: list[str] = []
    handler = get_handler(kind)
    before = _affected_snapshot(conn, kind, payload)
    applied = handler.apply(conn, payload)
    after_apply = _affected_snapshot(conn, kind, payload)
    if applied.get("noop") or after_apply == before:
        violations.append(f"{label} {kind} apply did not mutate affected rows")
        return violations

    repo_actions.mark_applied(
        conn,
        proposal_id,
        status="approved",
        revert=applied.get("revert"),
        detail={"apply": applied.get("detail", {})},
        actor="eval",
    )
    revert_payload = applied.get("revert")
    if revert_payload is None:
        violations.append(f"{label} {kind} apply produced no revert payload")
        return violations

    reverted = handler.revert(conn, revert_payload)
    repo_actions.mark_reverted(
        conn,
        proposal_id,
        detail={"revert": reverted},
        actor="eval",
    )
    after_revert = _affected_snapshot(conn, kind, payload)
    if after_revert != before:
        violations.append(f"{label} {kind} revert did not restore affected rows")
    return violations


def _exercise_stub_action(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict[str, Any],
    label: str,
) -> list[str]:
    handler = get_handler(kind)
    try:
        handler.apply(conn, payload)
    except NotImplementedError:
        return []
    return [f"{label} {kind} apply did not raise NotImplementedError"]


def check_action_safety(review: Any, source_db_path: str | Path) -> EvalResult:
    violations: list[str] = []
    details: dict[str, Any] = {"checked_actions": [], "unrouted_informational": []}

    for index, action in enumerate(review.proposed_actions):
        label = _action_label(action, index)
        adapted = adapt_action(action)
        if adapted is None:
            structured_keys = _unrouted_structured_payload_keys(action)
            if structured_keys:
                violations.append(
                    f"{label} unknown analyst action kind {action.kind} "
                    f"with structured payload keys {structured_keys}"
                )
                continue
            details["unrouted_informational"].append(
                {"label": label, "kind": action.kind}
            )
            continue

        kind, payload = adapted
        details["checked_actions"].append({"label": label, "kind": kind, "payload": payload})
        with tempfile.TemporaryDirectory(prefix="recon-eval-action-") as tmpdir:
            scratch_path = Path(tmpdir) / "scratch.sqlite"
            _sqlite_backup(source_db_path, scratch_path)
            try:
                with engine.write_tx(scratch_path) as conn:
                    proposal_id = repo_actions.enqueue_proposal(
                        conn,
                        kind=kind,
                        payload=payload,
                        evidence=_model_dump(action.evidence),
                        confidence=action.confidence,
                        rationale=action.rationale,
                        agent_run_id=action.agent_run_id,
                    )
                    normalized = repo_actions.get_proposal(conn, proposal_id)
                    normalized_payload = normalized["payload"] if normalized is not None else payload
                    if kind in _IMPLEMENTED_ACTION_KINDS:
                        violations.extend(
                            _exercise_implemented_action(
                                conn,
                                proposal_id=proposal_id,
                                kind=kind,
                                payload=normalized_payload,
                                label=label,
                            )
                        )
                    elif kind in _STUB_ACTION_KINDS:
                        violations.extend(
                            _exercise_stub_action(
                                conn,
                                kind=kind,
                                payload=normalized_payload,
                                label=label,
                            )
                        )
                    else:
                        violations.append(f"{label} adapted to unsupported handler kind {kind}")
            except Exception as exc:
                violations.append(
                    f"{label} {kind} did not enqueue cleanly: {type(exc).__name__}: {exc}"
                )

    return _result(violations, details)


async def _arun_traced_review(
    db_path: str | Path,
    *,
    source_document_id: int | None,
    month: str | None,
    llm: Any,
) -> tuple[Any, list[NodeTrace]]:
    from app.agents.recon_analyst.evidence import resolve_statement_run as live_resolve_statement_run
    from app.agents.recon_analyst.graph import build_reconciliation_graph
    from app.agents.recon_analyst.tools import IdAllowlist as LiveIdAllowlist

    graph = build_reconciliation_graph()
    traces: list[NodeTrace] = []
    review = None
    previous_evidence_keys: set[str] = set()

    with engine.read_conn(db_path) as conn:
        run = live_resolve_statement_run(
            conn,
            source_document_id=source_document_id,
            month=month,
        )
        initial = {
            "conn": conn,
            "llm": llm,
            "agent_run_id": "eval-trace-run",
            "statement_run": run,
            "evidence_bundle": {},
            "allowed_ids": LiveIdAllowlist(),
            "candidate_findings": [],
            "candidate_actions": [],
        }
        async for update in graph.astream(
            initial,
            config={"recursion_limit": 8},
            stream_mode="updates",
        ):
            for node, payload in update.items():
                evidence_bundle = payload.get("evidence_bundle") or {}
                evidence_keys = set(evidence_bundle)
                added_keys = sorted(evidence_keys - previous_evidence_keys)
                evidence_key = added_keys[0] if len(added_keys) == 1 else None
                if evidence_bundle:
                    previous_evidence_keys = evidence_keys

                findings = payload.get("candidate_findings") or []
                actions = payload.get("candidate_actions") or []
                if node == "guard":
                    review = payload["review"]
                    findings = review.findings
                    actions = review.proposed_actions

                traces.append(
                    NodeTrace(
                        node=node,
                        evidence_key=evidence_key,
                        finding_ids=[finding.finding_id for finding in findings],
                        action_ids=[
                            f"{index}:{action.kind}"
                            for index, action in enumerate(actions)
                        ],
                        action_kinds=[action.kind for action in actions],
                        evidence=evidence_bundle.get(evidence_key) if evidence_key else None,
                    )
                )

    if review is None:
        raise RuntimeError("reconciliation graph did not emit a review")
    return review, traces


def run_traced_review(
    db_path: str | Path,
    *,
    source_document_id: int | None = None,
    month: str | None = None,
    llm: Any,
) -> tuple[Any, list[NodeTrace]]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _arun_traced_review(
                db_path,
                source_document_id=source_document_id,
                month=month,
                llm=llm,
            )
        )
    raise RuntimeError("run_traced_review cannot run inside an active event loop")


def _normalize_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _normalize_dump(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize_dump(item) for item in value]
    if isinstance(value, set):
        return sorted(_normalize_dump(item) for item in value)
    return value


def check_trajectory(traces: list[NodeTrace], conn: sqlite3.Connection, run: Any) -> EvalResult:
    expected_order = [
        "statement_auditor",
        "receipt_matcher",
        "recurring_analyst",
        "planning_analyst",
        "synthesizer",
        "guard",
    ]
    expected_evidence = {
        "statement_auditor": ("statement_audit", statement_auditor_tool),
        "receipt_matcher": ("receipt_matches", receipt_matcher_tool),
        "recurring_analyst": ("recurring", recurring_analyst_tool),
        "planning_analyst": ("planning", planning_analyst_tool),
    }
    violations: list[str] = []
    actual_order = [trace.node for trace in traces]
    if actual_order != expected_order:
        violations.append(f"node order expected {expected_order!r} got {actual_order!r}")

    by_node = {trace.node: trace for trace in traces}
    for node, (expected_key, tool) in expected_evidence.items():
        trace = by_node.get(node)
        if trace is None:
            violations.append(f"missing trace for {node}")
            continue
        if trace.evidence_key != expected_key:
            violations.append(
                f"{node} evidence key expected {expected_key!r} got {trace.evidence_key!r}"
            )
            continue
        expected_payload = _normalize_dump(tool(conn, run))
        actual_payload = _normalize_dump(trace.evidence or {})
        if actual_payload != expected_payload:
            violations.append(f"{node} evidence payload did not match independent tool output")

    return _result(
        violations,
        {
            "node_order": actual_order,
            "evidence_keys": {trace.node: trace.evidence_key for trace in traces},
        },
    )
