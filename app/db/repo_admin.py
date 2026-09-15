"""Admin helpers: undo an import (delete a source document + what it produced)."""
from __future__ import annotations

import sqlite3

from . import repo_statement_expectations


def delete_document(
    conn: sqlite3.Connection,
    doc_id: int,
    *,
    actor: str = "system:document-delete",
    reason: str = "document deletion requested",
) -> dict:
    """Undo an import: delete transactions from doc_id (splits cascade), then the doc row
    (ingest_extractions cascade with it). An active statement-expectation link is
    detached and audited first; its closed-period guard therefore applies to this
    destructive path too. The blob on disk is left alone (content-addressed, harmless
    to keep)."""
    doc = conn.execute("SELECT id FROM source_documents WHERE id=?", (doc_id,)).fetchone()
    if doc is None:
        return {"document_id": doc_id, "transactions_deleted": 0, "existed": False}

    repo_statement_expectations.detach_document_source(
        conn,
        doc_id,
        actor=actor,
        reason=reason,
    )
    # Transactions must go first: their FK to source_documents is ON DELETE SET NULL, not
    # CASCADE, so deleting the doc row first would orphan them instead of removing them.
    cur = conn.execute("DELETE FROM transactions WHERE source_document_id=?", (doc_id,))
    n = cur.rowcount
    conn.execute("DELETE FROM source_documents WHERE id=?", (doc_id,))
    return {"document_id": doc_id, "transactions_deleted": n, "existed": True}


def list_documents(conn: sqlite3.Connection, status: str | None = None) -> list:
    """Newest first, optionally filtered by status."""
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM source_documents WHERE status=? ORDER BY created_at DESC, id DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM source_documents ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return list(rows)
