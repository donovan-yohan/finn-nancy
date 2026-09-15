"""The month-close summary "receipt" snapshot (FN-104).

At sign-off ``close.py`` composes this snapshot and persists it into
``closed_periods.summary_json``; ``/close`` then renders it read-only for a
closed month. The whole point is *state at close time*: every figure here is
frozen into the row, so reopening and editing the month never rewrites history
until a fresh sign-off produces a new snapshot.

The snapshot is a flat JSON dict. It preserves the keys FN-102 already writes at
sign-off (``inbox_count`` / ``inbox_ack`` plus the ``percent_complete`` /
``overspend_cents`` roll-ups) and layers the receipt fields on top, so callers
that read either half keep working.
"""
from __future__ import annotations

import sqlite3


def _transactions_reviewed(conn: sqlite3.Connection, month: str) -> int:
    # Half-open [start, end) date range instead of strftime('%Y-%m', posted_on):
    # strftime() wraps the column so it can't use idx_transactions_posted_on, while
    # a bare range predicate on the 'YYYY-MM-DD' text column is sargable.
    year, mon = int(month[:4]), int(month[5:7])
    start = f"{month}-01"
    end = f"{year + 1}-01-01" if mon == 12 else f"{year}-{mon + 1:02d}-01"
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE posted_on >= ? AND posted_on < ?",
            (start, end),
        ).fetchone()[0]
    )


def _expense_resolution_metrics(
    conn: sqlite3.Connection,
    month: str,
) -> dict[str, int]:
    """Accepted category evidence, kept separate from the ledger money-out total."""
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS total_count,
          SUM(CASE WHEN resolution_status='resolved' THEN 1 ELSE 0 END)
            AS resolved_count,
          SUM(CASE WHEN resolution_status='unresolved' THEN 1 ELSE 0 END)
            AS excluded_count,
          COALESCE(SUM(
            CASE WHEN resolution_status='resolved'
              THEN ABS(split_amount_cents) ELSE 0 END
          ), 0) AS resolved_cents,
          COALESCE(SUM(
            CASE WHEN resolution_status='unresolved'
              THEN ABS(split_amount_cents) ELSE 0 END
          ), 0) AS excluded_cents
        FROM v_expense_resolution_status
        WHERE posted_on >= ?
          AND posted_on < date(?, '+1 month')
        """,
        (f"{month}-01", f"{month}-01"),
    ).fetchone()
    return {
        "total_count": int(row["total_count"]),
        "resolved_count": int(row["resolved_count"] or 0),
        "excluded_count": int(row["excluded_count"] or 0),
        "resolved_cents": int(row["resolved_cents"]),
        "excluded_cents": int(row["excluded_cents"]),
    }


def _money_out_cents(conn: sqlite3.Connection, month: str) -> int:
    """The established cashflow total; category trust never rewrites this value."""
    row = conn.execute(
        "SELECT expense_cents FROM v_cashflow_monthly WHERE month=?",
        (month,),
    ).fetchone()
    return int(row["expense_cents"]) if row is not None else 0


def _goals_funded(conn: sqlite3.Connection, month: str) -> tuple[int, int]:
    """(count, cents) of goals with an applied funding row in ``month``."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(actual_cents), 0) AS cents
        FROM goal_ledger
        WHERE month = ? AND status = 'applied'
        """,
        (month,),
    ).fetchone()
    return int(row["n"]), int(row["cents"])


def compose_summary(
    conn: sqlite3.Connection,
    month: str,
    *,
    check: dict,
    inbox_count: int,
    inbox_ack: bool,
    variance_ack: bool,
    anomaly_count: int,
) -> dict:
    """Freeze the month's completeness figures into the close-receipt snapshot.

    ``check`` is the checklist read-model (``app.close.checklist.build_checklist``)
    already built for the sign-off; the remaining figures are queried here so the
    snapshot is a single self-contained record of the month at close time.
    """
    goals_count, goals_cents = _goals_funded(conn, month)
    resolution = _expense_resolution_metrics(conn, month)
    return {
        # Preserved from FN-102's sign-off snapshot.
        "percent_complete": check["percent_complete"],
        "overspend_cents": check["overspend_cents"],
        "inbox_count": inbox_count,
        "inbox_ack": inbox_ack,
        # FN-104 receipt fields — the frozen "here's your month".
        "transactions_reviewed": _transactions_reviewed(conn, month),
        "categories_confirmed": resolution["resolved_count"],
        "expense_resolution_total_count": resolution["total_count"],
        "expense_resolution_unresolved_count": resolution["excluded_count"],
        "resolved_expense_cents": resolution["resolved_cents"],
        "excluded_expense_cents": resolution["excluded_cents"],
        "money_out_cents": _money_out_cents(conn, month),
        "anomalies_flagged": anomaly_count,
        "anomalies_acknowledged": inbox_ack,
        "coverage_pct": check["coverage_pct"],
        # New closes cannot proceed while this is non-zero. Keeping the explicit
        # zero in the frozen receipt distinguishes a verified close from legacy
        # snapshots that predate typed flow semantics.
        "flow_review_count": check.get(
            "flow_semantic_review_count", check["flow_review_count"]
        ),
        "uncategorized_count": check["uncategorized_count"],
        "net_delta_cents": check["net_delta_cents"],
        "goals_funded_count": goals_count,
        "goals_funded_cents": goals_cents,
        "variance_ack": variance_ack,
    }
