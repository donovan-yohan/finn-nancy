"""The month-close checklist read-model.

Most incomplete rows are warn-not-block signals. Unknown transaction flow
meaning and purchase/fee splits without active accepted category evidence are
different: those rows cannot support a clean close or a trusted category
breakdown.
"""
from __future__ import annotations

import sqlite3

from ..db import repo_goals, repo_statement_expectations


def _coverage_row(conn: sqlite3.Connection, month: str) -> dict:
    matrix = repo_statement_expectations.period_matrix(conn, month)
    required = [
        row for row in matrix if row["requirement_state"] == "required"
    ]
    reconciled = [
        row for row in required if row["lifecycle_state"] == "reconciled"
    ]
    unconfigured = [
        row for row in matrix if row["requirement_state"] == "unconfigured"
    ]
    blockers = [
        row
        for row in matrix
        if row["requirement_state"] == "unconfigured"
        or (
            row["requirement_state"] == "required"
            and row["lifecycle_state"] != "reconciled"
        )
    ]
    denominator = len(required) + len(unconfigured)
    coverage_pct = (
        round(100.0 * len(reconciled) / denominator, 1)
        if denominator
        else 100.0
    )
    complete = not blockers
    document_count = sum(int(row["document_count"]) for row in matrix)
    if unconfigured:
        detail = (
            f"{len(unconfigured)} account(s) need statement policy configuration; "
            f"{len(required) - len(reconciled)} required period(s) remain open"
        )
    elif not required:
        detail = "no statements required for this account-period matrix"
    elif complete:
        detail = "every required account statement is reconciled"
    elif document_count == 0:
        detail = "no statement evidence uploaded"
    else:
        detail = (
            f"{len(required) - len(reconciled)} required account statement(s) "
            "not reconciled"
        )
    return {
        "key": "coverage",
        "label": "Statement coverage",
        "href": f"/recon?month={month}",
        "complete": complete,
        "count": len(blockers),
        "coverage_pct": coverage_pct,
        "required_count": len(required),
        "reconciled_count": len(reconciled),
        "unconfigured_count": len(unconfigured),
        "detail": detail,
    }


