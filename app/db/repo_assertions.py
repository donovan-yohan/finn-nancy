"""account_balance_assertions access — record + read per-account balance assertions.

A row records that at ``asof_date`` the account balance was asserted to be
``asserted_cents`` (signed as the ledger is: expenses negative, credits positive),
captured from a statement's closing balance. Writes upsert on UNIQUE(account_id,
asof_date) so a re-approved/re-processed statement refreshes the assertion in place.
"""
from __future__ import annotations

import sqlite3


def record_assertion(conn: sqlite3.Connection, *, account_id: int, asof_date: str,
                     asserted_cents: int, source_document_id: int | None = None,
                     statement_period: str = "") -> int:
    """Upsert one assertion; returns its id. Re-recording the same (account, date)
    refreshes the asserted amount and its provenance rather than duplicating."""
    cur = conn.execute(
        """INSERT INTO account_balance_assertions(
             account_id, asof_date, asserted_cents, source_document_id, statement_period)
           VALUES (?,?,?,?,?)
           ON CONFLICT(account_id, asof_date) DO UPDATE SET
             asserted_cents=excluded.asserted_cents,
             source_document_id=excluded.source_document_id,
             statement_period=excluded.statement_period,
             created_at=CURRENT_TIMESTAMP
           RETURNING id""",
        (account_id, asof_date, asserted_cents, source_document_id, statement_period),
    )
    return int(cur.fetchone()[0])


def get_assertion(conn: sqlite3.Connection, assertion_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM account_balance_assertions WHERE id=?", (assertion_id,)
    ).fetchone()


def get_assertion_by_account_date(conn: sqlite3.Connection, account_id: int,
                                  asof_date: str) -> sqlite3.Row | None:
    """The assertion for one (account, asof_date) — the pair is UNIQUE, so at most one."""
    return conn.execute(
        "SELECT * FROM account_balance_assertions WHERE account_id=? AND asof_date=?",
        (account_id, asof_date),
    ).fetchone()


def latest_for_account(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
    """The most recent assertion for an account, by asof_date."""
    return conn.execute(
        """SELECT * FROM account_balance_assertions
           WHERE account_id=? ORDER BY asof_date DESC, id DESC LIMIT 1""",
        (account_id,),
    ).fetchone()


def list_assertions(conn: sqlite3.Connection, *, month: str | None = None,
                    account_id: int | None = None) -> list[sqlite3.Row]:
    """All assertions, newest asof first. Filter by closing ``month`` ('YYYY-MM',
    matched against the asof_date prefix) and/or a single ``account_id``."""
    sql = ["SELECT * FROM account_balance_assertions WHERE 1=1"]
    args: list = []
    if month is not None:
        sql.append("AND asof_date LIKE ?")
        args.append(f"{month}-%")
    if account_id is not None:
        sql.append("AND account_id=?")
        args.append(account_id)
    sql.append("ORDER BY asof_date DESC, id DESC")
    return conn.execute(" ".join(sql), args).fetchall()
