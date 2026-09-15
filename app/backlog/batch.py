"""Batch backlog suggestion job handler."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..db import engine, repo_actions
from .suggest import (
    Suggestion,
    get_backlog_transaction,
    list_uncategorized_expense_backlog,
    suggest_category,
)

def _proposal_transaction_id(proposal: dict[str, Any]) -> int | None:
    try:
        return int((proposal.get("payload") or {}).get("transaction_id"))
    except (TypeError, ValueError):
        return None


def has_active_recategorization_proposal(conn, transaction_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM proposed_actions
        WHERE kind='recategorization'
          AND status IN ('proposed', 'needs_evidence', 'snoozed')
          AND CAST(json_extract(payload_json, '$.transaction_id') AS INTEGER)=?
        LIMIT 1
        """,
        (int(transaction_id),),
    ).fetchone()
    return row is not None


def active_recategorization_txn_ids(conn) -> set[int]:
    rows = conn.execute(
        """
        SELECT json_extract(payload_json, '$.transaction_id') AS transaction_id
        FROM proposed_actions
        WHERE kind='recategorization'
          AND status IN ('proposed', 'needs_evidence', 'snoozed')
        """
    ).fetchall()
    transaction_ids: set[int] = set()
    for row in rows:
        try:
            transaction_ids.add(int(row["transaction_id"]))
        except (TypeError, ValueError):
            continue
    return transaction_ids


def _transaction_evidence(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "transaction_split_id": int(row["transaction_split_id"]),
        "posted_on": row["posted_on"],
        "merchant": row["merchant"],
        "counterparty": row["counterparty"],
        "description": row["description"],
        "amount_cents": int(row["amount_cents"]),
        "split_amount_cents": int(row["split_amount_cents"]),
        "source": row["source"],
        "recon_status": row["recon_status"],
        "resolution_status": row["resolution_status"],
        "categories": [
            {
                "category_id": int(row["category_id"]),
                "name": row["category_name"],
                "kind": row["category_kind"],
            }
        ],
    }


def _neighbor_ids(suggestion: Suggestion) -> list[int]:
    ids: list[int] = []
    for neighbor in suggestion.neighbors:
        try:
            ids.append(int(neighbor["transaction_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def _proposal_evidence(row: Any, suggestion: Suggestion) -> dict[str, Any]:
    neighbor_ids = _neighbor_ids(suggestion)
    return {
        "transaction_ids": [suggestion.transaction_id],
        "category_ids": [int(suggestion.suggested_category_id or 0)],
        "similar_transaction_ids": neighbor_ids,
        "neighbors": suggestion.neighbors,
        "vote_breakdown": suggestion.vote_breakdown,
        "method": suggestion.method,
        "knowledge_claim_ids": list(suggestion.knowledge_claim_ids),
        "knowledge_version": suggestion.knowledge_version,
        "transaction": _transaction_evidence(row),
        "deciding_signal": {
            "method": suggestion.method,
            "confidence": suggestion.confidence,
            "explanation": suggestion.rationale,
        },
    }


def _valid_target(conn, suggestion: Suggestion) -> bool:
    if suggestion.suggested_category_id is None:
        return False
    row = conn.execute(
        "SELECT name, kind FROM categories WHERE id=?",
        (int(suggestion.suggested_category_id),),
    ).fetchone()
    return row is not None and row["kind"] == "expense" and row["name"] != "Uncategorized"


def run_batch(
    db_path: str | Path,
    *,
    batch: str,
    limit: int,
    llm,
    include_closed: bool = False,
) -> dict[str, int]:
    """Queue proposals for the largest expenses lacking accepted category evidence.

    Cleared expenses remain eligible because reconciliation and category
    evidence are independent. Closed-month transactions are skipped unless
    ``include_closed`` is set (see
    :func:`app.backlog.suggest.list_uncategorized_expense_backlog`).
    """
    summary = {
        "suggested": 0,
        "skipped_existing": 0,
        "no_suggestion": 0,
        "scanned": 0,
        "errors": 0,
    }
    batch = (batch or "default").strip() or "default"
    limit = max(1, int(limit or 1))

    with engine.read_conn(db_path) as conn:
        candidates = list_uncategorized_expense_backlog(
            conn,
            limit=limit,
            include_closed=include_closed,
            mutation_safe_only=True,
        )
        active_transaction_ids = active_recategorization_txn_ids(conn)

    for candidate in candidates:
        transaction_id = int(candidate["id"])
        summary["scanned"] += 1
        try:
            if transaction_id in active_transaction_ids:
                summary["skipped_existing"] += 1
                continue
            with engine.read_conn(db_path) as conn:
                txn = get_backlog_transaction(conn, transaction_id)
                if txn is None:
                    summary["no_suggestion"] += 1
                    continue
                suggestion = suggest_category(conn, txn, k=8, llm=llm)
                if suggestion.method == "none" or not _valid_target(conn, suggestion):
                    summary["no_suggestion"] += 1
                    continue

            with engine.write_tx(db_path) as conn:
                if has_active_recategorization_proposal(conn, transaction_id):
                    summary["skipped_existing"] += 1
                    continue
                repo_actions.enqueue_proposal(
                    conn,
                    kind="recategorization",
                    payload={
                        "transaction_id": suggestion.transaction_id,
                        "to_category_id": int(suggestion.suggested_category_id),
                    },
                    evidence=_proposal_evidence(txn, suggestion),
                    confidence=suggestion.confidence,
                    rationale=suggestion.rationale,
                    agent_run_id=f"backlog:{batch}",
                )
                summary["suggested"] += 1
        except Exception:  # noqa: BLE001 - one bad transaction must not abort the batch
            summary["errors"] += 1
            summary["no_suggestion"] += 1
    return summary


def active_backlog_proposals_by_transaction(conn, transaction_ids: list[int]) -> dict[int, dict]:
    """Return active backlog recategorization proposals keyed by transaction id."""
    wanted = {int(transaction_id) for transaction_id in transaction_ids}
    if not wanted:
        return {}
    proposals = repo_actions.list_proposals(
        conn,
        statuses=("proposed", "needs_evidence", "snoozed"),
    )
    out: dict[int, dict] = {}
    for proposal in proposals:
        if proposal["kind"] != "recategorization":
            continue
        if not str(proposal.get("agent_run_id") or "").startswith("backlog:"):
            continue
        transaction_id = _proposal_transaction_id(proposal)
        if transaction_id is None or transaction_id not in wanted or transaction_id in out:
            continue
        target_id = proposal.get("payload", {}).get("to_category_id")
        category = None
        try:
            category = conn.execute(
                "SELECT name FROM categories WHERE id=?",
                (int(target_id),),
            ).fetchone()
        except (TypeError, ValueError):
            category = None
        proposal["to_category_name"] = category["name"] if category is not None else ""
        out[transaction_id] = proposal
    return out
