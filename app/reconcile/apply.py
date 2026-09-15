"""Manual reconcile actions (recon UI / future CLI). Pure conn-level, no locking here —
callers wrap these in engine.write_tx()."""
from __future__ import annotations

import sqlite3

from ..db import (
    repo_documents,
    repo_merchant_knowledge,
    repo_statement_expectations,
    repo_statements,
)
from ..db.repo_merchant_knowledge import Evidence
from .descriptor_normalization import DescriptorNormalizationError
from .engine import promote_from_line


def _get_line(conn: sqlite3.Connection, line_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT * FROM statement_lines
           WHERE id=? AND review_disposition='active'""",
        (line_id,),
    ).fetchone()
    if row is None:
        raise ValueError("statement line is unavailable for reconciliation")
    return row


def _sync_line_expectation(
    conn: sqlite3.Connection, line: sqlite3.Row, *, reason: str
) -> None:
    remaining = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM statement_lines
            WHERE source_document_id=?
              AND review_disposition='active'
              AND match_status IN ('unmatched', 'needs_review')
            """,
            (int(line["source_document_id"]),),
        ).fetchone()[0]
    )
    repo_documents.set_status(
        conn,
        int(line["source_document_id"]),
        "needs_review" if remaining else "matched",
    )
    repo_statement_expectations.sync_document_reconciliation(
        conn,
        int(line["source_document_id"]),
        actor="reconcile:operator",
        reason=reason,
    )


