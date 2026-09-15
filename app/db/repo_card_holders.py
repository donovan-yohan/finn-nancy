"""Map a card's last four digits to the person who spends on it.

A statement prints per-card sections but only ever names the primary holder, so
the mapping is user-supplied and recorded once. Everything here is deliberately
non-blocking: an unknown card resolves to a stable synthetic label rather than
raising or refusing, because a new supplemental card appearing mid-statement is
ordinary life and must not be able to stall a close.
"""
from __future__ import annotations

import sqlite3

MAX_NAME_CHARS = 120


def unassigned_label(card_last4: str) -> str:
    return f"Unassigned card ••{card_last4}"


def observe(
    conn: sqlite3.Connection, card_last4: str, *, account_id: int | None = None
) -> None:
    """Record that a card exists, without naming it."""
    if not _valid(card_last4):
        return
    conn.execute(
        """INSERT INTO card_holders(account_id, card_last4, display_name)
           VALUES (?,?,'')
           ON CONFLICT(IFNULL(account_id, 0), card_last4) DO NOTHING""",
        (account_id, card_last4),
    )


def set_name(
    conn: sqlite3.Connection,
    card_last4: str,
    display_name: str,
    *,
    account_id: int | None = None,
) -> None:
    if not _valid(card_last4):
        raise ValueError("card_last4 must be four digits")
    name = (display_name or "").strip()[:MAX_NAME_CHARS]
    conn.execute(
        """INSERT INTO card_holders(account_id, card_last4, display_name)
           VALUES (?,?,?)
           ON CONFLICT(IFNULL(account_id, 0), card_last4)
           DO UPDATE SET display_name=excluded.display_name,
                         updated_at=CURRENT_TIMESTAMP""",
        (account_id, card_last4, name),
    )


def label_for(
    conn: sqlite3.Connection, card_last4: str, *, account_id: int | None = None
) -> str:
    """The display label for a card: its person, or a stable placeholder."""
    if not _valid(card_last4):
        return ""
    row = conn.execute(
        """SELECT display_name FROM card_holders
           WHERE card_last4=? AND (account_id=? OR account_id IS NULL)
           ORDER BY account_id IS NULL
           LIMIT 1""",
        (card_last4, account_id),
    ).fetchone()
    if row is None or not str(row["display_name"]).strip():
        return unassigned_label(card_last4)
    return str(row["display_name"]).strip()


def labels_for(
    conn: sqlite3.Connection, cards, *, account_id: int | None = None
) -> dict[str, str]:
    return {card: label_for(conn, card, account_id=account_id) for card in cards if card}


def listing(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT holder.*, account.name AS account_name
           FROM card_holders holder
           LEFT JOIN accounts account ON account.id=holder.account_id
           ORDER BY holder.card_last4"""
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "card_last4": row["card_last4"],
            "display_name": row["display_name"],
            "account_id": row["account_id"],
            "account_name": row["account_name"] or "",
            "first_seen_at": row["first_seen_at"],
            "is_named": bool(str(row["display_name"]).strip()),
        }
        for row in rows
    ]


def unnamed_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM card_holders WHERE trim(display_name)=''"
    ).fetchone()
    return int(row["n"] or 0)


def _valid(card_last4: str) -> bool:
    return bool(card_last4) and len(card_last4) == 4 and card_last4.isdigit()
