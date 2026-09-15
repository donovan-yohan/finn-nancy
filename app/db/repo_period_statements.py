"""Read-only evidence projection for FN-148 period statements.

The repository deliberately returns complete, untruncated contribution sets.
Aggregation, currency policy, and presentation semantics live in
``app.reporting.period_statements``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from collections import defaultdict
from typing import Any

from . import repo_period_policy


def accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, name, institution, kind, currency, is_active, external_ref
        FROM accounts
        ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def split_rows_through(
    conn: sqlite3.Connection,
    *,
    period_end: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          split.id AS transaction_split_id,
          split.amount_cents AS split_amount_cents,
          category.id AS category_id,
          category.name AS category_name,
          category.kind AS category_kind,
          txn.id AS transaction_id,
          txn.account_id,
          txn.posted_on,
          txn.description,
          txn.counterparty,
          txn.amount_cents AS transaction_amount_cents,
          txn.flow_kind,
          txn.source,
          txn.source_document_id AS transaction_source_document_id,
          txn.recon_status,
          txn.cleared_on,
          flow.semantic_status,
          flow.semantic_reason,
          flow.report_eligible,
          account.name AS account_name,
          account.institution,
          account.kind AS account_kind,
          account.currency AS account_currency,
          account.is_active
        FROM transaction_splits split
        JOIN transactions txn ON txn.id=split.transaction_id
        JOIN categories category ON category.id=split.category_id
        JOIN accounts account ON account.id=txn.account_id
        JOIN v_transaction_flow_status flow ON flow.transaction_id=txn.id
        WHERE txn.posted_on<=?
        ORDER BY
          account.id, txn.posted_on, txn.id, split.id
        """,
        (period_end,),
    ).fetchall()
    return [dict(row) for row in rows]


def split_rows_for_transactions(
    conn: sqlite3.Connection,
    transaction_ids: Iterable[int],
) -> list[dict[str, Any]]:
    """Load exact relationship counterparts, including forward-dated legs."""
    ids = sorted({int(value) for value in transaction_ids})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT
          split.id AS transaction_split_id,
          split.amount_cents AS split_amount_cents,
          category.id AS category_id,
          category.name AS category_name,
          category.kind AS category_kind,
          txn.id AS transaction_id,
          txn.account_id,
          txn.posted_on,
          txn.description,
          txn.counterparty,
          txn.amount_cents AS transaction_amount_cents,
          txn.flow_kind,
          txn.source,
          txn.source_document_id AS transaction_source_document_id,
          txn.recon_status,
          txn.cleared_on,
          flow.semantic_status,
          flow.semantic_reason,
          flow.report_eligible,
          account.name AS account_name,
          account.institution,
          account.kind AS account_kind,
          account.currency AS account_currency,
          account.is_active
        FROM transaction_splits split
        JOIN transactions txn ON txn.id=split.transaction_id
        JOIN categories category ON category.id=split.category_id
        JOIN accounts account ON account.id=txn.account_id
        JOIN v_transaction_flow_status flow ON flow.transaction_id=txn.id
        WHERE txn.id IN ({placeholders})
        ORDER BY
          account.id, txn.posted_on, txn.id, split.id
        """,
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def statement_lines_through(
    conn: sqlite3.Connection,
    *,
    period_end: str,
    include_transaction_ids: Iterable[int] = (),
) -> dict[int, list[dict[str, Any]]]:
    included = sorted({int(value) for value in include_transaction_ids})
    include_sql = ""
    parameters: list[Any] = [period_end]
    if included:
        placeholders = ",".join("?" for _ in included)
        include_sql = f" OR txn.id IN ({placeholders})"
        parameters.extend(included)
    rows = conn.execute(
        f"""
        SELECT
          line.id AS statement_line_id,
          line.matched_transaction_id AS transaction_id,
          line.source_document_id,
          line.account_id,
          line.posted_on,
          line.raw_description,
          line.norm_merchant,
          line.amount_cents,
          line.currency,
          line.statement_period,
          line.match_status,
          line.match_method,
          line.match_score,
          line.match_rationale,
          line.review_disposition,
          line.review_revision,
          line.source_anchor_id,
          document.sha256 AS source_document_sha256
        FROM statement_lines line
        JOIN transactions txn ON txn.id=line.matched_transaction_id
        JOIN source_documents document ON document.id=line.source_document_id
        WHERE (txn.posted_on<=?{include_sql})
        ORDER BY txn.id, line.posted_on, line.id
        """,
        parameters,
    ).fetchall()
    by_transaction: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_transaction[int(row["transaction_id"])].append(dict(row))
    return dict(by_transaction)


def active_resolution_claims_through(
    conn: sqlite3.Connection,
    *,
    period_end: str,
) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT active.*
        FROM v_active_merchant_resolution_claims active
        JOIN transactions txn ON txn.id=active.transaction_id
        WHERE txn.posted_on<=?
        ORDER BY
          active.transaction_id,
          COALESCE(active.transaction_split_id, 0),
          active.claim_kind,
          active.claim_id
        """,
        (period_end,),
    ).fetchall()
    by_transaction: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_transaction[int(row["transaction_id"])].append(dict(row))
    return dict(by_transaction)


