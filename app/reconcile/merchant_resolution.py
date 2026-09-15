"""Read-only scoped merchant and expense-category resolver."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from ..db import repo_merchant_knowledge
from ..db.repo_merchant_knowledge import MerchantScope
from .automation_policy import decision_for
from .descriptor_normalization import (
    DescriptorNormalizationError,
    NORMALIZATION_VERSION,
    normalize_descriptor_v2,
)

ResolutionStatus = Literal["resolved", "abstained", "no_evidence"]


@dataclass(frozen=True)
class ClaimResolution:
    claim_kind: Literal["canonical_merchant", "expense_category"]
    status: ResolutionStatus
    reason: str
    target_id: int | None = None
    target_name: str = ""
    claim_ids: tuple[int, ...] = ()
    scope_fingerprint: str = ""
    trust_state: str = ""
    automatic_assignment_allowed: bool = False


@dataclass(frozen=True)
class DescriptorResolution:
    normalization_version: str
    descriptor_fingerprint: str
    normalized_tokens: tuple[str, ...]
    merchant: ClaimResolution
    category: ClaimResolution
    knowledge_version: str


def _scope_tuple(row: dict[str, object]) -> tuple[object, ...]:
    return (
        int(row["account_id"]) if row["account_id"] is not None else None,
        str(row["provider_identity_hash"] or ""),
        str(row["processor_family"] or ""),
        str(row["region"] or ""),
    )


def _dominates(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    """True when left is strictly narrower than right in every scope dimension."""
    narrower = False
    for left_value, right_value in zip(left, right, strict=True):
        left_empty = left_value in (None, "")
        right_empty = right_value in (None, "")
        if right_empty:
            if not left_empty:
                narrower = True
            continue
        if left_value != right_value:
            return False
    return narrower


def _resolve_claim_kind(
    rows: list[dict[str, object]],
    *,
    claim_kind: Literal["canonical_merchant", "expense_category"],
) -> ClaimResolution:
    assignment_kind = (
        "canonical_merchant"
        if claim_kind == "canonical_merchant"
        else "expense_category"
    )
    authority = decision_for(assignment_kind)
    if not rows:
        return ClaimResolution(
            claim_kind=claim_kind,
            status="no_evidence",
            reason="no applicable trusted claim or active rejection",
            automatic_assignment_allowed=authority.allowed,
        )

    groups: dict[str, list[dict[str, object]]] = {}
    group_scopes: dict[str, tuple[object, ...]] = {}
    for row in rows:
        fingerprint = str(row["scope_fingerprint"])
        groups.setdefault(fingerprint, []).append(row)
        group_scopes[fingerprint] = _scope_tuple(row)

    maximal = [
        fingerprint
        for fingerprint, scope in group_scopes.items()
        if not any(
            other_fingerprint != fingerprint
            and _dominates(other_scope, scope)
            for other_fingerprint, other_scope in group_scopes.items()
        )
    ]
    if len(maximal) != 1:
        return ClaimResolution(
            claim_kind=claim_kind,
            status="abstained",
            reason="applicable claim scopes are incomparable",
            claim_ids=tuple(
                sorted(
                    int(row["claim_id"])
                    for fingerprint in maximal
                    for row in groups[fingerprint]
                )
            ),
            automatic_assignment_allowed=authority.allowed,
        )

    scope_fingerprint = maximal[0]
    selected = groups[scope_fingerprint]
    if any(row["event_kind"] == "rejected" for row in selected):
        return ClaimResolution(
            claim_kind=claim_kind,
            status="abstained",
            reason="the most-specific applicable scope contains an active rejection",
            claim_ids=tuple(sorted(int(row["claim_id"]) for row in selected)),
            scope_fingerprint=scope_fingerprint,
            trust_state="rejected",
            automatic_assignment_allowed=authority.allowed,
        )

    accepted = [
        row
        for row in selected
        if row["event_kind"] in {"accepted", "corrected"}
        and row["trust_state"] == "human_confirmed"
    ]
    if claim_kind == "canonical_merchant":
        target_rows: dict[int, list[dict[str, object]]] = {}
        for row in accepted:
            target_rows.setdefault(int(row["merchant_entity_id"]), []).append(row)
        target_name_field = "canonical_name"
    else:
        target_rows = {}
        for row in accepted:
            target_rows.setdefault(int(row["category_id"]), []).append(row)
        target_name_field = "category_name"

    if not target_rows:
        return ClaimResolution(
            claim_kind=claim_kind,
            status="no_evidence",
            reason="no trusted claim remains in the most-specific scope",
            scope_fingerprint=scope_fingerprint,
            automatic_assignment_allowed=authority.allowed,
        )
    if len(target_rows) != 1:
        return ClaimResolution(
            claim_kind=claim_kind,
            status="abstained",
            reason="conflicting trusted claims exist at equal scope",
            claim_ids=tuple(sorted(int(row["claim_id"]) for row in accepted)),
            scope_fingerprint=scope_fingerprint,
            trust_state="human_confirmed",
            automatic_assignment_allowed=authority.allowed,
        )

    target_id, evidence_rows = next(iter(target_rows.items()))
    return ClaimResolution(
        claim_kind=claim_kind,
        status="resolved",
        reason="one trusted target exists at the unique most-specific scope",
        target_id=target_id,
        target_name=str(evidence_rows[0][target_name_field]),
        claim_ids=tuple(sorted(int(row["claim_id"]) for row in evidence_rows)),
        scope_fingerprint=scope_fingerprint,
        trust_state="human_confirmed",
        automatic_assignment_allowed=authority.allowed,
    )


def resolve_descriptor(
    conn: sqlite3.Connection,
    *,
    descriptor: str,
    scope: MerchantScope,
) -> DescriptorResolution:
    knowledge_version = repo_merchant_knowledge.knowledge_digest(conn)
    try:
        normalized = normalize_descriptor_v2(descriptor)
    except DescriptorNormalizationError as exc:
        merchant = ClaimResolution(
            claim_kind="canonical_merchant",
            status="abstained",
            reason=str(exc),
        )
        category = ClaimResolution(
            claim_kind="expense_category",
            status="abstained",
            reason=str(exc),
        )
        return DescriptorResolution(
            normalization_version=NORMALIZATION_VERSION,
            descriptor_fingerprint="",
            normalized_tokens=(),
            merchant=merchant,
            category=category,
            knowledge_version=knowledge_version,
        )

    merchant_rows = repo_merchant_knowledge.current_claims_for_descriptor(
        conn,
        descriptor=descriptor,
        scope=scope,
        claim_kind="canonical_merchant",
    )
    category_rows = repo_merchant_knowledge.current_claims_for_descriptor(
        conn,
        descriptor=descriptor,
        scope=scope,
        claim_kind="expense_category",
    )
    return DescriptorResolution(
        normalization_version=normalized.version,
        descriptor_fingerprint=normalized.fingerprint,
        normalized_tokens=normalized.tokens,
        merchant=_resolve_claim_kind(
            merchant_rows,
            claim_kind="canonical_merchant",
        ),
        category=_resolve_claim_kind(
            category_rows,
            claim_kind="expense_category",
        ),
        knowledge_version=knowledge_version,
    )