def _expense_resolution_row(conn: sqlite3.Connection, month: str) -> dict:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS split_count,
          COUNT(DISTINCT transaction_id) AS transaction_count,
          COALESCE(SUM(ABS(split_amount_cents)), 0) AS excluded_cents,
          SUM(CASE WHEN category_name='Uncategorized' THEN 1 ELSE 0 END)
            AS uncategorized_count
        FROM v_expense_resolution_status
        WHERE resolution_status='unresolved'
          AND posted_on >= ?
          AND posted_on < date(?, '+1 month')
        """,
        (f"{month}-01", f"{month}-01"),
    ).fetchone()
    count = int(row["split_count"])
    transaction_count = int(row["transaction_count"])
    complete = count == 0
    return {
        "key": "backlog",
        "label": "Expense categories confirmed",
        "href": "/backlog",
        "complete": complete,
        "blocking": not complete,
        "count": count,
        "transaction_count": transaction_count,
        "excluded_cents": int(row["excluded_cents"]),
        "uncategorized_count": int(row["uncategorized_count"] or 0),
        "detail": (
            "every purchase and fee has a confirmed category"
            if complete
            else (
                f"{count} purchase/fee split(s) across {transaction_count} "
                "transaction(s) still need a confirmed category"
            )
        ),
    }


def _flow_review_row(conn: sqlite3.Connection, month: str) -> dict:
    transaction_rows = conn.execute(
        """
        SELECT
          txn.id AS transaction_id,
          flow_status.semantic_status,
          CASE WHEN review.transaction_id IS NULL THEN 1 ELSE 0 END
            AS missing_review
        FROM v_transaction_flow_status flow_status
        JOIN transactions txn ON txn.id=flow_status.transaction_id
        LEFT JOIN transaction_flow_reviews review
          ON review.transaction_id = txn.id
         AND review.status = 'pending'
        WHERE flow_status.semantic_status <> 'complete'
          AND txn.posted_on >= ?
          AND txn.posted_on < date(?, '+1 month')
        """,
        (f"{month}-01", f"{month}-01"),
    ).fetchall()
    positive_line_rows = conn.execute(
        """
        SELECT
          line.id AS statement_line_id,
          line.match_status,
          line.matched_transaction_id,
          COALESCE(flow_status.semantic_status, 'unknown') AS semantic_status,
          CASE
            WHEN line.matched_transaction_id IS NOT NULL
              AND review.transaction_id IS NULL
            THEN 1 ELSE 0
          END AS missing_review
        FROM statement_lines line
        LEFT JOIN transactions txn ON txn.id=line.matched_transaction_id
        LEFT JOIN v_transaction_flow_status flow_status
          ON flow_status.transaction_id=txn.id
        LEFT JOIN transaction_flow_reviews review
          ON review.transaction_id=txn.id AND review.status='pending'
        LEFT JOIN statement_reviews statement_review
          ON statement_review.source_document_id=line.source_document_id
        LEFT JOIN statement_expectation_documents expectation_link
          ON expectation_link.source_document_id=line.source_document_id
         AND expectation_link.status='active'
        LEFT JOIN account_statement_expectations expectation
          ON expectation.id=expectation_link.expectation_id
        WHERE line.review_disposition='active'
          AND line.amount_cents > 0
          AND COALESCE(
            expectation.period_month,
            statement_review.period_month,
            line.statement_period,
            substr(line.posted_on, 1, 7)
          )=?
          AND NOT (
            line.match_status IN ('matched', 'promoted')
            AND flow_status.semantic_status='complete'
          )
        ORDER BY line.id
        """,
        (month,),
    ).fetchall()

    blockers: dict[tuple[str, int], tuple[str, int]] = {}
    for row in transaction_rows:
        blockers[("transaction", int(row["transaction_id"]))] = (
            str(row["semantic_status"]),
            int(row["missing_review"]),
        )
    for row in positive_line_rows:
        transaction_id = row["matched_transaction_id"]
        key = (
            ("transaction", int(transaction_id))
            if transaction_id is not None
            else ("statement_line", int(row["statement_line_id"]))
        )
        blockers[key] = (
            str(row["semantic_status"]),
            int(row["missing_review"]),
        )

    count = len(blockers)
    unknown_count = sum(
        status == "unknown" for status, _missing_review in blockers.values()
    )
    missing_relationship_count = sum(
        status == "missing_relationship"
        for status, _missing_review in blockers.values()
    )
    missing_review_count = sum(
        status == "unknown" and missing_review
        for status, missing_review in blockers.values()
    )
    positive_line_count = len(positive_line_rows)
    intake_line_id = next(
        (
            int(row["statement_line_id"])
            for row in positive_line_rows
            if row["matched_transaction_id"] is None
            or str(row["match_status"]) not in ("matched", "promoted")
        ),
        None,
    )
    complete = count == 0 and missing_review_count == 0
    return {
        "key": "flow_review",
        "label": "Transaction meaning",
        "href": (
            (
                f"/recon?month={month}#positive-intake-{intake_line_id}"
                if intake_line_id is not None
                else f"/recon?month={month}#positive-flow-review"
            )
        ),
        "complete": complete,
        "blocking": not complete,
        "count": count,
        "unknown_count": unknown_count,
        "missing_relationship_count": missing_relationship_count,
        "missing_review_count": missing_review_count,
        "positive_line_count": positive_line_count,
        "detail": (
            "every transaction has an accounting flow"
            if complete
            else (
                f"{count} transaction(s) need flow/provenance review and are excluded "
                "from totals"
            )
        ),
    }


def _variance_row(conn: sqlite3.Connection, month: str) -> dict:
    rows = conn.execute(
        "SELECT remaining_cents FROM v_budget_vs_actual WHERE month = ?",
        (month,),
    ).fetchall()
    overspend_cents = sum(max(0, -int(r["remaining_cents"])) for r in rows)
    over_categories = sum(1 for r in rows if int(r["remaining_cents"]) < 0)
    complete = overspend_cents == 0
    return {
        "key": "variance",
        "label": "Budget variance",
        "href": "/insights",
        "complete": complete,
        "count": over_categories,
        "overspend_cents": overspend_cents,
        "detail": (
            "no category over budget"
            if complete
            else f"{over_categories} category(ies) over budget — acknowledge to close"
        ),
    }


def _goal_row(conn: sqlite3.Connection, month: str) -> dict:
    rows = repo_goals.close_review_rows(conn, month)
    behind = sum(1 for r in rows if r["payoff"]["state"] == "behind")
    complete = behind == 0
    return {
        "key": "goals",
        "label": "Goal funding",
        "href": f"/goals/close?month={month}",
        "complete": complete,
        "count": behind,
        "detail": (
            "all auto-funded goals on pace"
            if complete
            else f"{behind} goal(s) behind pace"
        ),
    }


def _net_delta_cents(conn: sqlite3.Connection, month: str) -> int:
    row = conn.execute(
        "SELECT net_cents FROM v_cashflow_monthly WHERE month = ?",
        (month,),
    ).fetchone()
    return int(row["net_cents"]) if row else 0


def build_checklist(conn: sqlite3.Connection, month: str) -> dict:
    """Return the close checklist plus roll-up metrics for ``month``."""
    coverage = _coverage_row(conn, month)
    flow_review = _flow_review_row(conn, month)
    backlog = _expense_resolution_row(conn, month)
    variance = _variance_row(conn, month)
    goals = _goal_row(conn, month)
    rows = [coverage, flow_review, backlog, variance, goals]

    complete_count = sum(1 for r in rows if r["complete"])
    total = len(rows)
    return {
        "rows": rows,
        "complete_count": complete_count,
        "total": total,
        "percent_complete": round(100 * complete_count / total),
        "all_complete": complete_count == total,
        "coverage_pct": coverage["coverage_pct"],
        "statement_blocker_count": coverage["count"],
        "has_blocking_statement_expectations": not coverage["complete"],
        "flow_semantic_review_count": flow_review["count"],
        "has_blocking_flow_semantics": not flow_review["complete"],
        "flow_review_count": flow_review["count"],
        "has_blocking_flow_reviews": not flow_review["complete"],
        "expense_resolution_count": backlog["count"],
        "expense_resolution_transaction_count": backlog["transaction_count"],
        "expense_resolution_excluded_cents": backlog["excluded_cents"],
        "has_blocking_expense_resolutions": not backlog["complete"],
        # Compatibility for the current closed_periods column and legacy
        # snapshots. Assigned-but-unconfirmed categories are tracked separately
        # above instead of being mislabeled Uncategorized.
        "uncategorized_count": backlog["uncategorized_count"],
        "overspend_cents": variance["overspend_cents"],
        "has_overspend": not variance["complete"],
        "net_delta_cents": _net_delta_cents(conn, month),
    }
