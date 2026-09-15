"""Deterministic goals, payoff math, and monthly close helpers.

Monthly close rows are history. Fresh closes snapshot the then-current plan;
corrective re-closes only recompute the available underspend/actual side for
existing applied underspend rows and keep stored ``planned_cents`` frozen. An
applied non-underspend row for a goal-month is treated as manual history and is
not rewritten by close. Completion is recomputed only for goals participating in
that close; users who want an under-target auto-funded goal to stay completed
should pause or cancel it.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date
from typing import Any

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
PAYOFF_TOLERANCE_CENTS = 1


def _validate_month(month: str) -> str:
    if not MONTH_RE.match(month):
        raise ValueError("invalid month")
    year_text, month_text = month.split("-", 1)
    month_number = int(month_text)
    if month_number < 1 or month_number > 12:
        raise ValueError("invalid month")
    return f"{int(year_text):04d}-{month_number:02d}"


def _today_month() -> str:
    return date.today().strftime("%Y-%m")


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator <= 0:
        return 0
    return (numerator + denominator - 1) // denominator


def add_months(month: str, months: int) -> str:
    """Return ``month`` shifted by ``months`` calendar months."""
    month = _validate_month(month)
    year, month_number = (int(part) for part in month.split("-", 1))
    absolute = year * 12 + (month_number - 1) + months
    new_year, zero_based_month = divmod(absolute, 12)
    return f"{new_year:04d}-{zero_based_month + 1:02d}"


def diff_months(start_month: str, end_month: str) -> int:
    """Return ``end_month - start_month`` in whole calendar months."""
    start_month = _validate_month(start_month)
    end_month = _validate_month(end_month)
    start_year, start_number = (int(part) for part in start_month.split("-", 1))
    end_year, end_number = (int(part) for part in end_month.split("-", 1))
    return (end_year - start_year) * 12 + (end_number - start_number)


def elapsed_months(start_month: str, as_of_month: str) -> int:
    """Inclusive months from start through as-of; zero when as-of is before start."""
    elapsed = diff_months(start_month, as_of_month) + 1
    return max(0, elapsed)


def planned_total_months(
    start_month: str,
    target_month: str | None,
    *,
    target_cents: int | None = None,
    monthly_contribution_cents: int | None = None,
) -> int:
    """Inclusive planned duration.

    When a goal has no explicit target month, infer the duration from the target
    and current monthly contribution. A zero monthly contribution cannot define a
    useful schedule, so the conservative fallback is one month.
    """
    _validate_month(start_month)
    if target_month:
        return max(1, elapsed_months(start_month, target_month))
    target = int(target_cents or 0)
    monthly = int(monthly_contribution_cents or 0)
    if target > 0 and monthly > 0:
        return max(1, _ceil_div(target, monthly))
    return 1


def payoff_status(
    *,
    target_cents: int,
    contributed_cents: int,
    monthly_contribution_cents: int,
    start_month: str,
    target_month: str | None,
    as_of_month: str,
    as_of_funded: bool = True,
) -> dict[str, Any]:
    """Return deterministic payoff progress and re-plan math for a goal."""
    target_cents = max(0, int(target_cents))
    contributed_cents = max(0, int(contributed_cents))
    monthly_contribution_cents = max(0, int(monthly_contribution_cents))
    as_of_month = _validate_month(as_of_month)
    start_month = _validate_month(start_month)
    if target_month:
        target_month = _validate_month(target_month)

    remaining_cents = max(0, target_cents - contributed_cents)
    total_months = planned_total_months(
        start_month,
        target_month,
        target_cents=target_cents,
        monthly_contribution_cents=monthly_contribution_cents,
    )
    raw_elapsed = elapsed_months(start_month, as_of_month)
    effective_elapsed = raw_elapsed if as_of_funded else max(0, raw_elapsed - 1)
    if effective_elapsed <= 0 or target_cents <= 0:
        expected_by_now_cents = 0
    else:
        expected_months = min(effective_elapsed, total_months)
        expected_by_now_cents = min(
            target_cents,
            math.ceil(target_cents * expected_months / total_months),
        )

    months_remaining = max(1, total_months - effective_elapsed)
    required_monthly_cents = _ceil_div(remaining_cents, months_remaining)
    ahead_behind_cents = contributed_cents - expected_by_now_cents

    if contributed_cents >= target_cents and target_cents > 0:
        state = "completed"
    elif contributed_cents < expected_by_now_cents - PAYOFF_TOLERANCE_CENTS:
        state = "behind"
    elif contributed_cents > expected_by_now_cents + PAYOFF_TOLERANCE_CENTS:
        state = "ahead"
    else:
        state = "on_track"

    if remaining_cents <= 0:
        projected_target_month = as_of_month
    elif monthly_contribution_cents <= 0:
        projected_target_month = None
    else:
        months_needed = _ceil_div(remaining_cents, monthly_contribution_cents)
        if raw_elapsed == 0:
            projected_target_month = add_months(add_months(start_month, -1), months_needed)
        elif as_of_funded:
            projected_target_month = add_months(as_of_month, months_needed)
        else:
            projected_target_month = add_months(as_of_month, months_needed - 1)

    return {
        "remaining_cents": remaining_cents,
        "expected_by_now_cents": expected_by_now_cents,
        "state": state,
        "required_monthly_cents": required_monthly_cents,
        "projected_target_month": projected_target_month,
        "months_remaining": months_remaining,
        "ahead_behind_cents": ahead_behind_cents,
        "elapsed_months": effective_elapsed,
        "planned_total_months": total_months,
    }


def _goal_row(conn: sqlite3.Connection, goal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if row is None:
        raise LookupError("goal not found")
    return row


def _contributed_through(conn: sqlite3.Connection, goal_id: int, as_of_month: str) -> int:
    as_of_month = _validate_month(as_of_month)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(actual_cents), 0) AS contributed_cents
        FROM goal_ledger
        WHERE goal_id = ?
          AND status = 'applied'
          AND month <= ?
        """,
        (goal_id, as_of_month),
    ).fetchone()
    return int(row["contributed_cents"] or 0)


