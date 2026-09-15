"""Fail-closed category planning for receipt promotion.

Trusted local knowledge and model guesses may create review proposals.  Only an
exact approved production policy may turn a resolver result into an automatic
ledger category, and FN-149B intentionally ships with that authority disabled.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..db import repo_ledger, repo_merchant_knowledge
from ..reconcile.automation_policy import decision_for
from ..reconcile.merchant_resolution import resolve_descriptor
from .schemas import ExtractedReceipt


@dataclass(frozen=True)
class ReceiptCategoryPlan:
    ledger_category_id: int
    method: str
    proposed_category_id: int | None = None
    proposal_source: str = ""
    evidence_claim_ids: tuple[int, ...] = ()


def resolve_category(
    conn: sqlite3.Connection,
    receipt: ExtractedReceipt,
    *,
    account_id: int,
) -> ReceiptCategoryPlan:
    """Plan one receipt category without trusting model or legacy state."""
    fallback_id = repo_ledger.ensure_uncategorized(conn)
    scope = repo_merchant_knowledge.scope_for(conn, account_id=account_id)
    resolved = resolve_descriptor(
        conn,
        descriptor=receipt.merchant,
        scope=scope,
    )
    authority = decision_for("expense_category")
    if resolved.category.status == "resolved":
        category_id = int(resolved.category.target_id)
        if authority.allowed:
            return ReceiptCategoryPlan(
                ledger_category_id=category_id,
                method="trusted_automatic",
                evidence_claim_ids=resolved.category.claim_ids,
            )
        return ReceiptCategoryPlan(
            ledger_category_id=fallback_id,
            method="review_trusted_knowledge",
            proposed_category_id=category_id,
            proposal_source="trusted_local_knowledge",
            evidence_claim_ids=resolved.category.claim_ids,
        )

    guess = (receipt.category_guess or "").strip()
    if guess:
        category = repo_ledger.find_category_by_name(conn, guess)
        if (
            category is not None
            and category["kind"] == "expense"
            and category["name"] != "Uncategorized"
        ):
            return ReceiptCategoryPlan(
                ledger_category_id=fallback_id,
                method="review_model_guess",
                proposed_category_id=int(category["id"]),
                proposal_source="model_guess",
            )

    return ReceiptCategoryPlan(
        ledger_category_id=fallback_id,
        method="uncategorized",
    )
