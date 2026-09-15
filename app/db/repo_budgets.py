"""Budget settings and SQL-backed insight read models."""
from __future__ import annotations

import sqlite3
import re

DEFAULT_BIG_TICKET_THRESHOLD_CENTS = 30_000
SUBSCRIPTION_WATCHLIST_DECISIONS = {
    "subscription": "marked subscription",
    "not_subscription": "not a subscription",
    "already_known": "already known",
    "watch_next_month": "watch next month",
}
MAX_UNMATCHED_CARD_LINES = 8
BUDGET_TRANSACTION_EVIDENCE_LIMIT = 12
TREND_TRANSACTION_EVIDENCE_LIMIT = 6


def _slugify_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "member"


def _setting_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return default


def big_ticket_threshold_cents(conn: sqlite3.Connection) -> int:
    return _setting_int(conn, "big_ticket_threshold_cents", DEFAULT_BIG_TICKET_THRESHOLD_CENTS)


def set_big_ticket_threshold_cents(conn: sqlite3.Connection, cents: int) -> None:
    if cents < 0:
        raise ValueError("threshold must be non-negative")
    conn.execute(
        """
        INSERT INTO app_settings(key, value, updated_at)
        VALUES ('big_ticket_threshold_cents', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
          value=excluded.value,
          updated_at=CURRENT_TIMESTAMP
        """,
        (str(cents),),
    )


def set_category_budget(
    conn: sqlite3.Connection,
    *,
    category_id: int,
    amount_cents: int,
    owner_member_id: int | None = None,
    period_month: str = "",
) -> None:
    row = conn.execute(
        "SELECT id FROM categories WHERE id=? AND kind='expense'", (category_id,)
    ).fetchone()
    if row is None:
        raise LookupError("expense category not found")
    if amount_cents < 0:
        raise ValueError("budget must be non-negative")
    legacy_owner = "shared"
    if owner_member_id is not None:
        owner_row = conn.execute(
            "SELECT slug FROM household_members WHERE id=? AND is_active=1",
            (owner_member_id,),
        ).fetchone()
        if owner_row is None:
            raise ValueError("invalid budget owner")
        if owner_row["slug"] in ("sample_member_a", "sample_member_b"):
            legacy_owner = owner_row["slug"]
    conn.execute(
        """
        INSERT INTO budgets(category_id, period_month, amount_cents, owner, owner_member_id, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(category_id, period_month) DO UPDATE SET
          amount_cents=excluded.amount_cents,
          owner=excluded.owner,
          owner_member_id=excluded.owner_member_id,
          updated_at=CURRENT_TIMESTAMP
        """,
        (category_id, period_month, amount_cents, legacy_owner, owner_member_id),
    )


def set_category_leisure(conn: sqlite3.Connection, *, category_id: int, is_leisure: bool) -> None:
    cur = conn.execute(
        "UPDATE categories SET is_leisure=? WHERE id=? AND kind='expense'",
        (1 if is_leisure else 0, category_id),
    )
    if cur.rowcount == 0:
        raise LookupError("expense category not found")