def _contributed_other(conn: sqlite3.Connection, goal_id: int, month: str) -> int:
    month = _validate_month(month)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(actual_cents), 0) AS contributed_cents
        FROM goal_ledger
        WHERE goal_id = ?
          AND status = 'applied'
          AND month != ?
        """,
        (goal_id, month),
    ).fetchone()
    return int(row["contributed_cents"] or 0)


def _has_applied_row(conn: sqlite3.Connection, goal_id: int, month: str) -> bool:
    month = _validate_month(month)
    row = conn.execute(
        """
        SELECT 1
        FROM goal_ledger
        WHERE goal_id = ?
          AND month = ?
          AND status = 'applied'
        """,
        (goal_id, month),
    ).fetchone()
    return row is not None


def _payoff_for_goal(conn: sqlite3.Connection, goal: sqlite3.Row, as_of_month: str) -> dict[str, Any]:
    return payoff_status(
        target_cents=goal["target_cents"],
        contributed_cents=_contributed_through(conn, int(goal["id"]), as_of_month),
        monthly_contribution_cents=goal["monthly_contribution_cents"],
        start_month=goal["start_month"],
        target_month=goal["target_month"],
        as_of_month=as_of_month,
        as_of_funded=_has_applied_row(conn, int(goal["id"]), as_of_month),
    )


def _implied_target_month(goal: sqlite3.Row) -> str:
    total_months = planned_total_months(
        goal["start_month"],
        None,
        target_cents=goal["target_cents"],
        monthly_contribution_cents=goal["monthly_contribution_cents"],
    )
    return add_months(goal["start_month"], total_months - 1)


def catch_up(conn: sqlite3.Connection, goal_id: int, as_of_month: str) -> dict[str, Any]:
    """Raise/lower monthly contribution to hit the existing target month."""
    goal = _goal_row(conn, goal_id)
    status = _payoff_for_goal(conn, goal, as_of_month)
    if goal["status"] == "completed" or int(status["remaining_cents"]) <= 0:
        return status
    target_month = goal["target_month"]
    if target_month is None:
        target_month = _implied_target_month(goal)
        conn.execute("UPDATE goals SET target_month=? WHERE id=?", (target_month, goal_id))
        goal = _goal_row(conn, goal_id)
        status = _payoff_for_goal(conn, goal, as_of_month)
    conn.execute(
        "UPDATE goals SET monthly_contribution_cents=? WHERE id=?",
        (status["required_monthly_cents"], goal_id),
    )
    return status


def extend(conn: sqlite3.Connection, goal_id: int, as_of_month: str) -> dict[str, Any]:
    """Push the target month out to the projected completion month at current monthly pace."""
    goal = _goal_row(conn, goal_id)
    status = _payoff_for_goal(conn, goal, as_of_month)
    if int(status["remaining_cents"]) <= 0:
        status["target_month"] = goal["target_month"]
        return status
    projected = status["projected_target_month"]
    if not projected:
        raise ValueError("monthly contribution must be positive to extend")
    current_target = goal["target_month"]
    new_target = projected
    if current_target and projected <= current_target:
        new_target = current_target
    conn.execute("UPDATE goals SET target_month=? WHERE id=?", (new_target, goal_id))
    status["target_month"] = new_target
    return status


def _compact_note(note: dict[str, Any]) -> str:
    return json.dumps(note, separators=(",", ":"))


def _parse_note(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {
            "pool_before_cents": 0,
            "planned_cents": 0,
            "alloc_cents": 0,
            "sources": [],
        }
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "pool_before_cents": 0,
            "planned_cents": 0,
            "alloc_cents": 0,
            "sources": [],
        }
    if not isinstance(parsed, dict):
        return {
            "pool_before_cents": 0,
            "planned_cents": 0,
            "alloc_cents": 0,
            "sources": [],
        }
    parsed.setdefault("sources", [])
    return parsed


def _pool_buckets(conn: sqlite3.Connection, month: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT category_id, category_name, underspend_cents
        FROM v_category_underspend_monthly
        WHERE month = ?
        ORDER BY underspend_cents DESC, category_id ASC
        """,
        (month,),
    ).fetchall()
    return [
        {
            "category_id": int(row["category_id"]),
            "category_name": row["category_name"],
            "remaining_cents": int(row["underspend_cents"]),
        }
        for row in rows
    ]


