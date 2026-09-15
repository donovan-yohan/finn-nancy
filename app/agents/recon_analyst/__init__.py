"""Deep-agent-compatible reconciliation analyst prototype.

The final graph guard grounds row-id citations and vetted payload fields. It
does not numerically verify narrative prose in summaries, finding details, or
action rationales. LLM generation pins kind values via LLMReviewFinding and
LLMProposedAction; the core schemas stay permissive so the guard can still
handle unknown kinds defensively.
"""
from __future__ import annotations

from .analyst import areview_reconciliation, review_reconciliation
from .schemas import (
    ActionKind,
    Evidence,
    FindingKind,
    LLMProposedAction,
    LLMReviewFinding,
    ProposedAction,
    ReconciliationReview,
    ReviewFinding,
)

__all__ = [
    "ActionKind",
    "Evidence",
    "FindingKind",
    "LLMProposedAction",
    "LLMReviewFinding",
    "ProposedAction",
    "ReconciliationReview",
    "ReviewFinding",
    "areview_reconciliation",
    "review_reconciliation",
]