def create_household_member(
    conn: sqlite3.Connection,
    *,
    name: str,
    source: str = "manual",
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    if source not in {"manual", "seed", "account", "statement"}:
        raise ValueError("invalid member source")
    base_slug = _slugify_name(name)
    slug = base_slug
    suffix = 2
    while conn.execute("SELECT 1 FROM household_members WHERE slug=?", (slug,)).fetchone():
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    cur = conn.execute(
        "INSERT INTO household_members(name, slug, source) VALUES (?, ?, ?)",
        (name, slug, source),
    )
    return int(cur.lastrowid)


def household_members(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, name, slug, source
        FROM household_members
        WHERE is_active=1
        ORDER BY name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def available_months(conn: sqlite3.Connection) -> list[str]:
    """Ledger-derived months for planning surfaces and defaults."""
    return [
        r["month"]
        for r in conn.execute(
            """
            SELECT month
            FROM v_month_spine
            ORDER BY month DESC
            """
        ).fetchall()
    ]


def reconciliation_months(conn: sqlite3.Connection) -> list[str]:
    """Months visible to reconciliation, including fresh statement-only imports."""
    return [
        r["month"]
        for r in conn.execute(
            """
            SELECT month
            FROM v_month_spine
            UNION
            SELECT month
            FROM v_statement_coverage_lines
            ORDER BY month DESC
            """
        ).fetchall()
    ]


def default_month(conn: sqlite3.Connection) -> str:
    months = available_months(conn)
    return months[0] if months else ""


def budget_management_context(conn: sqlite3.Connection) -> dict:
    month = default_month(conn)
    rows = conn.execute(
        """
        SELECT
          c.id AS category_id,
          c.name AS category_name,
          c.brand_owner,
          c.color,
          c.is_leisure,
          COALESCE(b.amount_cents, 0) AS budget_cents,
          b.owner_member_id,
          COALESCE(hm.name,
            CASE
              WHEN b.owner = 'sample_member_a' THEN 'Sample Member A'
              WHEN b.owner = 'sample_member_b' THEN 'Sample Member B'
              ELSE 'Shared'
            END
          ) AS budget_owner,
          COALESCE(a.actual_cents, 0) AS latest_actual_cents
        FROM categories c
        LEFT JOIN budgets b
          ON b.category_id = c.id
         AND b.period_month = ''
        LEFT JOIN household_members hm
          ON hm.id = b.owner_member_id
        LEFT JOIN (
          SELECT category_id, SUM(magnitude_cents) AS actual_cents
          FROM v_expense_classified
          WHERE month = ?
          GROUP BY category_id
        ) a ON a.category_id = c.id
        WHERE c.kind = 'expense'
        ORDER BY c.brand_owner = 'nancy' DESC, c.name
        """,
        (month,),
    ).fetchall()
    return {
        "brand": "nancy",
        "active": "budget",
        "selected_month": month,
        "budget_rows": [dict(r) for r in rows],
        "household_members": household_members(conn),
        "big_ticket_threshold_cents": big_ticket_threshold_cents(conn),
    }


def _progress_width(actual_cents: int, budget_cents: int) -> str:
    if budget_cents <= 0:
        return "3%" if actual_cents > 0 else "0%"
    pct = min(100, int(round(100 * actual_cents / budget_cents)))
    if actual_cents > 0 and pct < 3:
        pct = 3
    return f"{pct}%"


def _share_width(value: int, total: int) -> str:
    if value <= 0 or total <= 0:
        return "0%"
    pct = max(3, int(round(100 * value / total)))
    return f"{min(100, pct)}%"


def _money(cents: int | None) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _split_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _unique_ids(ids: list[str]) -> list[str]:
    seen = set()
    out = []
    for raw_id in ids:
        item = str(raw_id).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    label = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {label}"


def recurring_delta_rows(conn: sqlite3.Connection, month: str) -> list[dict]:
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT *
            FROM v_recurring_payment_deltas
            WHERE month = ?
              AND is_meaningful_delta = 1
            ORDER BY ABS(amount_delta_cents) DESC, merchant
            LIMIT 8
            """,
            (month,),
        ).fetchall()
    ]
    for row in rows:
        row["delta_class"] = "negative" if row["amount_delta_cents"] > 0 else "positive"
        row["direction_label"] = "increased" if row["amount_delta_cents"] > 0 else "decreased"
    return rows


PLANNING_CARD_ACTIONS = {
    "accepted": "accepted",
    "dismissed": "dismissed",
    "snoozed": "snoozed",
}
DEFAULT_PLANNING_CARD_ACTION_LABEL = "needs review"


def _build_card_key(
    merchant: str,
    account_id: int,
    previous_month: str,
    month: str,
    direction: str,
) -> str:
    return f"recurring_price_{direction}:{account_id}:{merchant}:{previous_month}:{month}"


def set_planning_insight_card_action(
    conn: sqlite3.Connection,
    card_key: str,
    action: str,
) -> None:
    card_key = card_key.strip()
    if not card_key:
        raise ValueError("card key is required")
    action = action.strip().lower()
    if action not in PLANNING_CARD_ACTIONS:
        raise ValueError("invalid action")
    conn.execute(
        """
        INSERT INTO planning_insight_card_actions(card_key, action, acted_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(card_key) DO UPDATE SET
          action=excluded.action,
          acted_at=CURRENT_TIMESTAMP
        """,
        (card_key, action),
    )


def _planning_action_fields(current_action: str | None = None) -> dict:
    return {
        "current_action": current_action,
        "current_action_label": PLANNING_CARD_ACTIONS.get(
            current_action or "", DEFAULT_PLANNING_CARD_ACTION_LABEL
        ),
    }


def _apply_planning_action_fields(conn: sqlite3.Connection, cards: list[dict]) -> list[dict]:
    for card in cards:
        card.update(_planning_action_fields())
    card_keys = _unique_ids([card["card_key"] for card in cards])
    if not card_keys:
        return cards

    placeholders = ",".join("?" for _ in card_keys)
    rows = conn.execute(
        f"""
        SELECT card_key, action
        FROM planning_insight_card_actions
        WHERE card_key IN ({placeholders})
        """,
        card_keys,
    ).fetchall()
    actions = {row["card_key"]: row["action"] for row in rows}
    for card in cards:
        card.update(_planning_action_fields(actions.get(card["card_key"])))
    return cards


def _previous_month(month: str) -> str:
    year_text, month_text = month.split("-", 1)
    year = int(year_text)
    month_number = int(month_text)
    if month_number == 1:
        return f"{year - 1}-12"
    return f"{year}-{month_number - 1:02d}"


def _category_transaction_evidence(
    conn: sqlite3.Connection,
    *,
    month: str,
    category_id: int,
    limit: int,
) -> dict:
    rows = conn.execute(
        """
        SELECT
          transaction_id,
          MAX(posted_on) AS posted_on,
          -SUM(split_amount_cents) AS net_expense_cents
        FROM v_split_detail
        WHERE month = ?
          AND category_id = ?
          AND category_kind = 'expense'
        GROUP BY transaction_id
        HAVING net_expense_cents <> 0
        ORDER BY ABS(net_expense_cents) DESC, posted_on DESC, transaction_id DESC
        """,
        (month, category_id),
    ).fetchall()
    selected = rows[:limit]
    return {
        "transaction_ids": [str(row["transaction_id"]) for row in selected],
        "transaction_count": len(rows),
        "linked_transaction_count": len(selected),
        "net_cents": sum(int(row["net_expense_cents"]) for row in rows),
        "linked_net_cents": sum(int(row["net_expense_cents"]) for row in selected),
    }


def _transaction_evidence_note(evidence: dict, month_label: str) -> str:
    count = int(evidence["transaction_count"])
    linked = int(evidence["linked_transaction_count"])
    if count == 0:
        return f"No {month_label} transaction evidence linked."
    if linked == count:
        return (
            f"Evidence links cover all {_plural(count, 'transaction')} "
            f"from {month_label} netting {_money(evidence['net_cents'])}."
        )
    return (
        f"Evidence links show top {linked} of {count} transactions from {month_label} "
        f"netting {_money(evidence['linked_net_cents'])} of "
        f"{_money(evidence['net_cents'])} total."
    )


def _statement_line_evidence_note(
    *,
    line_count: int,
    linked_count: int,
    total_spend_cents: int,
    linked_spend_cents: int,
) -> str:
    if linked_count == line_count:
        return (
            f"Evidence links cover all {_plural(line_count, 'statement line')} "
            f"totaling {_money(total_spend_cents)}."
        )
    return (
        f"Evidence links show top {linked_count} of {line_count} statement lines "
        f"totaling {_money(linked_spend_cents)} of {_money(total_spend_cents)}."
    )


def _build_recurring_insight_cards(rows: list[dict]) -> list[dict]:
    cards = []
    for row in rows:
        direction = row["direction_label"]
        previous = _money(row["previous_amount_cents"])
        current = _money(row["current_amount_cents"])
        delta = _money(abs(row["amount_delta_cents"]))
        pct = abs(float(row["pct_change"] or 0))
        pct_text = f"{pct:g}%"
        merchant = row["merchant"]
        account_id = row["account_id"]
        month = row["month"]
        previous_month = row["previous_month"]
        change = "increase" if row["amount_delta_cents"] > 0 else "decrease"
        card_key = _build_card_key(merchant, account_id, previous_month, month, change)

        previous_txn_ids = _split_ids(row.get("previous_transaction_ids"))
        current_txn_ids = _split_ids(row.get("current_transaction_ids"))
        previous_line_ids = _split_ids(row.get("previous_statement_line_ids"))
        current_line_ids = _split_ids(row.get("current_statement_line_ids"))
        statement_line_evidence_groups = []
        if previous_line_ids:
            statement_line_evidence_groups.append(
                {"label": previous_month, "statement_line_ids": previous_line_ids}
            )
        if current_line_ids:
            statement_line_evidence_groups.append(
                {"label": month, "statement_line_ids": current_line_ids}
            )
        suggested_action = (
            "Review the monthly budget impact"
            if row["amount_delta_cents"] > 0
            else "Consider lowering the forecast"
        )
        cards.append(
            {
                "card_key": card_key,
                "merchant": merchant,
                "title": f"{merchant} {direction} from {previous} to {current}",
                "body": (
                    f"Your {merchant} recurring payment {direction} by {delta} "
                    f"({pct_text}) from {previous} in {row['previous_month']} "
                    f"to {current} in {row['month']}."
                ),
                "reason_code": f"recurring_price_{change}",
                "confidence_label": "high",
                "severity_class": row["delta_class"],
                "suggested_action": suggested_action,
                "previous_transaction_ids": previous_txn_ids,
                "current_transaction_ids": current_txn_ids,
                "transaction_ids": previous_txn_ids + current_txn_ids,
                "transaction_evidence_groups": [
                    {"label": previous_month, "transaction_ids": previous_txn_ids},
                    {"label": month, "transaction_ids": current_txn_ids},
                ],
                "previous_statement_line_ids": previous_line_ids,
                "current_statement_line_ids": current_line_ids,
                "statement_line_ids": previous_line_ids + current_line_ids,
                "statement_line_evidence_groups": statement_line_evidence_groups,
                "category_ids": [],
                "evidence_notes": [],
                "transaction_evidence": " -> ".join(
                    [
                        ",".join(previous_txn_ids) or "n/a",
                        ",".join(current_txn_ids) or "n/a",
                    ]
                ),
                "statement_line_evidence": " -> ".join(
                    [
                        ",".join(previous_line_ids) or "n/a",
                        ",".join(current_line_ids) or "n/a",
                    ]
                ),
            }
        )
    return cards


def recurring_insight_cards(
    conn: sqlite3.Connection,
    month: str,
    *,
    delta_rows: list[dict] | None = None,
) -> list[dict]:
    rows = delta_rows if delta_rows is not None else recurring_delta_rows(conn, month)
    return _apply_planning_action_fields(conn, _build_recurring_insight_cards(rows))


def _build_budget_overrun_card(conn: sqlite3.Connection, row: dict) -> dict:
    """Build a planning insight card for a budget overrun."""
    category_name = row["category_name"]
    budget_cents = row["budget_cents"]
    actual_cents = row["actual_cents"]
    remaining_cents = row["remaining_cents"]
    month = row["month"]
    category_id = int(row["category_id"])
    card_key = f"budget_overrun:{row['category_id']}:{row['month']}"
    evidence = _category_transaction_evidence(
        conn,
        month=month,
        category_id=category_id,
        limit=BUDGET_TRANSACTION_EVIDENCE_LIMIT,
    )

    return {
        "card_key": card_key,
        "title": f"{category_name} budget overrun",
        "body": (
            f"{category_name} is {_money(abs(remaining_cents))} over budget for {month}: "
            f"{_money(actual_cents)} spent against {_money(budget_cents)} planned."
        ),
        "reason_code": "budget_overrun",
        "confidence_label": "high",
        "severity_class": "negative",
        "suggested_action": "Review spending and adjust budget",
        "category_ids": [str(category_id)],
        "transaction_ids": evidence["transaction_ids"],
        "statement_line_ids": [],
        "evidence_notes": [_transaction_evidence_note(evidence, month)],
    }


def _unmatched_statement_groups(conn: sqlite3.Connection, month: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM v_statement_coverage_lines
        WHERE month = ?
          AND coverage_bucket = 'unmatched'
          AND spend_cents > 0
        ORDER BY source_document_id, spend_cents DESC, posted_on DESC, line_id DESC
        """,
        (month,),
    ).fetchall()
    groups: dict[int, dict] = {}
    for row in rows:
        doc_id = int(row["source_document_id"])
        group = groups.setdefault(
            doc_id,
            {
                "source_document_id": doc_id,
                "document_name": row["document_name"],
                "month": month,
                "line_count": 0,
                "spend_cents": 0,
                "lines": [],
            },
        )
        line = dict(row)
        group["line_count"] += 1
        group["spend_cents"] += int(row["spend_cents"])
        group["lines"].append(line)

    out = list(groups.values())
    out.sort(
        key=lambda item: (
            -item["spend_cents"],
            item["document_name"],
            item["source_document_id"],
        )
    )
    return out


def _build_unmatched_statement_card(row: dict) -> dict:
    """Build a planning insight card for unmatched statement lines."""
    doc_id = row["source_document_id"]
    document_name = row["document_name"]
    month = row["month"]
    spend_cents = row["spend_cents"]
    line_count = row["line_count"]
    linked_lines = row["lines"][:MAX_UNMATCHED_CARD_LINES]
    linked_spend_cents = sum(int(line["spend_cents"]) for line in linked_lines)
    line_ids = [str(line["line_id"]) for line in linked_lines]
    card_key = f"unmatched_statement:{doc_id}:{month}"

    return {
        "card_key": card_key,
        "title": f"Unmatched statement spend: {document_name}",
        "body": (
            f"{_plural(line_count, 'unmatched line')} totaling {_money(spend_cents)} "
            f"in {document_name} for {month}."
        ),
        "reason_code": "unmatched_statement",
        "confidence_label": "medium",
        "severity_class": "negative",
        "suggested_action": "Add receipts or categorize transactions",
        "category_ids": [],
        "transaction_ids": [],
        "statement_line_ids": line_ids,
        "evidence_notes": [
            _statement_line_evidence_note(
                line_count=line_count,
                linked_count=len(line_ids),
                total_spend_cents=spend_cents,
                linked_spend_cents=linked_spend_cents,
            )
        ],
    }


def _build_category_trend_card(conn: sqlite3.Connection, row: dict) -> dict | None:
    """Build a planning insight card for category spending increase."""
    category_name = row["category_name"]
    actual_cents = row["net_expense_cents"]
    prev_actual_cents = row["prev_net_expense_cents"]
    delta_cents = row["net_delta_cents"]
    pct_change = row["net_pct_change"]
    month = row["month"]
    previous_month = _previous_month(month)
    category_id = int(row["category_id"])

    if actual_cents <= 0 or delta_cents < 1000:
        return None
    is_new_spending = prev_actual_cents <= 0
    if not is_new_spending and (pct_change is None or pct_change < 10):
        return None

    previous_evidence = _category_transaction_evidence(
        conn,
        month=previous_month,
        category_id=category_id,
        limit=TREND_TRANSACTION_EVIDENCE_LIMIT,
    )
    current_evidence = _category_transaction_evidence(
        conn,
        month=month,
        category_id=category_id,
        limit=TREND_TRANSACTION_EVIDENCE_LIMIT,
    )
    transaction_ids = previous_evidence["transaction_ids"] + current_evidence["transaction_ids"]

    reason_code = "category_new_spending" if is_new_spending else "category_increase"
    card_key = f"{reason_code}:{row['category_id']}:{month}"
    if is_new_spending:
        title = f"{category_name} new spending"
        body = (
            f"Spent {_money(actual_cents)} this month after no positive net spend "
            f"last month ({_money(delta_cents)} new spending)."
        )
    else:
        pct_text = f"{float(pct_change):g}%"
        title = f"{category_name} spending increased"
        body = (
            f"Net spend {_money(actual_cents)} this month, up from "
            f"{_money(prev_actual_cents)} last month "
            f"({_money(delta_cents)} more, {pct_text} increase)."
        )

    return {
        "card_key": card_key,
        "title": title,
        "body": body,
        "reason_code": reason_code,
        "confidence_label": "medium",
        "severity_class": "negative",
        "suggested_action": "Review category budget and spending",
        "category_ids": [str(category_id)],
        "transaction_ids": transaction_ids,
        "statement_line_ids": [],
        "evidence_notes": [
            _transaction_evidence_note(previous_evidence, previous_month),
            _transaction_evidence_note(current_evidence, month),
        ],
    }


def _budget_overrun_rows(conn: sqlite3.Connection, month: str) -> list[dict]:
    rows = conn.execute(
        """
        WITH expense_categories AS (
          SELECT id AS category_id
          FROM categories
          WHERE kind = 'expense'
        ),
        grid AS (
          SELECT s.month, c.category_id
          FROM v_month_spine s
          CROSS JOIN expense_categories c
        ),
        resolved AS (
          SELECT
            g.month,
            g.category_id,
            COALESCE(bm.amount_cents, bd.amount_cents, 0) AS budget_cents
          FROM grid g
          LEFT JOIN budgets bm
            ON bm.category_id = g.category_id
           AND bm.period_month = g.month
          LEFT JOIN budgets bd
            ON bd.category_id = g.category_id
           AND bd.period_month = ''
        )
        SELECT
          r.month,
          r.category_id,
          c.name AS category_name,
          c.brand_owner,
          c.color,
          c.is_leisure,
          r.budget_cents,
          COALESCE(a.net_expense_cents, 0) AS actual_cents,
          r.budget_cents - COALESCE(a.net_expense_cents, 0) AS remaining_cents
        FROM resolved r
        JOIN categories c ON c.id = r.category_id
        LEFT JOIN v_planning_category_monthly_net a
          ON a.category_id = r.category_id
         AND a.month = r.month
        WHERE r.month = ?
          AND r.budget_cents > 0
          AND COALESCE(a.net_expense_cents, 0) > r.budget_cents
        ORDER BY remaining_cents ASC, category_name
        """,
        (month,),
    ).fetchall()
    return [dict(row) for row in rows]


def planning_insight_cards(
    conn: sqlite3.Connection,
    month: str,
    *,
    recurring_rows: list[dict] | None = None,
) -> list[dict]:
    """Build all planning insight cards for the selected month."""
    cards = []

    # (1) Recurring payment price changes.
    recurring = recurring_rows if recurring_rows is not None else recurring_delta_rows(conn, month)
    cards.extend(_build_recurring_insight_cards(recurring))

    # (2) Budget overruns using signed net expense, so refunds reduce spend.
    for row in _budget_overrun_rows(conn, month):
        cards.append(_build_budget_overrun_card(conn, row))

    # (3) Unmatched statement spend grouped per statement to avoid card floods.
    for group in _unmatched_statement_groups(conn, month):
        cards.append(_build_unmatched_statement_card(group))

    # (4) Category spending increases using the same signed net convention.
    trend_rows = conn.execute(
        """
        SELECT *
        FROM v_planning_category_net_trend
        WHERE month = ?
          AND category_kind = 'expense'
          AND net_expense_cents > 0
          AND net_delta_cents > 0
        ORDER BY net_delta_cents DESC, category_name
        """,
        (month,),
    ).fetchall()
    for row in trend_rows:
        card = _build_category_trend_card(conn, dict(row))
        if card:
            cards.append(card)

    return _apply_planning_action_fields(conn, cards)


def planning_statement_evidence_lines(conn: sqlite3.Connection, month: str) -> list[dict]:
    cards = planning_insight_cards(conn, month)
    line_ids: list[str] = []
    for card in cards:
        line_ids.extend(card.get("statement_line_ids") or [])
        for group in card.get("statement_line_evidence_groups") or []:
            line_ids.extend(group.get("statement_line_ids") or [])
    line_ids = _unique_ids(line_ids)
    if not line_ids:
        return []

    placeholders = ",".join("?" for _ in line_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM v_statement_coverage_lines
        WHERE line_id IN ({placeholders})
        ORDER BY posted_on DESC, line_id DESC
        """,
        line_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def subscription_watchlist_rows(conn: sqlite3.Connection, month: str) -> list[dict]:
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT
              c.*,
              d.decision,
              d.decided_at
            FROM v_subscription_watchlist_candidates c
            LEFT JOIN subscription_watchlist_decisions d
              ON d.merchant = c.merchant
             AND d.account_id = c.account_id
            WHERE c.last_month = ?
            ORDER BY c.estimated_amount_cents DESC, c.merchant
            LIMIT 12
            """,
            (month,),
        ).fetchall()
    ]
    for row in rows:
        row["decision_label"] = SUBSCRIPTION_WATCHLIST_DECISIONS.get(
            row.get("decision") or "", "needs review"
        )
    return rows


def set_subscription_watchlist_decision(
    conn: sqlite3.Connection,
    *,
    merchant: str,
    account_id: int,
    decision: str,
) -> None:
    merchant = merchant.strip()
    if not merchant:
        raise ValueError("merchant is required")
    if decision not in SUBSCRIPTION_WATCHLIST_DECISIONS:
        raise ValueError("invalid subscription decision")
    account = conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone()
    if account is None:
        raise LookupError("account not found")
    conn.execute(
        """
        INSERT INTO subscription_watchlist_decisions(merchant, account_id, decision, decided_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(merchant, account_id) DO UPDATE SET
          decision=excluded.decision,
          decided_at=CURRENT_TIMESTAMP
        """,
        (merchant, account_id, decision),
    )


def insights_context(conn: sqlite3.Connection, month: str | None = None) -> dict:
    months = available_months(conn)
    selected_month = month if month in months else (months[0] if months else "")

    budget_rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT *
            FROM v_budget_vs_actual
            WHERE month = ?
            ORDER BY
              CASE WHEN budget_cents > 0 THEN 0 ELSE 1 END,
              remaining_cents ASC,
              actual_cents DESC,
              category_name
            """,
            (selected_month,),
        ).fetchall()
    ]
    for row in budget_rows:
        row["progress_width"] = _progress_width(row["actual_cents"], row["budget_cents"])
        row["status_class"] = "negative" if row["remaining_cents"] < 0 else "positive"

    total_budget = sum(r["budget_cents"] for r in budget_rows)
    total_actual = sum(r["actual_cents"] for r in budget_rows)
    summary = {
        "budget_cents": total_budget,
        "actual_cents": total_actual,
        "remaining_cents": total_budget - total_actual,
        "underspend_cents": sum(max(0, r["remaining_cents"]) for r in budget_rows),
        "over_cents": sum(max(0, -r["remaining_cents"]) for r in budget_rows),
    }
    summary["remaining_class"] = "negative" if summary["remaining_cents"] < 0 else "positive"

    leisure = conn.execute(
        "SELECT * FROM v_leisure_vs_bigticket WHERE month=?", (selected_month,)
    ).fetchone()
    leisure_row = dict(leisure) if leisure else {
        "month": selected_month,
        "leisure_cents": 0,
        "bigticket_cents": 0,
        "everyday_cents": 0,
    }
    leisure_total = (
        leisure_row["leisure_cents"]
        + leisure_row["bigticket_cents"]
        + leisure_row["everyday_cents"]
    )
    leisure_row["leisure_width"] = _share_width(leisure_row["leisure_cents"], leisure_total)
    leisure_row["bigticket_width"] = _share_width(leisure_row["bigticket_cents"], leisure_total)
    leisure_row["everyday_width"] = _share_width(leisure_row["everyday_cents"], leisure_total)

    trends = [
        dict(r)
        for r in conn.execute(
            """
            SELECT *
            FROM v_category_monthly_trend
            WHERE month = ? AND category_kind = 'expense'
            ORDER BY magnitude_cents DESC, category_name
            LIMIT 8
            """,
            (selected_month,),
        ).fetchall()
    ]
    for row in trends:
        row["delta_class"] = "negative" if row["magnitude_delta_cents"] > 0 else "positive"

    top_merchants = [
        dict(r)
        for r in conn.execute(
            """
            SELECT *
            FROM v_top_merchants
            WHERE month = ?
            ORDER BY merchant_rank
            LIMIT 8
            """,
            (selected_month,),
        ).fetchall()
    ]
    recurring = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM v_recurring_candidates LIMIT 6"
        ).fetchall()
    ]
    subscription_watchlist = subscription_watchlist_rows(conn, selected_month)
    recurring_deltas = recurring_delta_rows(conn, selected_month)
    planning_cards = planning_insight_cards(
        conn,
        selected_month,
        recurring_rows=recurring_deltas,
    )
    runway = conn.execute(
        "SELECT * FROM v_cashflow_runway WHERE month=?", (selected_month,)
    ).fetchone()

    return {
        "brand": "nancy",
        "active": "insights",
        "months": months,
        "selected_month": selected_month,
        "budget_rows": budget_rows,
        "budget_summary": summary,
        "leisure": leisure_row,
        "trends": trends,
        "top_merchants": top_merchants,
        "subscription_watchlist": subscription_watchlist,
        "subscription_decisions": SUBSCRIPTION_WATCHLIST_DECISIONS,
        "recurring": recurring,
        "recurring_deltas": recurring_deltas,
        "planning_insight_cards": planning_cards,
        "runway": dict(runway) if runway else None,
        "big_ticket_threshold_cents": big_ticket_threshold_cents(conn),
    }
