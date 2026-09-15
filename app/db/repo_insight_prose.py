"""Cached LLM prose for monthly insight summaries."""
from __future__ import annotations

import sqlite3


def get(
    conn: sqlite3.Connection,
    period_month: str,
    scope: str = "household",
    kind: str = "monthly_summary",
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM insight_prose
        WHERE period_month = ?
          AND scope = ?
          AND kind = ?
        """,
        (period_month, scope, kind),
    ).fetchone()


def upsert(
    conn: sqlite3.Connection,
    *,
    period_month: str,
    scope: str,
    kind: str,
    body: str,
    model: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO insight_prose(period_month, scope, kind, body, model)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(period_month, scope, kind) DO UPDATE SET
          body = excluded.body,
          model = excluded.model,
          created_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (period_month, scope, kind, body, model),
    )
    return int(cur.fetchone()["id"])
