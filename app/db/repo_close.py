"""closed_periods + close_audit access — the month-close lock/sign-off foundation.

A month is *locked* only while its period row is ``status='closed'``. ``reopen``
moves it to the distinct ``reopened`` state (unlocked, but recorded as having been
closed) rather than back to ``open`` so the audit trail can tell a never-closed
month apart from a re-opened one. Every close/reopen/override writes an append-only
``close_audit`` row; the repo exposes no update or delete path for those rows.
"""
from __future__ import annotations

import sqlite3

from . import repo_period_policy


class MonthLockedError(ValueError):
    """A ledger write touched a transaction in a *closed* month without override.

    Subclasses ``ValueError`` on purpose: every ledger/categorization write path
    already routes ``ValueError`` into its own inline/validation failure handling
    (form re-render, approval-queue 400), so the guard degrades gracefully instead
    of crashing a request or the background worker.
    """

    def __init__(self, month: str):
        self.month = month
        super().__init__(f"{month} is closed — edits require an explicit override")


def get_period(conn: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM closed_periods WHERE month=?", (month,)).fetchone()


def get_or_create_period(conn: sqlite3.Connection, month: str) -> sqlite3.Row:
    """Return the period for ``month``, creating an ``open`` row if none exists."""
    conn.execute("INSERT OR IGNORE INTO closed_periods(month) VALUES (?)", (month,))
    row = get_period(conn, month)
    assert row is not None  # just inserted-or-existing
    return row


def record_audit(conn: sqlite3.Connection, *, month: str, entity: str,
                 entity_id: int | None = None, field: str = "",
                 old_value: object = None, new_value: object = None,
                 reason: str = "") -> int:
    """Append one immutable audit row. Values stringify so any type round-trips."""
    cur = conn.execute(
        """INSERT INTO close_audit(month, entity, entity_id, field, old_value, new_value, reason)
           VALUES (?,?,?,?,?,?,?)""",
        (month, entity, entity_id, field,
         None if old_value is None else str(old_value),
         None if new_value is None else str(new_value),
         reason),
    )
    return int(cur.lastrowid)


def list_audit(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM close_audit WHERE month=? ORDER BY id", (month,)
    ).fetchall()


def mark_closed(conn: sqlite3.Connection, month: str, *, coverage_pct: float = 0.0,
                uncategorized_count: int = 0, variance_ack: bool | int = 0,
                net_delta_cents: int = 0, summary: dict | None = None,
                reason: str = "") -> sqlite3.Row:
    """Compatibility wrapper around the immutable FN-147 close boundary."""
    period = get_or_create_period(conn, month)
    current = repo_period_policy.get_state(conn, month)
    cycle_number = 1 if current is None else int(current["cycle_number"]) + 1
    close_reason = reason.strip() or "legacy close compatibility"
    exceptions: list[dict] = []
    if int(uncategorized_count) > 0:
        exceptions.append(
            {
                "exception_type": "unconfirmed_merchant_category",
                "subject_kind": "period",
                "subject_id": month,
                "affected_ids": {"period_month": month},
                "evidence": {"uncategorized_count": int(uncategorized_count)},
                "reason": (
                    f"{int(uncategorized_count)} transactions lack confirmed categories"
                ),
                "resolution_href": f"/close?month={month}#categories",
            }
        )
    snapshot = {
        **(summary or {}),
        "month": month,
        "coverage_pct": float(coverage_pct),
        "uncategorized_count": int(uncategorized_count),
        "variance_ack": bool(variance_ack),
        "net_delta_cents": int(net_delta_cents),
        "compatibility_source": "repo_close",
    }
    repo_period_policy.close_period(
        conn,
        month,
        snapshot=snapshot,
        exceptions=exceptions,
        actor="legacy:repo_close",
        reason=close_reason,
        operation_key=f"legacy:close:{month}:{cycle_number}",
    )
    conn.execute(
        """UPDATE closed_periods
           SET coverage_pct=?, uncategorized_count=?, variance_ack=?,
               net_delta_cents=?
           WHERE month=?""",
        (coverage_pct, uncategorized_count, int(bool(variance_ack)),
         net_delta_cents, month),
    )
    record_audit(conn, month=month, entity="period", entity_id=period["id"],
                 field="status", old_value=period["status"], new_value="closed",
                 reason=close_reason)
    row = get_period(conn, month)
    assert row is not None
    return row


def reopen(conn: sqlite3.Connection, month: str, *, reason: str = "") -> sqlite3.Row:
    """Compatibility wrapper which appends an explicit immutable reopen."""
    period = get_or_create_period(conn, month)
    current = repo_period_policy.get_state(conn, month)
    if current is None and str(period["status"]) == "closed":
        # A post-036 legacy caller may have written only the compatibility row.
        # Materialize that unverifiable close conservatively before reopening it.
        legacy_exception = {
            "exception_type": "evidence_gap",
            "subject_kind": "legacy_close",
            "subject_id": month,
            "affected_ids": {"period_month": month},
            "evidence": {"legacy_period_id": int(period["id"])},
            "reason": "legacy close lacks FN-147 evidence",
            "resolution_href": f"/close?month={month}",
        }
        repo_period_policy.acknowledge_preclose_exception(
            conn,
            month,
            legacy_exception,
            actor="legacy:repo_close",
            reason="preserve the unverifiable legacy close before reopening",
            operation_key=(
                f"legacy:materialize-close:{month}:{int(period['id'])}:ack"
            ),
            evidence={"compatibility_source": "post_migration_legacy_close"},
        )
        repo_period_policy.close_period(
            conn,
            month,
            snapshot={
                "month": month,
                "compatibility_source": "post_migration_legacy_close",
                "integrity_status": "unverified_legacy",
            },
            exceptions=[legacy_exception],
            actor="legacy:repo_close",
            reason="materialize post-migration legacy close before reopen",
            operation_key=f"legacy:materialize-close:{month}:{int(period['id'])}",
        )
        current = repo_period_policy.get_state(conn, month)
    event_id = 0 if current is None else int(current["event_id"])
    reopen_reason = reason.strip() or "legacy reopen compatibility"
    repo_period_policy.reopen_period(
        conn,
        month,
        actor="legacy:repo_close",
        reason=reopen_reason,
        operation_key=f"legacy:reopen:{month}:{event_id}",
        affected_ids={"period_month": month},
    )
    record_audit(conn, month=month, entity="period", entity_id=period["id"],
                 field="status", old_value=period["status"], new_value="reopened",
                 reason=reopen_reason)
    row = get_period(conn, month)
    assert row is not None
    return row


def is_month_locked(conn: sqlite3.Connection, month: str) -> bool:
    return repo_period_policy.is_month_locked(conn, month)


def transaction_month(conn: sqlite3.Connection, transaction_id: int) -> str | None:
    """The ``YYYY-MM`` a transaction posts in, or None if it doesn't exist.

    One indexed lookup (``idx_transactions_posted_on``); cheap enough to call on
    every ledger write.
    """
    return repo_period_policy.transaction_month(conn, transaction_id)


def guard_transaction_write(
    conn: sqlite3.Connection,
    transaction_id: int,
    *,
    override: bool = False,
    extra_month: str | None = None,
) -> list[str]:
    """Enforce the soft lock on a write touching ``transaction_id``.

    Returns the sorted list of *closed* months the write touches — the transaction's
    own month plus ``extra_month`` (e.g. the target month when an edit moves a
    transaction's date). Raises :class:`MonthLockedError` when any touched month is
    closed and ``override`` is falsy; when ``override`` is set the caller is expected
    to record an audit row per returned month via :func:`record_audit`.
    """
    own = transaction_month(conn, transaction_id)
    months = [month for month in (own, extra_month) if month]
    locked = sorted({month for month in months if is_month_locked(conn, month)})
    if not locked:
        return []
    if not override:
        raise MonthLockedError(locked[0])
    state_receipt = ",".join(
        f"{month}:{repo_period_policy.get_state(conn, month)['event_id']}"
        for month in locked
    )
    repo_period_policy.guard_months(
        conn,
        locked,
        override=True,
        actor="legacy:repo_close",
        reason="explicit audited transaction override",
        operation_key=f"legacy:transaction-write:{int(transaction_id)}:{state_receipt}",
        affected_ids={"transaction_id": int(transaction_id)},
    )
    return locked


def guard_transaction_insert(
    conn: sqlite3.Connection,
    posted_on: str,
    *,
    override: bool = False,
) -> list[str]:
    """Enforce the month lock before creating a new transaction.

    Centralizing this below every production writer prevents API, MCP, receipt,
    statement, reconciliation, and web insert paths from drifting apart.
    """
    month = repo_period_policy.month_for_date(posted_on)
    if not is_month_locked(conn, month):
        return []
    if not override:
        raise MonthLockedError(month)
    state = repo_period_policy.get_state(conn, month)
    assert state is not None
    repo_period_policy.guard_months(
        conn,
        [month],
        override=True,
        actor="legacy:repo_close",
        reason="explicit audited transaction insert override",
        operation_key=(
            f"legacy:transaction-insert:{month}:{state['event_id']}:{posted_on}"
        ),
        affected_ids={"posted_on": str(posted_on)},
    )
    return [month]
