"""Deterministic month-end anomaly scan (FN-105).

Two checks over the existing analytics views, both pure SQL + arithmetic (no
LLM) so the scan is eval-testable and stable:

  1. Category deviation — an expense category whose signed net spend this month
     departs from its trailing N-month average by more than a configurable
     percentage *and* dollar amount. Catches "Dining +40% vs trend" as an
     increase and "no rent this month" as a drop-to-zero decrease.
  2. Missing recurring — a merchant that recurred (present last month, seen in
     enough months to be recurring) but has no charge this month at all.

The result is a short typed list ordered worst-first by dollar magnitude; it
becomes the fourth Close Inbox source (FN-102).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..config import get_settings


@dataclass(frozen=True)
class Anomaly:
    """One flagged month-end exception, worst-first by ``severity_cents``."""

    kind: str  # 'category_deviation' | 'missing_recurring'
    reason_code: str
    severity_cents: int
    direction: str  # 'increase' | 'decrease' | 'missing'
    title: str
    detail: str
    month: str
    current_cents: int = 0
    baseline_cents: int = 0
    deviation_cents: int = 0
    pct_change: float | None = None
    category_id: int | None = None
    category_name: str | None = None
    merchant: str | None = None
    account_id: int | None = None
    account_name: str | None = None


def _money(cents: int | float | None) -> str:
    cents = int(round(cents or 0))
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _shift_month(month: str, delta: int) -> str:
    """Return ``month`` ('YYYY-MM') shifted by ``delta`` calendar months."""
    year_text, month_text = month.split("-", 1)
    index = int(year_text) * 12 + (int(month_text) - 1) + delta
    return f"{index // 12}-{index % 12 + 1:02d}"


def _category_deviations(
    conn: sqlite3.Connection,
    month: str,
    *,
    trailing_months: int,
    deviation_pct: float,
    min_deviation_cents: int,
) -> list[Anomaly]:
    start = _shift_month(month, -trailing_months)

    # Divide the trailing spend by the months of ledger history actually in the
    # window (not by how many months a category happened to appear) so a steady
    # monthly category averages to its true monthly figure while an occasional
    # one-off is diluted toward zero. No history -> no established trend to
    # deviate from, so nothing is flagged.
    months_available = conn.execute(
        "SELECT COUNT(*) AS n FROM v_month_spine WHERE month >= ? AND month < ?",
        (start, month),
    ).fetchone()["n"]
    if months_available <= 0:
        return []

    current = {
        row["category_id"]: row
        for row in conn.execute(
            """
            SELECT category_id, category_name, net_expense_cents
            FROM v_planning_category_monthly_net
            WHERE month = ?
            """,
            (month,),
        ).fetchall()
    }
    trailing = {
        row["category_id"]: row
        for row in conn.execute(
            """
            SELECT
              category_id,
              MAX(category_name) AS category_name,
              SUM(net_expense_cents) AS trailing_sum
            FROM v_planning_category_monthly_net
            WHERE month >= ? AND month < ?
            GROUP BY category_id
            """,
            (start, month),
        ).fetchall()
    }

    anomalies: list[Anomaly] = []
    for category_id in current.keys() | trailing.keys():
        trailing_row = trailing.get(category_id)
        # A trend only exists once there is positive trailing spend to deviate
        # from; brand-new categories are surfaced by other close sources.
        baseline = 0
        if trailing_row is not None:
            baseline = int(trailing_row["trailing_sum"]) // months_available
        if baseline <= 0:
            continue

        current_row = current.get(category_id)
        current_cents = int(current_row["net_expense_cents"]) if current_row else 0
        deviation = current_cents - baseline
        pct = 100.0 * deviation / baseline

        if abs(deviation) < min_deviation_cents or abs(pct) < deviation_pct:
            continue

        category_name = (
            current_row["category_name"] if current_row else trailing_row["category_name"]
        )
        direction = "increase" if deviation > 0 else "decrease"
        moved = "up" if deviation > 0 else "down"
        anomalies.append(
            Anomaly(
                kind="category_deviation",
                reason_code="anomaly_category_deviation",
                severity_cents=abs(deviation),
                direction=direction,
                title=f"{category_name} spend {moved} vs trend",
                detail=(
                    f"{category_name} spent {_money(current_cents)} this month, "
                    f"{moved} {abs(pct):.1f}% from the {_money(baseline)} "
                    f"trailing {months_available}-month average."
                ),
                month=month,
                current_cents=current_cents,
                baseline_cents=baseline,
                deviation_cents=deviation,
                pct_change=round(pct, 1),
                category_id=int(category_id),
                category_name=category_name,
            )
        )
    return anomalies


def _missing_recurring(
    conn: sqlite3.Connection,
    month: str,
    *,
    min_recurring_months: int,
) -> list[Anomaly]:
    previous_month = _shift_month(month, -1)
    rows = conn.execute(
        """
        SELECT
          prev.merchant,
          prev.account_id,
          prev.account_name,
          prev.amount_cents AS previous_amount_cents,
          prev.avg_monthly_cents,
          prev.months_seen
        FROM v_recurring_payment_series_monthly prev
        WHERE prev.month = ?
          AND prev.months_seen >= ?
          AND NOT EXISTS (
            SELECT 1
            FROM v_recurring_payment_series_monthly curr
            WHERE curr.merchant = prev.merchant
              AND curr.account_id = prev.account_id
              AND curr.month = ?
          )
        ORDER BY prev.avg_monthly_cents DESC, prev.merchant
        """,
        (previous_month, min_recurring_months, month),
    ).fetchall()

    anomalies: list[Anomaly] = []
    for row in rows:
        avg_cents = int(round(row["avg_monthly_cents"] or 0))
        merchant = row["merchant"]
        account_name = row["account_name"]
        anomalies.append(
            Anomaly(
                kind="missing_recurring",
                reason_code="anomaly_missing_recurring",
                severity_cents=avg_cents,
                direction="missing",
                title=f"{merchant} recurring payment missing",
                detail=(
                    f"{merchant} ({account_name}) has no charge this month; it "
                    f"averaged {_money(avg_cents)} over {int(row['months_seen'])} "
                    f"months and last charged {_money(row['previous_amount_cents'])} "
                    f"in {previous_month}."
                ),
                month=month,
                current_cents=0,
                baseline_cents=avg_cents,
                deviation_cents=-avg_cents,
                merchant=merchant,
                account_id=int(row["account_id"]),
                account_name=account_name,
            )
        )
    return anomalies


def scan_anomalies(
    conn: sqlite3.Connection,
    month: str,
    *,
    trailing_months: int | None = None,
    deviation_pct: float | None = None,
    min_deviation_cents: int | None = None,
    min_recurring_months: int | None = None,
) -> list[Anomaly]:
    """Scan ``month`` for spending anomalies, worst (largest dollars) first.

    Thresholds default to the app settings but can be overridden per call.
    """
    settings = get_settings()
    trailing_months = trailing_months if trailing_months is not None else settings.anomaly_trailing_months
    deviation_pct = deviation_pct if deviation_pct is not None else settings.anomaly_deviation_pct
    min_deviation_cents = (
        min_deviation_cents if min_deviation_cents is not None else settings.anomaly_min_deviation_cents
    )
    min_recurring_months = (
        min_recurring_months if min_recurring_months is not None else settings.anomaly_min_recurring_months
    )
    if trailing_months < 1:
        raise ValueError("trailing_months must be >= 1")
    if min_recurring_months < 1:
        raise ValueError("min_recurring_months must be >= 1")
    if deviation_pct < 0:
        raise ValueError("deviation_pct must be >= 0")
    if min_deviation_cents < 0:
        raise ValueError("min_deviation_cents must be >= 0")

    anomalies = _category_deviations(
        conn,
        month,
        trailing_months=trailing_months,
        deviation_pct=deviation_pct,
        min_deviation_cents=min_deviation_cents,
    )
    anomalies.extend(
        _missing_recurring(conn, month, min_recurring_months=min_recurring_months)
    )
    anomalies.sort(key=lambda a: (-a.severity_cents, a.kind, a.title))
    return anomalies
