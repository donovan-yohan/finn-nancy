"""Read queries over the reporting views, plus dashboard data assembly.

Ports the Go portal's ``loadDashboardData`` exactly: same views, same ordering,
same width/stat math, so the Python dashboard renders at parity.
"""
from __future__ import annotations

import sqlite3

_CASHFLOW_SQL = """
    SELECT month, income_cents, expense_cents, net_cents
    FROM v_cashflow_monthly
    ORDER BY month
"""

_CATEGORY_TOTALS_SQL = """
    SELECT category_name, category_kind, brand_owner, color,
           total_cents, magnitude_cents, pct_of_total
    FROM v_report_category_totals
    ORDER BY magnitude_cents DESC, category_name
"""

_RECENT_TX_SQL = """
    SELECT id, posted_on, account_name, description, counterparty,
           amount_cents, flow_kind, semantic_status, semantic_reason,
           COALESCE(categories, '') AS categories
    FROM v_transactions_recent
    LIMIT ?
"""

ACTIVITY_PAGE_SIZE = 50

_ACTIVITY_ACCOUNTS_SQL = "SELECT id, name FROM accounts ORDER BY name"
_ACTIVITY_CATEGORIES_SQL = "SELECT id, name FROM categories ORDER BY name"


def _int_or_none(raw: str | int | None) -> int | None:
    """Coerce a query-param value to int, treating blank/garbage as absent."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so search treats ``%``/``_`` as literal characters.

    Pairs with ``ESCAPE '\\'`` on the LIKE predicate; the backslash itself is
    escaped first so it can act as the escape character.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_cursor(cursor: str) -> tuple[str, int] | None:
    """Decode a ``posted_on:id`` keyset cursor; ignore anything malformed."""
    if not cursor:
        return None
    posted, sep, raw_id = cursor.rpartition(":")
    if not sep or not posted or not raw_id.isdigit():
        return None
    return posted, int(raw_id)


def activity_results(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    account_id: str | int | None = "",
    category_id: str | int | None = "",
    date_from: str = "",
    date_to: str = "",
    flow_kind: str = "",
    semantic_review: bool = False,
    cursor: str = "",
    limit: int = ACTIVITY_PAGE_SIZE,
) -> dict:
    """Filtered, keyset-paginated activity rows.

    Ordered newest-first by ``(posted_on, id)`` — the same order as
    ``v_transactions_recent`` — so the cursor is a stable ``posted_on:id`` pair.
    Fetches one extra row to decide whether a next page exists.
    """
    where: list[str] = []
    params: list[object] = []

    q = (q or "").strip()
    if q:
        like = f"%{_escape_like(q)}%"
        where.append(
            "(t.description LIKE ? ESCAPE '\\' OR t.counterparty LIKE ? ESCAPE '\\')"
        )
        params += [like, like]

    acct = _int_or_none(account_id)
    if acct is not None:
        where.append("t.account_id = ?")
        params.append(acct)

    cat = _int_or_none(category_id)
    if cat is not None:
        where.append(
            "EXISTS (SELECT 1 FROM transaction_splits sc "
            "WHERE sc.transaction_id = t.id AND sc.category_id = ?)"
        )
        params.append(cat)

    date_from = (date_from or "").strip()
    if date_from:
        where.append("t.posted_on >= ?")
        params.append(date_from)

    date_to = (date_to or "").strip()
    if date_to:
        where.append("t.posted_on <= ?")
        params.append(date_to)

    flow_kind = (flow_kind or "").strip()
    if flow_kind:
        where.append("t.flow_kind = ?")
        params.append(flow_kind)

    if semantic_review:
        where.append("flow_status.semantic_status <> 'complete'")

    parsed = _parse_cursor(cursor)
    if parsed:
        cposted, cid = parsed
        where.append("(t.posted_on < ? OR (t.posted_on = ? AND t.id < ?))")
        params += [cposted, cposted, cid]

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT t.id, t.posted_on, a.name AS account_name, t.description, t.counterparty,
               t.amount_cents, t.flow_kind, flow_status.semantic_status,
               flow_status.semantic_reason,
               COALESCE(GROUP_CONCAT(c.name, ', '), '') AS categories
        FROM transactions t
        JOIN v_transaction_flow_status flow_status
          ON flow_status.transaction_id=t.id
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN transaction_splits s ON s.transaction_id = t.id
        LEFT JOIN categories c ON c.id = s.category_id
        {clause}
        GROUP BY t.id
        ORDER BY t.posted_on DESC, t.id DESC
        LIMIT ?
    """
    rows = [dict(r) for r in conn.execute(sql, (*params, limit + 1)).fetchall()]

    next_cursor = ""
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = f"{last['posted_on']}:{last['id']}"

    return {"transactions": rows, "next_cursor": next_cursor}


def filter_options(conn: sqlite3.Connection) -> dict:
    """Account and category option lists for the activity filter controls."""
    return {
        "accounts": [dict(r) for r in conn.execute(_ACTIVITY_ACCOUNTS_SQL).fetchall()],
        "filter_categories": [
            dict(r) for r in conn.execute(_ACTIVITY_CATEGORIES_SQL).fetchall()
        ],
    }


def _pct_width(value: int, mx: int) -> str:
    """Bar width as a CSS percentage; tiny non-zero bars get a 3% floor (matches Go)."""
    if mx <= 0:
        mx = 1
    pct = 100 * value // mx
    if value > 0 and pct < 3:
        pct = 3
    return f"{pct}%"


def dashboard_context(conn: sqlite3.Connection, tx_limit: int, db_path: str) -> dict:
    cashflow_rows = conn.execute(_CASHFLOW_SQL).fetchall()

    mx = 1
    for r in cashflow_rows:
        mx = max(mx, r["income_cents"], r["expense_cents"])

    cashflow = [
        {
            "month": r["month"],
            "income_cents": r["income_cents"],
            "expense_cents": r["expense_cents"],
            "net_cents": r["net_cents"],
            "income_width": _pct_width(r["income_cents"], mx),
            "expense_width": _pct_width(r["expense_cents"], mx),
            "net_class": "negative" if r["net_cents"] < 0 else "positive",
        }
        for r in cashflow_rows
    ]

    stats = {
        "money_in_cents": sum(c["income_cents"] for c in cashflow),
        "money_out_cents": sum(c["expense_cents"] for c in cashflow),
        "net_cents": sum(c["net_cents"] for c in cashflow),
        "months_tracked": len(cashflow),
    }
    stats["net_class"] = "negative" if stats["net_cents"] < 0 else "positive"

    latest_net = cashflow[-1]["net_cents"] if cashflow else 0
    semantic_review_count = int(
        conn.execute(
            """SELECT COUNT(*)
               FROM v_transaction_flow_status
               WHERE semantic_status <> 'complete'"""
        ).fetchone()[0]
    )

    return {
        "db_path": db_path,
        "cashflow": cashflow,
        "stats": stats,
        "latest_net_cents": latest_net,
        "semantic_review_count": semantic_review_count,
        "categories": [dict(r) for r in conn.execute(_CATEGORY_TOTALS_SQL).fetchall()],
        "transactions": [dict(r) for r in conn.execute(_RECENT_TX_SQL, (tx_limit,)).fetchall()],
    }
