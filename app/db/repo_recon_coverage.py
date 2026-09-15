"""Read models for statement reconciliation coverage."""
from __future__ import annotations

import sqlite3


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _coverage_pct(covered: int, unmatched: int) -> float:
    denominator = covered + unmatched
    return round((100.0 * covered / denominator), 1) if denominator else 0.0


def _scoped_cte(month: str | None) -> tuple[str, tuple]:
    if month is None:
        return (
            "WITH scoped_statement_coverage AS "
            "(SELECT * FROM v_statement_coverage_lines)",
            (),
        )
    return (
        """WITH scoped_statement_coverage AS (
             SELECT coverage.*
             FROM v_statement_coverage_lines coverage
             JOIN statement_expectation_documents link
               ON link.source_document_id=coverage.source_document_id
              AND link.status='active'
             JOIN account_statement_expectations expectation
               ON expectation.id=link.expectation_id
             WHERE expectation.period_month=?
           )""",
        (month,),
    )


def coverage_dashboard(
    conn: sqlite3.Connection, *, month: str | None = None
) -> dict:
    cte, args = _scoped_cte(month)
    summary_row = conn.execute(
        cte
        + """
        SELECT
          COUNT(*) AS line_count,
          COALESCE(SUM(spend_cents), 0) AS statement_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='covered' THEN spend_cents ELSE 0 END), 0) AS covered_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='unmatched' THEN spend_cents ELSE 0 END), 0) AS unmatched_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='ignored' THEN spend_cents ELSE 0 END), 0) AS ignored_spend_cents,
          COALESCE(SUM(income_cents), 0) AS income_cents,
          COALESCE(SUM(positive_review_cents), 0) AS positive_review_cents,
          COALESCE(SUM(CASE WHEN attention_reason <> '' AND attention_reason <> 'ignored/internal transfer'
                            THEN spend_cents ELSE 0 END), 0) AS attention_spend_cents
        FROM scoped_statement_coverage
        """,
        args,
    ).fetchone()
    summary = _row_to_dict(summary_row)
    summary["coverage_pct"] = _coverage_pct(
        int(summary["covered_spend_cents"]),
        int(summary["unmatched_spend_cents"]),
    )

    reason_rows = conn.execute(
        cte
        + """
        SELECT
          attention_reason AS reason,
          COUNT(*) AS line_count,
          SUM(spend_cents) AS spend_cents,
          SUM(positive_review_cents) AS positive_review_cents
        FROM scoped_statement_coverage
        WHERE attention_reason <> ''
          AND attention_reason <> 'ignored/internal transfer'
        GROUP BY attention_reason
        ORDER BY spend_cents DESC, line_count DESC, reason
        """,
        args,
    ).fetchall()

    doc_rows = conn.execute(
        cte
        + """
        SELECT
          source_document_id,
          document_name,
          document_status,
          MIN(posted_on) AS first_posted_on,
          MAX(posted_on) AS last_posted_on,
          COUNT(*) AS line_count,
          COALESCE(SUM(spend_cents), 0) AS statement_spend_cents,
          COALESCE(SUM(
            CASE WHEN coverage_bucket='covered' THEN spend_cents ELSE 0 END
          ), 0) AS covered_spend_cents,
          COALESCE(SUM(
            CASE WHEN coverage_bucket='unmatched' THEN spend_cents ELSE 0 END
          ), 0) AS unmatched_spend_cents,
          COALESCE(SUM(
            CASE WHEN coverage_bucket='ignored' THEN spend_cents ELSE 0 END
          ), 0) AS ignored_spend_cents,
          COALESCE(SUM(income_cents), 0) AS income_cents,
          COALESCE(SUM(positive_review_cents), 0) AS positive_review_cents,
          COALESCE(SUM(
            CASE WHEN attention_reason <> ''
                       AND attention_reason <> 'ignored/internal transfer'
                 THEN spend_cents ELSE 0 END
          ), 0) AS attention_spend_cents
        FROM scoped_statement_coverage
        GROUP BY source_document_id, document_name, document_status
        ORDER BY last_posted_on DESC, source_document_id DESC
        """,
        args,
    ).fetchall()
    documents = [_row_to_dict(row) for row in doc_rows]
    for document in documents:
        document["coverage_pct"] = _coverage_pct(
            int(document["covered_spend_cents"]),
            int(document["unmatched_spend_cents"]),
        )
    month_rows = (
        _breakdown(conn, "month", "month")
        if month is None
        else [
            {
                "label": month,
                "line_count": int(summary["line_count"]),
                "statement_spend_cents": int(summary["statement_spend_cents"]),
                "covered_spend_cents": int(summary["covered_spend_cents"]),
                "unmatched_spend_cents": int(summary["unmatched_spend_cents"]),
                "ignored_spend_cents": int(summary["ignored_spend_cents"]),
                "income_cents": int(summary["income_cents"]),
                "positive_review_cents": int(
                    summary["positive_review_cents"]
                ),
                "coverage_pct": float(summary["coverage_pct"]),
            }
        ]
    )

    return {
        "summary": summary,
        "reasons": [_row_to_dict(row) for row in reason_rows],
        "documents": documents,
        "months": month_rows,
        "accounts": _breakdown(
            conn, "account_name", "account_name", month=month
        ),
        "categories": _category_breakdown(conn, month=month),
        "merchants": _breakdown(
            conn, "norm_merchant", "norm_merchant", limit=8, month=month
        ),
        "attention_lines": attention_lines(conn, limit=8, month=month),
    }