def _latest_manual_match_event_id(
    conn: sqlite3.Connection,
    *,
    line_id: int,
    transaction_id: int,
) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(id), 0)
        FROM merchant_resolution_events
        WHERE provenance_kind='manual_match'
          AND statement_line_id=?
          AND transaction_id=?
        """,
        (line_id, transaction_id),
    ).fetchone()
    return int(row[0])


def confirm_match(conn: sqlite3.Connection, line_id: int, transaction_id: int) -> None:
    """Manually pair a line and transaction and record merchant evidence only."""
    line = _get_line(conn, line_id)
    txn = conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
    already_claimed = conn.execute(
        """SELECT 1 FROM statement_lines
           WHERE matched_transaction_id=? AND review_disposition='active'""",
        (transaction_id,),
    ).fetchone()
    if txn is None or txn["recon_status"] != "uncleared" or already_claimed is not None:
        raise ValueError("transaction already reconciled")
    repo_statements.set_match(conn, line_id, status="matched", method="manual",
                              transaction_id=transaction_id, score=1.0, rationale="manual confirm")
    repo_statements.mark_cleared(conn, transaction_id, line["posted_on"], account_id=line["account_id"])

    canonical_name = str(
        txn["counterparty"] or txn["description"] or ""
    ).strip()
    if canonical_name and str(line["raw_description"] or "").strip():
        try:
            scope = repo_merchant_knowledge.scope_for_statement_line(
                conn,
                line_id,
            )
            repo_merchant_knowledge.confirm_merchant(
                conn,
                descriptor=str(line["raw_description"]),
                canonical_name=canonical_name,
                scope=scope,
                operation_key=(
                    f"manual-match:{line_id}:r{int(line['review_revision'])}:"
                    f"{transaction_id}:after-event:"
                    f"{_latest_manual_match_event_id(conn, line_id=line_id, transaction_id=transaction_id)}:"
                    "merchant"
                ),
                actor="operator:reconciliation",
                reason="operator confirmed statement merchant identity",
                evidence=Evidence(
                    statement_line_id=line_id,
                    transaction_id=transaction_id,
                    source_anchor_id=line["source_anchor_id"],
                ),
                provenance_kind="manual_match",
                provenance_ref=f"statement-line:{line_id}",
            )
        except DescriptorNormalizationError:
            pass
    _sync_line_expectation(
        conn,
        line,
        reason="operator confirmed a statement-line match",
    )


def promote_line(conn: sqlite3.Connection, line_id: int) -> int | None:
    line = _get_line(conn, line_id)
    transaction_id = promote_from_line(conn, line)
    _sync_line_expectation(
        conn,
        line,
        reason="operator promoted a statement line",
    )
    return transaction_id


def ignore_line(conn: sqlite3.Connection, line_id: int) -> None:
    line = _get_line(conn, line_id)
    repo_statements.set_match(conn, line_id, status="ignored", rationale="manually ignored")
    _sync_line_expectation(
        conn,
        line,
        reason="operator ignored a statement line",
    )


def _undo_manual_match_knowledge(
    conn: sqlite3.Connection,
    *,
    source_document_id: int,
    line_id: int,
    transaction_id: int,
) -> None:
    claims = conn.execute(
        """
        SELECT claim_id, acceptance_event_id
        FROM v_active_merchant_resolution_claims
        WHERE claim_kind='canonical_merchant'
          AND provenance_kind='manual_match'
          AND statement_line_id=?
          AND transaction_id=?
        ORDER BY claim_id
        """,
        (line_id, transaction_id),
    ).fetchall()
    for claim in claims:
        repo_merchant_knowledge.undo_claim(
            conn,
            claim_id=int(claim["claim_id"]),
            operation_key=(
                f"unreconcile:{source_document_id}:line:{line_id}:"
                f"transaction:{transaction_id}:claim:{int(claim['claim_id'])}:"
                f"acceptance:{int(claim['acceptance_event_id'])}:undo"
            ),
            actor="operator:reconciliation",
            reason="operator unreconciled the supporting statement-line match",
        )


def unreconcile_document(conn: sqlite3.Connection, source_document_id: int) -> dict:
    """Undo a reconcile run without erasing later accounting decisions.

    Disposable promoted rows are removed as before.  A promoted transaction that
    gained flow audit or relationship evidence is instead detached and made
    uncleared, so a re-run can match it again and append-only evidence never
    becomes an orphan.  Every statement line is retained and reset for recovery.
    """
    repo_statement_expectations.unreconcile_document(
        conn,
        source_document_id,
        actor="reconcile:operator",
        reason="operator unreconciled statement document",
    )
    lines = repo_statements.lines_for_document(conn, source_document_id)
    reset = removed = 0
    for line in lines:
        if line["match_status"] == "matched" and line["matched_transaction_id"] is not None:
            transaction_id = int(line["matched_transaction_id"])
            _undo_manual_match_knowledge(
                conn,
                source_document_id=source_document_id,
                line_id=int(line["id"]),
                transaction_id=transaction_id,
            )
            conn.execute(
                "UPDATE transactions SET recon_status='uncleared', cleared_on='' WHERE id=?",
                (transaction_id,),
            )
            reset += 1
        elif line["match_status"] == "promoted" and line["matched_transaction_id"] is not None:
            transaction_id = int(line["matched_transaction_id"])
            audited = conn.execute(
                """
                SELECT
                  EXISTS (
                    SELECT 1 FROM positive_flow_decision_events
                    WHERE subject_transaction_id=?
                       OR candidate_transaction_id=?
                  )
                  OR EXISTS (
                    SELECT 1 FROM transaction_relationships
                    WHERE source_transaction_id=?
                       OR target_transaction_id=?
                  )
                  OR EXISTS (
                    SELECT 1 FROM transaction_flow_audit
                    WHERE transaction_id=?
                  )
                  OR EXISTS (
                    SELECT 1 FROM merchant_resolution_events
                    WHERE transaction_id=?
                  )
                """,
                (
                    transaction_id,
                    transaction_id,
                    transaction_id,
                    transaction_id,
                    transaction_id,
                    transaction_id,
                ),
            ).fetchone()[0]
            if audited:
                conn.execute(
                    """UPDATE transactions
                       SET recon_status='uncleared', cleared_on=''
                       WHERE id=?""",
                    (transaction_id,),
                )
                reset += 1
            else:
                conn.execute(
                    "DELETE FROM transactions WHERE id=?", (transaction_id,)
                )
                removed += 1
        repo_statements.set_match(conn, line["id"], status="unmatched")

    repo_documents.set_status(conn, source_document_id, "processed")
    return {"lines": len(lines), "reset": reset, "removed": removed}
