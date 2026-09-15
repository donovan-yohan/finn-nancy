"""RAG chunk maintenance and FTS search over approved ledger rows.

The approved set is exactly the ``transactions`` ledger table. Staged receipt
extractions and statement lines are not chunked directly; receipt line items are
included only when an extraction is linked to an approved transaction. The SQL
view ``v_rag_transaction_chunks`` is the single deterministic content builder
used by migration backfill, triggers, and the repair function below.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

MAX_SEARCH_LIMIT = 25


def transaction_chunk(conn: sqlite3.Connection, transaction_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM v_rag_transaction_chunks
        WHERE ref_id = ?
        """,
        (transaction_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def rebuild_rag_chunks(conn: sqlite3.Connection, transaction_id: int | None = None) -> int:
    """Repair RAG chunks from the canonical SQL builder view."""
    if transaction_id is None:
        conn.execute("DELETE FROM rag_chunks")
        conn.execute(
            """
            INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
            SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
            FROM v_rag_transaction_chunks
            """
        )
        row = conn.execute("SELECT COUNT(*) AS count FROM rag_chunks").fetchone()
        return int(row["count"])

    conn.execute("DELETE FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = ?", (transaction_id,))
    conn.execute(
        """
        INSERT INTO rag_chunks(ref_kind, ref_id, posted_on, category_id, amount_cents, content)
        SELECT ref_kind, ref_id, posted_on, category_id, amount_cents, content
        FROM v_rag_transaction_chunks
        WHERE ref_id = ?
        """,
        (transaction_id,),
    )
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM rag_chunks WHERE ref_kind = 'transaction' AND ref_id = ?",
        (transaction_id,),
    ).fetchone()
    return int(row["count"])


def _fts_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for char in query:
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def fts_query(query: str) -> str:
    """Return a safely quoted FTS5 AND query, or ``""`` when there are no terms."""
    tokens = _fts_tokens(query)
    return " ".join(f'"{token.replace(chr(34), chr(34) + chr(34))}"' for token in tokens)


def search_history(conn: sqlite3.Connection, query: str, limit: int = 8) -> dict[str, Any]:
    limit = max(1, min(int(limit or 8), MAX_SEARCH_LIMIT))
    match = fts_query(query)
    if not match:
        return {"query": query, "message": "No searchable terms found.", "rows": []}
    try:
        rows = conn.execute(
            """
            SELECT
              rc.ref_kind,
              rc.ref_id AS transaction_id,
              rc.posted_on,
              rc.category_id,
              rc.amount_cents,
              snippet(rag_fts, 0, '[', ']', '...', 16) AS snippet,
              bm25(rag_fts) AS rank
            FROM rag_fts
            JOIN rag_chunks rc ON rc.id = rag_fts.rowid
            WHERE rag_fts MATCH ?
            ORDER BY bm25(rag_fts), rc.posted_on DESC, rc.id DESC
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"query": query, "message": "Search terms could not be parsed.", "rows": []}
    return {"query": query, "match": match, "rows": [dict(row) for row in rows]}
