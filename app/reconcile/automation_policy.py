"""Fail-closed automation authority for reconciliation and classification."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AssignmentKind = Literal[
    "same_event",
    "canonical_merchant",
    "expense_category",
]

POLICY_VERSION = "fn149b-automation-policy.v1"
AUTHORITY_MODE = "disabled_pending_production_evidence"
_SUPPORTED_KINDS: tuple[AssignmentKind, ...] = (
    "same_event",
    "canonical_merchant",
    "expense_category",
)


@dataclass(frozen=True)
class AutomationDecision:
    assignment_kind: AssignmentKind
    allowed: bool
    policy_version: str
    authority_mode: str
    reason: str


class AutomationAuthorityDisabled(RuntimeError):
    """Raised when a caller attempts a ledger-changing automatic assignment."""


def decision_for(assignment_kind: AssignmentKind) -> AutomationDecision:
    if assignment_kind not in _SUPPORTED_KINDS:
        raise ValueError(f"unsupported automation assignment kind: {assignment_kind}")
    return AutomationDecision(
        assignment_kind=assignment_kind,
        allowed=False,
        policy_version=POLICY_VERSION,
        authority_mode=AUTHORITY_MODE,
        reason=(
            "no approved production scorer/corpus/knowledge/policy tuple grants "
            f"{assignment_kind} authority"
        ),
    )


def require_automatic_assignment(assignment_kind: AssignmentKind) -> None:
    decision = decision_for(assignment_kind)
    if not decision.allowed:
        raise AutomationAuthorityDisabled(decision.reason)


def authority_manifest() -> dict[str, object]:
    return {
        "policy_version": POLICY_VERSION,
        "authority_mode": AUTHORITY_MODE,
        "assignments": {
            kind: decision_for(kind).allowed for kind in _SUPPORTED_KINDS
        },
    }
