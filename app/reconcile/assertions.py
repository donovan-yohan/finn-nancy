"""Balance-assertion reconciliation (FN-106).

A balance assertion says: at ``asof_date`` this account's balance was
``asserted_cents`` (captured from a statement's closing balance). We compute the
*ledger balance to that date* — the signed sum of every transaction split on the
account posted on or before ``asof_date`` — and compare. Pure arithmetic over
``transaction_splits`` (the canonical money view), no LLM, so it is eval-stable.

Delta convention: ``delta_cents = ledger_cents - asserted_cents``.
  * ``tie``   — delta == 0; the ledger reconciles to the statement, NO exception.
  * ``over``  — delta  > 0; the ledger records MORE than the statement asserts.
  * ``under`` — delta  < 0; the ledger records LESS than the statement asserts.
This holds under both sign conventions: an asset account (positive balances) and a
credit-card liability (negative balances) both fall out of the same subtraction.

Close Inbox (FN-102) entry point: call ``scan_assertion_exceptions(conn, month=...)``
to get the non-tie checks for a closing month, worst (largest |delta|) first. Each
carries a reason_code/title/detail ready to render as a close exception. The
canonical statement-review approval service records the assertion from the
reviewed actual closing date.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ..db import repo_assertions
from ..ingest.schemas import ExtractedStatement

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class AssertionCheck:
    """Outcome of checking one balance assertion against the ledger.

    ``status`` is one of 'tie' | 'over' | 'under'; only the latter two are
    exceptions (``is_exception``). The presentation fields (reason_code/title/
    detail/severity_cents) are populated only for exceptions.
    """

    status: str
    account_id: int
    account_name: str
    asof_date: str
    asserted_cents: int
    ledger_cents: int
    delta_cents: int  # ledger_cents - asserted_cents; 0 on tie
    assertion_id: int | None = None
    statement_period: str = ""
    reason_code: str = ""
    severity_cents: int = 0
    title: str = ""
    detail: str = ""

    @property
    def is_exception(self) -> bool:
        return self.status != "tie"


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def ledger_balance_cents(conn: sqlite3.Connection, account_id: int, asof_date: str) -> int:
    """Signed ledger balance for an account through ``asof_date`` inclusive.

    Sums ``transaction_splits.amount_cents`` (not ``transactions.amount_cents``) so it
    stays on the canonical money view every report reads; the splits of a transaction
    partition its amount, so the two sums agree by construction.
    """
    row = conn.execute(
        """SELECT COALESCE(SUM(ts.amount_cents), 0) AS bal
           FROM transaction_splits ts
           JOIN transactions t ON t.id = ts.transaction_id
           WHERE t.account_id=? AND t.posted_on<=?""",
        (account_id, asof_date),
    ).fetchone()
    return int(row["bal"])


def _account_name(conn: sqlite3.Connection, account_id: int) -> str:
    row = conn.execute("SELECT name FROM accounts WHERE id=?", (account_id,)).fetchone()
    return row["name"] if row else f"account {account_id}"


def check_assertion(conn: sqlite3.Connection, assertion: sqlite3.Row) -> AssertionCheck:
    """Evaluate one stored assertion row against the current ledger."""
    account_id = int(assertion["account_id"])
    asof_date = assertion["asof_date"]
    asserted = int(assertion["asserted_cents"])
    ledger = ledger_balance_cents(conn, account_id, asof_date)
    delta = ledger - asserted
    name = _account_name(conn, account_id)

    if delta == 0:
        status, reason_code, title, detail = "tie", "", "", ""
    else:
        status = "over" if delta > 0 else "under"
        verb = "over" if delta > 0 else "under"
        reason_code = f"balance_assertion_{status}"
        title = f"{name} balance {verb} by {_money(abs(delta))}"
        detail = (
            f"{name} ledger totals {_money(ledger)} through {asof_date}, "
            f"but the statement asserts {_money(asserted)} "
            f"({verb} by {_money(abs(delta))})."
        )

    return AssertionCheck(
        status=status,
        account_id=account_id,
        account_name=name,
        asof_date=asof_date,
        asserted_cents=asserted,
        ledger_cents=ledger,
        delta_cents=delta,
        assertion_id=int(assertion["id"]) if assertion["id"] is not None else None,
        statement_period=assertion["statement_period"] or "",
        reason_code=reason_code,
        severity_cents=abs(delta),
        title=title,
        detail=detail,
    )


def scan_assertion_exceptions(conn: sqlite3.Connection, *, month: str | None = None,
                              account_id: int | None = None) -> list[AssertionCheck]:
    """Return the failing balance assertions as close exceptions, worst-first.

    The Close Inbox (FN-102) calls this — typically with the closing ``month`` — to
    fold balance-assertion failures in alongside its other exception sources. Ties are
    filtered out (a reconciling ledger is not an exception). ``month`` matches the
    asof_date's 'YYYY-MM' prefix; ``account_id`` narrows to a single account.
    """
    checks = [
        check_assertion(conn, row)
        for row in repo_assertions.list_assertions(conn, month=month, account_id=account_id)
    ]
    exceptions = [c for c in checks if c.is_exception]
    exceptions.sort(key=lambda c: (-c.severity_cents, c.asof_date, c.account_id))
    return exceptions


def _statement_asof_date(parsed: ExtractedStatement) -> str | None:
    """Use the actual printed statement closing date, never a transaction date."""
    closing = (parsed.period_end_on or "").strip()
    return closing if _ISO_DATE.fullmatch(closing) else None


def _canonical_asserted_cents(parsed: ExtractedStatement) -> int:
    """The closing balance normalized into the ledger's canonical sign.

    Statements print the closing balance under one of two conventions (see
    extract.statement.checksum_ok): asset-style, where balance = opening + signed_sum
    and the printed figure already matches our debit-negative ledger; or debt-style,
    where the printed figure is a positive amount-owing that our credit-card ledger
    holds as a negative. When only the debt-style reconciliation of the printed
    opening/closing holds, the printed closing is the negation of the ledger balance,
    so flip its sign before asserting. Falls back to storing verbatim when the opening
    balance is absent (convention undeterminable) or when both styles reconcile.
    """
    closing = int(parsed.closing_balance_cents)
    if parsed.opening_balance_cents is None:
        return closing
    total = sum(row.amount_cents for row in parsed.rows)
    asset_style = abs(parsed.opening_balance_cents + total - closing) <= 1
    debt_style = abs(parsed.opening_balance_cents - total - closing) <= 1
    if debt_style and not asset_style:
        return -closing
    return closing


def capture_statement_assertion(conn: sqlite3.Connection, *, parsed: ExtractedStatement,
                                account_id: int, source_document_id: int) -> int | None:
    """Record the statement's closing balance as a balance assertion; returns the
    assertion id, or None when there is nothing to capture (no closing balance or
    actual printed period-end date). Never substitutes the latest transaction date.
    """
    if parsed.closing_balance_cents is None:
        return None
    asof_date = _statement_asof_date(parsed)
    if asof_date is None:
        return None
    return repo_assertions.record_assertion(
        conn,
        account_id=account_id,
        asof_date=asof_date,
        asserted_cents=_canonical_asserted_cents(parsed),
        source_document_id=source_document_id,
        statement_period=parsed.statement_period or "",
    )
