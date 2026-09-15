"""Accounts, categories, transactions, and split writes.

Category policy: user categories are NEVER auto-minted. The single exception is the
system 'Uncategorized' fallback, ensured here. Legacy merchant aliases are read
only migration evidence; scoped claims live in ``repo_merchant_knowledge``.
"""
from __future__ import annotations

import sqlite3

from ..accounting.contract import FlowKind
from ..accounting.flows import validate_flow_amount
from . import repo_close


def ensure_uncategorized(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM categories WHERE name='Uncategorized'").fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Uncategorized','expense','finn','#9AA5B1')"
    )
    return int(cur.lastrowid)


def find_category_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()


def ensure_default_account(conn: sqlite3.Connection, card_last4: str | None = None) -> int:
    """Resolve which account a receipt lands in.

    Prefer an account whose external_ref ends in the card's last4; else the first
    cash account (then any account); else create an 'Unassigned' cash account so a
    fresh database can still accept a receipt (accounts.account_id is NOT NULL).
    """
    if card_last4:
        row = conn.execute(
            "SELECT id FROM accounts WHERE external_ref <> '' AND external_ref LIKE ?",
            (f"%{card_last4}",),
        ).fetchone()
        if row:
            return int(row["id"])
    row = conn.execute("SELECT id FROM accounts ORDER BY (kind='cash') DESC, id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Unassigned','','cash','CAD')"
    )
    return int(cur.lastrowid)


def insert_transaction(conn: sqlite3.Connection, *, account_id: int, posted_on: str,
                       description: str, counterparty: str, amount_cents: int, source: str,
                       external_id: str, source_document_id: int | None,
                       source_confidence: float, flow_kind: FlowKind | str,
                       notes: str = "",
                       closed_month_override: bool = False) -> int | None:
    """Insert a transaction; returns id, or None if (source, external_id) already exists."""
    flow = validate_flow_amount(flow_kind, amount_cents)
    existing = conn.execute(
        "SELECT id FROM transactions WHERE source=? AND external_id=?",
        (source, external_id),
    ).fetchone()
    if existing is not None:
        return None
    repo_close.guard_transaction_insert(
        conn,
        posted_on,
        override=closed_month_override,
    )
    cur = conn.execute(
        """INSERT INTO transactions(
             account_id, posted_on, description, counterparty, amount_cents,
             source, external_id, source_document_id, source_confidence, flow_kind, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source, external_id) DO NOTHING""",
        (account_id, posted_on, description, counterparty, amount_cents,
         source, external_id, source_document_id, source_confidence, flow.value, notes),
    )
    if cur.rowcount == 0:
        return None
    return int(cur.lastrowid)


def insert_split(conn: sqlite3.Connection, *, transaction_id: int, category_id: int,
                 amount_cents: int, memo: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents, memo) VALUES (?,?,?,?)",
        (transaction_id, category_id, amount_cents, memo),
    )
    return int(cur.lastrowid)


def lookup_merchant_alias(conn: sqlite3.Connection, raw_pattern: str) -> sqlite3.Row | None:
    """Return inert legacy evidence for migration/admin inspection only."""
    return conn.execute("SELECT * FROM merchant_aliases WHERE raw_pattern=?", (raw_pattern,)).fetchone()


def learn_merchant_alias(conn: sqlite3.Connection, raw_pattern: str, canonical: str,
                         category_id: int | None) -> None:
    del conn, raw_pattern, canonical, category_id
    raise ValueError(
        "merchant_aliases is frozen; use scoped merchant resolution claims"
    )
