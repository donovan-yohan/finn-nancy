"""Gold classification labels seeded by approved recategorizations."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def _json_dump_ids(ids: list[int] | tuple[int, ...] | None) -> str:
    values: list[int] = []
    for raw in ids or []:
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            continue
    return json.dumps(values, separators=(",", ":"))


def _json_load_ids(raw: str | None) -> list[int]:
    if raw in (None, ""):
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def record_label(
    conn: sqlite3.Connection,
    *,
    transaction_id: int,
    category_id: int,
    category_name: str,
    merchant: str,
    description: str,
    amount_cents: int,
    neighbor_ids: list[int] | tuple[int, ...] | None = None,
    confidence: float = 0.0,
    source: str = "",
    proposed_action_id: int | None = None,
) -> int:
    """Upsert a confirmed transaction->category label."""
    conn.execute(
        """
        INSERT INTO classification_labels(
          transaction_id, category_id, category_name, merchant, description,
          amount_cents, neighbor_ids_json, confidence, source, proposed_action_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(transaction_id) DO UPDATE SET
          category_id=excluded.category_id,
          category_name=excluded.category_name,
          merchant=excluded.merchant,
          description=excluded.description,
          amount_cents=excluded.amount_cents,
          neighbor_ids_json=excluded.neighbor_ids_json,
          confidence=excluded.confidence,
          source=excluded.source,
          proposed_action_id=excluded.proposed_action_id,
          created_at=CURRENT_TIMESTAMP
        """,
        (
            int(transaction_id),
            int(category_id),
            category_name,
            merchant,
            description,
            int(amount_cents),
            _json_dump_ids(neighbor_ids),
            max(0.0, min(1.0, float(confidence))),
            source,
            proposed_action_id,
        ),
    )
    row = conn.execute(
        """
        SELECT id
        FROM classification_labels
        WHERE transaction_id=?
        """,
        (int(transaction_id),),
    ).fetchone()
    return int(row["id"])


def clear_label(conn: sqlite3.Connection, transaction_id: int) -> int:
    """Remove any gold label for a transaction."""
    cur = conn.execute(
        "DELETE FROM classification_labels WHERE transaction_id=?",
        (int(transaction_id),),
    )
    return int(cur.rowcount)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["neighbor_ids"] = _json_load_ids(row["neighbor_ids_json"])
    return out


def list_labels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM classification_labels
        ORDER BY created_at DESC, id DESC
        """
    ).fetchall()
    return [_decode(row) for row in rows]


def export_eval_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return rows in the classification eval fixture shape."""
    return [
        {
            "transaction_id": int(row["transaction_id"]),
            "merchant": row["merchant"],
            "description": row["description"],
            "amount_cents": int(row["amount_cents"]),
            "expected_category": row["category_name"],
            "category_id": int(row["category_id"]),
            "neighbor_ids": row["neighbor_ids"],
            "confidence": float(row["confidence"]),
            "source": row["source"],
            "proposed_action_id": row["proposed_action_id"],
        }
        for row in list_labels(conn)
    ]
