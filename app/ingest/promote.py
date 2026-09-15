"""Turn an extracted receipt into a signed ledger transaction (+ split).

Sign convention: receipts are expenses -> negative amount. Reports read splits, so
we always write a correctly-signed split. Idempotent via UNIQUE(source, external_id).
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from ..accounting.contract import FlowKind, currency_review_reason
from ..config import get_settings
from ..db import (
    repo_actions,
    repo_documents,
    repo_embeddings,
    repo_ledger,
    repo_merchant_knowledge,
)
from ..db.repo_merchant_knowledge import Evidence
from ..reconcile.descriptor_normalization import DescriptorNormalizationError
from .classify import resolve_category
from .schemas import ExtractedReceipt


def _valid_date(value: str) -> str:
    try:
        return dt.date.fromisoformat((value or "").strip()).isoformat()
    except ValueError:
        return dt.date.today().isoformat()


def promote_receipt(conn: sqlite3.Connection, *, source_document_id: int, sha256: str,
                    receipt: ExtractedReceipt, extraction_id: int,
                    account_id: int | None = None,
                    human_review: bool = False) -> dict:
    settings = get_settings()
    currency_reason = currency_review_reason(receipt.currency, settings.home_currency)
    if currency_reason is not None:
        raise ValueError(f"{currency_reason}: receipt promotion blocked")
    external_id = "rcpt:" + sha256[:16]
    amount = -abs(int(receipt.total_cents or 0))  # expense -> negative
    account_id = (
        int(account_id)
        if account_id is not None
        else repo_ledger.ensure_default_account(conn, receipt.card_last4 or None)
    )
    account = conn.execute("SELECT currency FROM accounts WHERE id=?", (account_id,)).fetchone()
    account_currency_reason = currency_review_reason(
        account["currency"] if account is not None else None,
        settings.home_currency,
    )
    if account_currency_reason is not None:
        raise ValueError(f"{account_currency_reason}: receipt account promotion blocked")
    category_plan = resolve_category(conn, receipt, account_id=account_id)

    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=_valid_date(receipt.purchased_on),
        description=(receipt.merchant or "receipt"),
        counterparty=(receipt.merchant or ""),
        amount_cents=amount,
        source="receipt",
        external_id=external_id,
        source_document_id=source_document_id,
        source_confidence=float(receipt.confidence or 0.0),
        flow_kind=FlowKind.PURCHASE,
    )
    if txn_id is None:
        return {"status": "duplicate", "external_id": external_id}

    split_id = repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_plan.ledger_category_id,
        amount_cents=amount,
        memo=(receipt.merchant or ""),
    )
    repo_documents.link_extraction_txn(conn, extraction_id, txn_id, review_status="auto")

    proposal_id = None
    proposal_claim_id = None
    if (
        not human_review
        and category_plan.proposed_category_id is not None
    ):
        scope = repo_merchant_knowledge.scope_for(conn, account_id=account_id)
        actor_kind = (
            "model"
            if category_plan.proposal_source == "model_guess"
            else "system"
        )
        try:
            proposal_claim_id = repo_merchant_knowledge.propose_category(
                conn,
                descriptor=receipt.merchant,
                category_id=category_plan.proposed_category_id,
                scope=scope,
                operation_key=(
                    f"receipt:{txn_id}:category-proposal:"
                    f"{category_plan.proposed_category_id}"
                ),
                actor_kind=actor_kind,
                actor=(
                    "model:receipt-extractor"
                    if actor_kind == "model"
                    else "system:merchant-resolution"
                ),
                reason="receipt category requires human review",
                evidence=Evidence(
                    transaction_id=txn_id,
                    transaction_split_id=split_id,
                ),
                provenance_ref=f"extraction:{extraction_id}",
            )
        except DescriptorNormalizationError:
            proposal_claim_id = None
        proposal_id = repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={
                "transaction_id": txn_id,
                "to_category_id": category_plan.proposed_category_id,
            },
            evidence={
                "merchant_resolution_claim_id": proposal_claim_id,
                "trusted_claim_ids": list(category_plan.evidence_claim_ids),
                "source": category_plan.proposal_source,
            },
            confidence=(
                1.0
                if category_plan.proposal_source == "trusted_local_knowledge"
                else float(receipt.confidence or 0.0)
            ),
            rationale="Category suggestion requires operator approval.",
            agent_run_id=(
                "merchant-resolution"
                if category_plan.proposal_source == "trusted_local_knowledge"
                else "receipt-extractor"
            ),
        )

    repo_embeddings.enqueue_embed_transactions(conn)
    return {
        "status": "inserted",
        "transaction_id": txn_id,
        "split_id": split_id,
        "category_id": category_plan.ledger_category_id,
        "how": category_plan.method,
        "proposal_id": proposal_id,
        "proposal_claim_id": proposal_claim_id,
    }