def _close_participating_goals(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM goals g
        WHERE g.auto_fund = 1
          AND g.start_month <= ?
          AND (
            g.status = 'active'
            OR EXISTS (
              SELECT 1
              FROM goal_ledger gl
              WHERE gl.goal_id = g.id
                AND gl.month = ?
                AND gl.source = 'underspend'
                AND gl.status = 'applied'
            )
          )
        ORDER BY g.priority ASC, g.id ASC
        """,
        (month, month),
    ).fetchall()


def close_month(conn: sqlite3.Connection, month: str) -> list[dict[str, Any]]:
    """Allocate month underspend to active auto-funded goals, idempotently."""
    month = _validate_month(month)
    buckets = _pool_buckets(conn, month)
    bucket_by_category = {bucket["category_id"]: bucket for bucket in buckets}
    remaining_pool = sum(int(bucket["remaining_cents"]) for bucket in buckets)
    summaries: list[dict[str, Any]] = []

    for goal in _close_participating_goals(conn, month):
        existing = conn.execute(
            """
            SELECT *
            FROM goal_ledger
            WHERE goal_id = ?
              AND month = ?
              AND status = 'applied'
            """,
            (goal["id"], month),
        ).fetchone()
        if existing is not None and existing["source"] != "underspend":
            continue

        pool_before = remaining_pool
        contributed_other = _contributed_other(conn, int(goal["id"]), month)
        remaining_to_target = max(0, int(goal["target_cents"]) - contributed_other)
        monthly_contribution = int(goal["monthly_contribution_cents"])
        if existing is None:
            planned_cents = min(monthly_contribution, remaining_to_target)
            alloc_basis_cents = monthly_contribution
        else:
            planned_cents = int(existing["planned_cents"])
            alloc_basis_cents = planned_cents
        alloc_cents = min(alloc_basis_cents, remaining_pool, remaining_to_target)
        alloc_remaining = alloc_cents
        sources: list[dict[str, Any]] = []

        def draw(bucket: dict[str, Any]) -> None:
            nonlocal alloc_remaining, remaining_pool
            if alloc_remaining <= 0 or bucket["remaining_cents"] <= 0:
                return
            take = min(int(bucket["remaining_cents"]), alloc_remaining)
            bucket["remaining_cents"] -= take
            alloc_remaining -= take
            remaining_pool -= take
            sources.append(
                {
                    "category_id": bucket["category_id"],
                    "category_name": bucket["category_name"],
                    "cents": take,
                }
            )

        linked_category_id = goal["linked_category_id"]
        if linked_category_id is not None:
            preferred = bucket_by_category.get(int(linked_category_id))
            if preferred is not None:
                draw(preferred)
        for bucket in buckets:
            draw(bucket)
            if alloc_remaining <= 0:
                break

        note = _compact_note(
            {
                "pool_before_cents": pool_before,
                "planned_cents": planned_cents,
                "alloc_cents": alloc_cents,
                "sources": sources,
            }
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status, note)
                VALUES (?, ?, ?, ?, 'underspend', 'applied', ?)
                ON CONFLICT(goal_id, month) DO UPDATE SET
                  planned_cents=excluded.planned_cents,
                  actual_cents=excluded.actual_cents,
                  source=excluded.source,
                  status=excluded.status,
                  note=excluded.note
                """,
                (goal["id"], month, planned_cents, alloc_cents, note),
            )
        else:
            conn.execute(
                """
                UPDATE goal_ledger
                SET actual_cents=?,
                    note=?
                WHERE id=?
                """,
                (alloc_cents, note, existing["id"]),
            )

        total_after_close = contributed_other + alloc_cents
        if total_after_close >= int(goal["target_cents"]):
            conn.execute("UPDATE goals SET status='completed' WHERE id=?", (goal["id"],))
        else:
            conn.execute("UPDATE goals SET status='active' WHERE id=? AND status='completed'", (goal["id"],))
        summaries.append(
            {
                "goal_id": int(goal["id"]),
                "name": goal["name"],
                "planned_cents": planned_cents,
                "actual_cents": alloc_cents,
                "pool_before_cents": pool_before,
                "sources": sources,
                "remaining_to_target_cents": max(0, remaining_to_target - alloc_cents),
            }
        )
    return summaries