def active_relationships_through(
    conn: sqlite3.Connection,
    *,
    period_end: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT relationship.*
        FROM transaction_relationships relationship
        JOIN transactions source
          ON source.id=relationship.source_transaction_id
        JOIN transactions target
          ON target.id=relationship.target_transaction_id
        WHERE relationship.status='active'
          AND (source.posted_on<=? OR target.posted_on<=?)
        ORDER BY relationship.id
        """,
        (period_end, period_end),
    ).fetchall()
    return [dict(row) for row in rows]


def assertions_through(
    conn: sqlite3.Connection,
    *,
    period_end: str,
) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT assertion.*, document.sha256 AS source_document_sha256
        FROM account_balance_assertions assertion
        LEFT JOIN source_documents document
          ON document.id=assertion.source_document_id
        WHERE assertion.asof_date<=?
        ORDER BY assertion.account_id, assertion.asof_date, assertion.id
        """,
        (period_end,),
    ).fetchall()
    by_account: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_account[int(row["account_id"])].append(dict(row))
    return dict(by_account)


def close_evidence(conn: sqlite3.Connection, *, month: str) -> dict[str, Any]:
    state = repo_period_policy.get_state(conn, month)
    history = repo_period_policy.list_snapshot_history(conn, month)
    current_exceptions = repo_period_policy.list_current_exceptions(conn, month)
    current_snapshot = next(
        (dict(row) for row in history if int(row["is_current"]) == 1),
        None,
    )
    return {
        "state": "open" if state is None else str(state["state"]),
        "cycle_id": None if state is None else int(state["cycle_id"]),
        "snapshot_id": (
            None
            if current_snapshot is None
            else int(current_snapshot["snapshot_id"])
        ),
        "snapshot_is_current": current_snapshot is not None,
        "snapshot": current_snapshot,
        "exceptions": current_exceptions,
    }


def load_period_evidence(
    conn: sqlite3.Connection,
    *,
    month: str,
    period_end: str,
) -> dict[str, Any]:
    """Load every row needed to construct one untruncated report."""
    relationships = active_relationships_through(
        conn,
        period_end=period_end,
    )
    transfer_pair_transaction_ids = {
        int(relationship[key])
        for relationship in relationships
        if str(relationship["relationship_kind"]) == "transfer_pair"
        for key in ("source_transaction_id", "target_transaction_id")
    }
    return {
        "accounts": accounts(conn),
        "splits": split_rows_through(conn, period_end=period_end),
        "transfer_pair_splits": split_rows_for_transactions(
            conn,
            transfer_pair_transaction_ids,
        ),
        "statement_lines": statement_lines_through(
            conn,
            period_end=period_end,
            include_transaction_ids=transfer_pair_transaction_ids,
        ),
        "claims": active_resolution_claims_through(
            conn,
            period_end=period_end,
        ),
        "relationships": relationships,
        "assertions": assertions_through(conn, period_end=period_end),
        "close": close_evidence(conn, month=month),
    }
