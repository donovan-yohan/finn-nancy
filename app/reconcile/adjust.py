"""Forced reconciliation adjustment (FN-107).

YNAB's insight: a small unexplained variance should not block close forever — it can
be *accepted*, logged, and moved past. From a balance-assertion exception (FN-106) we
insert one clearly-tagged, reversible transaction that moves the ledger exactly onto
the asserted balance, clearing the exception, and write a ``close_audit`` row so the
forced adjustment is never silent.

Sign convention (mirrors ``assertions``): ``delta_cents = ledger - asserted``. To zero
the mismatch the ledger must gain ``asserted - ledger == -delta_cents``, so the
adjustment transaction (and its single split) is booked at ``-delta_cents``. Because
``assertions.ledger_balance_cents`` sums ``transaction_splits`` on the account through
``asof_date`` inclusive, a split posted on ``asof_date`` re-runs the check to a tie.

There is exactly one adjustment per ``(source='adjustment', external_id)``: a re-submit
that finds the ledger already tied is a no-op, but a re-submit after a *backdated or
edited* transaction reopened the exception **re-books in place** — the existing
adjustment's amount (and split) move so the ledger ties again, instead of silently
no-op'ing on the amount-blind key. It stays reversible by the ordinary transaction
delete (FN-103's month-lock guard applies there): removing the transaction drops its
split and the exception re-appears on the next scan. Creating or revising one in a
*closed* month is refused unless ``override`` is set — the normal flow is pre-sign-off,
when the exception is still open by definition.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..accounting.contract import FlowKind
from ..db import repo_assertions, repo_close, repo_ledger, repo_period_policy
from . import assertions


@dataclass(frozen=True)
class AdjustmentResult:
    status: str  # 'created' | 'rebooked' | 'exists' | 'tie' | 'no_assertion'
    account_id: int
    asof_date: str
    delta_cents: int = 0        # ledger - asserted at (re)booking time
    amount_cents: int = 0       # the booked adjustment amount now on the transaction
    transaction_id: int | None = None


def _external_id(account_id: int, asof_date: str) -> str:
    return f"reconadj:{account_id}:{asof_date}"


def _existing_adjustment(conn: sqlite3.Connection, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, amount_cents FROM transactions WHERE source='adjustment' AND external_id=?",
        (external_id,),
    ).fetchone()


def create_adjustment(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    asof_date: str,
    override: bool = False,
    actor: str = "",
    override_reason: str = "",
    operation_key: str = "",
) -> AdjustmentResult:
    """Book (or re-book) the reconciliation adjustment that zeroes the assertion at
    ``asof_date``.

    Raises :class:`repo_close.MonthLockedError` when the adjustment's month is closed
    and ``override`` is falsy. A closed-month override also requires ``actor``,
    ``override_reason``, and ``operation_key``; the shared period policy reopens the
    month and durably records that authority before this writer mutates the ledger.
    Returns an :class:`AdjustmentResult`; ``status`` is ``no_assertion`` when no
    assertion is stored, ``tie`` when the ledger already reconciles (nothing to book),
    ``rebooked`` when a prior adjustment was moved in place because drift reopened the
    exception, ``exists`` when an adjustment could not be (re)booked (a race left the
    key taken), and ``created`` on the first booking.
    """
    row = repo_assertions.get_assertion_by_account_date(conn, account_id, asof_date)
    if row is None:
        return AdjustmentResult(status="no_assertion", account_id=account_id, asof_date=asof_date)

    check = assertions.check_assertion(conn, row)
    if not check.is_exception:
        # The ledger already reconciles — possibly *because* a prior adjustment is in
        # place. Nothing to (re)book.
        return AdjustmentResult(status="tie", account_id=account_id, asof_date=asof_date,
                                delta_cents=check.delta_cents)

    month = asof_date[:7]
    locked = repo_close.is_month_locked(conn, month)
    if locked and not override:
        # Same override semantics for a fresh booking or a re-book: the revised
        # transaction also posts in ``month``, so FN-103's soft lock applies to it too.
        raise repo_close.MonthLockedError(month)

    external_id = _external_id(account_id, asof_date)
    existing = _existing_adjustment(conn, external_id)
    old_amount = int(existing["amount_cents"]) if existing is not None else 0
    # ``check.delta_cents`` is measured *with* any prior adjustment already in the ledger,
    # so the correction stacks on the old amount — setting it to ``-delta`` alone would
    # drop the prior adjustment's contribution and re-open the exception.
    booked = old_amount - check.delta_cents  # -> ledger ties to the asserted balance

    reason = (
        f"forced reconciliation adjustment {assertions._money(booked)} on "
        f"{check.account_name} to zero the balance assertion at {asof_date} "
        f"(ledger {assertions._money(check.ledger_cents)} vs asserted "
        f"{assertions._money(check.asserted_cents)})"
    )
    if locked:  # only reachable with override
        reason += (
            f" (override: month closed; actor={actor.strip() or 'missing'}; "
            f"reason={override_reason.strip() or 'missing'})"
        )
    if existing is not None:
        reason += f" (revised from {assertions._money(old_amount)})"

    if locked and override:
        state = repo_period_policy.get_state(conn, month)
        assert state is not None
        repo_period_policy.guard_months(
            conn,
            [month],
            override=True,
            actor=actor,
            reason=override_reason,
            operation_key=operation_key,
            affected_ids={
                "account_id": int(account_id),
                "assertion_id": int(row["id"]),
                **(
                    {}
                    if existing is None
                    else {"transaction_id": int(existing["id"])}
                ),
            },
            evidence={
                "action": (
                    "rebook_reconciliation_adjustment"
                    if existing is not None
                    else "create_reconciliation_adjustment"
                ),
                "asof_date": asof_date,
                "delta_cents": int(check.delta_cents),
                "booked_amount_cents": int(booked),
            },
        )

    if existing is not None:
        # Re-book: a backdated or edited transaction reopened the exception after a prior
        # adjustment. Move the existing adjustment (and its split) in place so the ledger
        # ties again — the amount-blind idempotency key would otherwise no-op this.
        txn_id = int(existing["id"])
        conn.execute("UPDATE transactions SET amount_cents=?, notes=? WHERE id=?",
                     (booked, reason, txn_id))
        conn.execute(
            "UPDATE transaction_splits SET amount_cents=?, memo='reconciliation adjustment (revised)' "
            "WHERE transaction_id=?",
            (booked, txn_id),
        )
        repo_close.record_audit(
            conn, month=month, entity="transaction", entity_id=txn_id,
            field="reconciliation_adjustment", old_value=old_amount, new_value=booked,
            reason=reason,
        )
        return AdjustmentResult(status="rebooked", account_id=account_id, asof_date=asof_date,
                                delta_cents=check.delta_cents, amount_cents=booked,
                                transaction_id=txn_id)

    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=asof_date,
        description=f"Reconciliation adjustment — {check.account_name}",
        counterparty="Reconciliation adjustment",
        amount_cents=booked,
        source="adjustment",
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind=FlowKind.ADJUSTMENT,
        notes=reason,
        # Any locked month was already atomically reopened above through the
        # canonical audited FN-147 capability.
        closed_month_override=False,
    )
    if txn_id is None:  # (source, external_id) taken under a race — surface, don't fake success
        return AdjustmentResult(status="exists", account_id=account_id, asof_date=asof_date,
                                delta_cents=check.delta_cents, amount_cents=booked)

    category_id = repo_ledger.ensure_uncategorized(conn)
    repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=category_id,
                             amount_cents=booked, memo="reconciliation adjustment")
    repo_close.record_audit(
        conn, month=month, entity="transaction", entity_id=txn_id,
        field="reconciliation_adjustment", old_value=None, new_value=booked,
        reason=reason,
    )
    return AdjustmentResult(status="created", account_id=account_id, asof_date=asof_date,
                            delta_cents=check.delta_cents, amount_cents=booked,
                            transaction_id=txn_id)
