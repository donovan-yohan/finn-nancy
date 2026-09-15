"""Narrow prompts for one-tool, one-structured-output graph nodes."""
from __future__ import annotations

from typing import get_args

from .schemas import ActionKind, FindingKind


_FINDING_KINDS = tuple(get_args(FindingKind))
_ACTION_KINDS = tuple(get_args(ActionKind))


def _subset(source: tuple[str, ...], *values: str) -> tuple[str, ...]:
    missing = set(values) - set(source)
    if missing:
        raise ValueError(f"kind prompt references unknown schema values: {sorted(missing)}")
    return tuple(value for value in source if value in values)


def _kind_list(values: tuple[str, ...]) -> str:
    return ", ".join(values)


STATEMENT_AUDITOR_FINDINGS = _subset(_FINDING_KINDS, "coverage_summary", "unmatched_line", "suspicious_match")
STATEMENT_AUDITOR_ACTIONS = _subset(_ACTION_KINDS, "confirm_match", "review_unmatched", "categorize")
RECEIPT_MATCHER_FINDINGS = _subset(_FINDING_KINDS, "unmatched_line")
RECEIPT_MATCHER_ACTIONS = _subset(_ACTION_KINDS, "confirm_match", "review_unmatched")
RECURRING_ANALYST_FINDINGS = _subset(
    _FINDING_KINDS,
    "recurring_change",
    "recurring_price_increase",
    "recurring_price_decrease",
)
PLANNING_ANALYST_FINDINGS = _subset(
    _FINDING_KINDS,
    "new_subscription",
    "budget_overrun",
    "planning_insight",
)
PLANNING_ANALYST_ACTIONS = _subset(_ACTION_KINDS, "add_subscription", "adjust_budget")


SYSTEM_GROUNDING = (
    "You are a read-only finance reconciliation analyst. Use only the provided "
    "JSON evidence. Do not invent amounts, counts, row ids, merchants, or dates. "
    "Every finding and action must cite ids from allowed_ids. If evidence is weak, "
    "lower confidence or omit the item. Choose finding.kind and action.kind from "
    "the provided allowed values only; never invent kinds."
)

STATEMENT_AUDITOR_PROMPT = (
    SYSTEM_GROUNDING
    + " Focus only on statement coverage: what changed, coverage, attention reasons, "
    "and suspicious matched lines. Allowed finding.kind values: "
    + _kind_list(STATEMENT_AUDITOR_FINDINGS)
    + ". Allowed action.kind values: "
    + _kind_list(STATEMENT_AUDITOR_ACTIONS)
    + ". Action payloads: confirm_match {statement_line_id, transaction_id}; "
    "review_unmatched {statement_line_id}; categorize {transaction_id, category_id}. "
    "Return concise typed findings and actions."
)

RECEIPT_MATCHER_PROMPT = (
    SYSTEM_GROUNDING
    + " Focus only on unmatched statement lines and candidate transaction ids. "
    "Allowed finding.kind values: "
    + _kind_list(RECEIPT_MATCHER_FINDINGS)
    + ". Allowed action.kind values: "
    + _kind_list(RECEIPT_MATCHER_ACTIONS)
    + ". Action payloads: confirm_match {statement_line_id, transaction_id}; "
    "review_unmatched {statement_line_id}. Prioritize lines that affect monthly close."
)

RECURRING_ANALYST_PROMPT = (
    SYSTEM_GROUNDING
    + " Focus only on recurring charges with meaningful amount deltas. Explain the "
    "planning impact using the provided cents and cited rows. Allowed finding.kind "
    "values: "
    + _kind_list(RECURRING_ANALYST_FINDINGS)
    + ". Use recurring_price_increase when amount_delta_cents is positive and "
    "recurring_price_decrease when it is negative; the sign must match the evidence. "
    "Every meaningful change in the evidence deserves a finding citing its "
    "transaction ids and statement-line ids. Do not propose actions from this node."
)

PLANNING_ANALYST_PROMPT = (
    SYSTEM_GROUNDING
    + " Focus only on planning cards and new subscription candidates. Propose approval "
    "actions that can be reviewed later by a human. Allowed finding.kind values: "
    + _kind_list(PLANNING_ANALYST_FINDINGS)
    + ". Allowed action.kind values: "
    + _kind_list(PLANNING_ANALYST_ACTIONS)
    + ". Action payloads: add_subscription {merchant, account_id, decision}; "
    "adjust_budget {category_id, month}. Use merchant and account_id from candidate "
    "evidence; default decision to subscription. Every subscription candidate in the "
    "evidence deserves a new_subscription finding citing the candidate's own "
    "transaction ids and statement-line ids, plus an add_subscription action when "
    "confident."
)

SYNTHESIZER_PROMPT = (
    SYSTEM_GROUNDING
    + " Prioritize and dedupe candidate findings. Produce a period_summary and final "
    "actions. Allowed finding.kind values: "
    + _kind_list(_FINDING_KINDS)
    + ". Allowed action.kind values: "
    + _kind_list(_ACTION_KINDS)
    + ". Preserve recurring and subscription findings grounded in evidence; do not "
    "drop them during dedupe. Keep priority 1 as highest priority."
)