def _progress_width(pct_complete: float) -> str:
    if pct_complete <= 0:
        return "0%"
    return f"{min(100.0, max(3.0, pct_complete)):.1f}%"


def goal_progress_rows(conn: sqlite3.Connection, as_of_month: str | None = None) -> list[dict[str, Any]]:
    as_of_month = _validate_month(as_of_month or _today_month())
    rows = conn.execute(
        """
        SELECT
          gp.*,
          g.auto_fund,
          g.priority,
          g.linked_transaction_id,
          g.linked_category_id,
          g.notes
        FROM v_goal_progress gp
        JOIN goals g ON g.id = gp.goal_id
        ORDER BY
          CASE gp.status
            WHEN 'active' THEN 0
            WHEN 'paused' THEN 1
            WHEN 'completed' THEN 2
            ELSE 3
          END,
          g.priority ASC,
          gp.goal_id ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["pct_width"] = _progress_width(float(item["pct_complete"] or 0))
        contributed_as_of = _contributed_through(conn, int(item["goal_id"]), as_of_month)
        item["payoff"] = payoff_status(
            target_cents=item["target_cents"],
            contributed_cents=contributed_as_of,
            monthly_contribution_cents=item["monthly_contribution_cents"],
            start_month=item["start_month"],
            target_month=item["target_month"],
            as_of_month=as_of_month,
            as_of_funded=_has_applied_row(conn, int(item["goal_id"]), as_of_month),
        )
        out.append(item)
    return out


def goal_form_options(conn: sqlite3.Connection, *, include_transaction_id: int | None = None) -> dict[str, Any]:
    if include_transaction_id is None:
        transactions = conn.execute(
            """
            SELECT id, posted_on, description, counterparty, amount_cents
            FROM transactions
            ORDER BY posted_on DESC, id DESC
            LIMIT 80
            """
        ).fetchall()
    else:
        transactions = conn.execute(
            """
            WITH recent AS (
              SELECT id, posted_on, description, counterparty, amount_cents
              FROM transactions
              ORDER BY posted_on DESC, id DESC
              LIMIT 80
            ),
            included AS (
              SELECT id, posted_on, description, counterparty, amount_cents
              FROM transactions
              WHERE id = ?
            )
            SELECT * FROM recent
            UNION
            SELECT * FROM included
            ORDER BY posted_on DESC, id DESC
            """,
            (include_transaction_id,),
        ).fetchall()
    return {
        "categories": conn.execute(
            """
            SELECT id, name, color
            FROM categories
            WHERE kind = 'expense'
            ORDER BY name
            """
        ).fetchall(),
        "transactions": transactions,
    }


def close_review_rows(conn: sqlite3.Connection, month: str) -> list[dict[str, Any]]:
    month = _validate_month(month)
    rows = conn.execute(
        """
        SELECT
          g.*,
          gl.planned_cents,
          gl.actual_cents,
          gl.note,
          gl.status AS ledger_status
        FROM goals g
        LEFT JOIN goal_ledger gl
          ON gl.goal_id = g.id
         AND gl.month = ?
        WHERE g.auto_fund = 1
          AND g.start_month <= ?
          AND (
            g.status = 'active'
            OR EXISTS (
              SELECT 1
              FROM goal_ledger existing
              WHERE existing.goal_id = g.id
                AND existing.month = ?
                AND existing.source = 'underspend'
                AND existing.status = 'applied'
            )
          )
        ORDER BY g.priority ASC, g.id ASC
        """,
        (month, month, month),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payoff"] = payoff_status(
            target_cents=row["target_cents"],
            contributed_cents=_contributed_through(conn, int(row["id"]), month),
            monthly_contribution_cents=row["monthly_contribution_cents"],
            start_month=row["start_month"],
            target_month=row["target_month"],
            as_of_month=month,
            as_of_funded=_has_applied_row(conn, int(row["id"]), month),
        )
        item["note_json"] = _parse_note(row["note"])
        if row["planned_cents"] is None:
            remaining = max(0, int(row["target_cents"]) - _contributed_other(conn, int(row["id"]), month))
            item["planned_cents"] = min(int(row["monthly_contribution_cents"]), remaining)
            item["actual_cents"] = 0
        out.append(item)
    return out