def _breakdown(
    conn: sqlite3.Connection,
    select_expr: str,
    group_expr: str,
    *,
    limit: int | None = None,
    month: str | None = None,
) -> list[dict]:
    cte, args = _scoped_cte(month)
    sql = cte + f"""
        SELECT
          {select_expr} AS label,
          COUNT(*) AS line_count,
          COALESCE(SUM(spend_cents), 0) AS statement_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='covered' THEN spend_cents ELSE 0 END), 0) AS covered_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='unmatched' THEN spend_cents ELSE 0 END), 0) AS unmatched_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='ignored' THEN spend_cents ELSE 0 END), 0) AS ignored_spend_cents,
          COALESCE(SUM(income_cents), 0) AS income_cents,
          COALESCE(SUM(positive_review_cents), 0) AS positive_review_cents
        FROM scoped_statement_coverage
        GROUP BY {group_expr}
        HAVING statement_spend_cents <> 0
            OR income_cents <> 0
            OR positive_review_cents <> 0
        ORDER BY statement_spend_cents DESC,
                 positive_review_cents DESC, income_cents DESC, label
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for row in rows:
        item = _row_to_dict(row)
        item["coverage_pct"] = _coverage_pct(
            int(item["covered_spend_cents"]),
            int(item["unmatched_spend_cents"]),
        )
        out.append(item)
    return out


def _category_breakdown(
    conn: sqlite3.Connection, *, month: str | None = None
) -> list[dict]:
    cte, args = _scoped_cte(month)
    rows = conn.execute(
        cte
        + """
        SELECT
          CASE
            WHEN category_names <> '' THEN category_names
            WHEN coverage_bucket='unmatched' THEN 'Unmatched / needs review'
            WHEN coverage_bucket='ignored' THEN 'Ignored / transfer'
            WHEN coverage_bucket='income' THEN 'Income / deposits'
            WHEN coverage_bucket='flow_review' THEN 'Flow meaning / needs review'
            WHEN coverage_bucket='resolved_positive' THEN 'Resolved money in'
            ELSE 'Uncategorized'
          END AS label,
          COUNT(*) AS line_count,
          COALESCE(SUM(spend_cents), 0) AS statement_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='covered' THEN spend_cents ELSE 0 END), 0) AS covered_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='unmatched' THEN spend_cents ELSE 0 END), 0) AS unmatched_spend_cents,
          COALESCE(SUM(CASE WHEN coverage_bucket='ignored' THEN spend_cents ELSE 0 END), 0) AS ignored_spend_cents,
          COALESCE(SUM(income_cents), 0) AS income_cents,
          COALESCE(SUM(positive_review_cents), 0) AS positive_review_cents
        FROM scoped_statement_coverage
        GROUP BY label
        HAVING statement_spend_cents <> 0
            OR income_cents <> 0
            OR positive_review_cents <> 0
        ORDER BY statement_spend_cents DESC,
                 positive_review_cents DESC, income_cents DESC, label
        """,
        args,
    ).fetchall()
    out = []
    for row in rows:
        item = _row_to_dict(row)
        item["coverage_pct"] = _coverage_pct(
            int(item["covered_spend_cents"]),
            int(item["unmatched_spend_cents"]),
        )
        out.append(item)
    return out


def attention_lines(
    conn: sqlite3.Connection,
    *,
    limit: int = 25,
    month: str | None = None,
) -> list[dict]:
    cte, args = _scoped_cte(month)
    rows = conn.execute(
        cte
        + """
        SELECT *
        FROM scoped_statement_coverage
        WHERE attention_reason <> ''
          AND attention_reason <> 'ignored/internal transfer'
        ORDER BY MAX(spend_cents, positive_review_cents) DESC,
                 posted_on DESC, line_id DESC
        LIMIT ?
        """,
        (*args, limit),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]
