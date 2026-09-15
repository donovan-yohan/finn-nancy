"""FN-121: activity search / filter / keyset pagination + responsive cards."""
from __future__ import annotations

import re
from urllib.parse import unquote

from fastapi.testclient import TestClient

from app.db import engine


def _client(app_env):
    from app.web.app import create_app

    return TestClient(create_app())


def _row_count(text: str) -> int:
    return text.count('class="tx-row"')


def _cursor_from(text: str) -> str | None:
    m = re.search(r"/transactions\?cursor=([^&\"]+)", text)
    return unquote(m.group(1)) if m else None


def test_activity_renders_responsive_cards_not_table(app_env):
    r = _client(app_env).get("/activity")
    assert r.status_code == 200
    # Cards/list markup, never an overflowing <table>.
    assert 'class="tx-list"' in r.text
    assert "tx-row" in r.text
    assert "<table" not in r.text
    # per-cell labels drive the narrow-viewport card layout
    assert 'data-label="amount"' in r.text
    # why affordance is preserved
    assert 'data-why-url="/chat?why=3"' in r.text


def test_search_matches_description_and_merchant(app_env):
    client = _client(app_env)

    # description match: three "rent payment" rows (ids 2, 10, 16)
    r = client.get("/transactions", params={"q": "rent"})
    assert r.status_code == 200
    assert _row_count(r.text) == 3
    assert "rent payment" in r.text
    assert "weekly groceries" not in r.text

    # merchant/counterparty match: three synthetic payroll rows (ids 1, 9, 15)
    r = client.get("/transactions", params={"q": "Synthetic Employer"})
    assert _row_count(r.text) == 3
    assert "payroll deposit" in r.text


def test_filter_by_category(app_env):
    # Groceries = category 4 -> transactions 3, 11, 17
    r = _client(app_env).get("/transactions", params={"category_id": "4"})
    assert _row_count(r.text) == 3
    assert "weekly groceries" in r.text
    assert "rent payment" not in r.text


def test_filter_by_account(app_env):
    # Account 2 (savings) only has the incoming transfer, txn 8
    r = _client(app_env).get("/transactions", params={"account_id": "2"})
    assert _row_count(r.text) == 1
    assert "#8" in r.text


def test_unknown_flow_is_visible_filterable_and_warns_about_excluded_totals(app_env):
    with engine.write_tx(app_env) as conn:
        transaction_id = int(
            conn.execute(
                """INSERT INTO transactions(
                     account_id, posted_on, description, counterparty,
                     amount_cents, source, external_id, flow_kind)
                   VALUES (1, '2026-06-18', 'ambiguous bank text', '',
                           -1234, 'manual', 'activity-unknown-flow', 'unknown')"""
            ).lastrowid
        )
        conn.execute(
            """INSERT INTO transactions(
                 account_id, posted_on, description, counterparty,
                 amount_cents, source, external_id, flow_kind)
               VALUES (1, '2026-06-19', 'orphan owned transfer', '',
                       -5000, 'manual', 'activity-orphan-transfer',
                       'internal_transfer')"""
        )

    page = _client(app_env).get("/activity", params={"semantic_review": "1"})
    assert page.status_code == 200
    assert "need accounting review" in page.text
    assert "Unknown flows and movements without required provenance" in page.text
    assert "ambiguous bank text" in page.text
    assert "orphan owned transfer" in page.text
    assert f"#{transaction_id} · unknown · needs review" in page.text
    assert "active provenance relationship is required" in page.text
    assert '<input type="hidden" name="semantic_review" value="1">' in page.text


def test_filters_compose_category_and_date_range(app_env):
    client = _client(app_env)

    # February window alone: ids 9..14 -> 6 rows
    r = client.get(
        "/transactions", params={"date_from": "2026-02-01", "date_to": "2026-02-28"}
    )
    assert _row_count(r.text) == 6

    # Compose with the Groceries category -> only txn 11 survives
    r = client.get(
        "/transactions",
        params={"date_from": "2026-02-01", "date_to": "2026-02-28", "category_id": "4"},
    )
    assert _row_count(r.text) == 1
    assert "#11" in r.text


def test_empty_state_when_nothing_matches(app_env):
    r = _client(app_env).get("/transactions", params={"q": "zzz-no-such-merchant"})
    assert _row_count(r.text) == 0
    assert "no transactions match" in r.text


def test_activity_full_page_ignores_stray_cursor(app_env):
    # /activity is the full page: a stray cursor must not strip the wrapper,
    # header, or the why-hold script (those only drop for /transactions appends).
    r = _client(app_env).get("/activity", params={"cursor": "2026-03-19:20"})
    assert r.status_code == 200
    assert 'class="tx-list"' in r.text
    assert 'class="tx-head"' in r.text
    assert "fnWhyHoldInstalled" in r.text


def test_search_treats_like_wildcards_as_literals(app_env):
    # Seed rows whose descriptions contain literal % / _ characters.
    with engine.write_tx(app_env) as conn:
        conn.execute(
            "INSERT INTO transactions(account_id, posted_on, description, counterparty, "
            "amount_cents, source, external_id) "
            "VALUES (1, '2026-06-01', '50% cashback bonus', '', 500, 'sample', 'wild-pct')"
        )
        conn.execute(
            "INSERT INTO transactions(account_id, posted_on, description, counterparty, "
            "amount_cents, source, external_id) "
            "VALUES (1, '2026-06-02', 'under_score co', '', -1000, 'sample', 'wild-und')"
        )

    client = _client(app_env)

    # A bare '%' must not act as "match everything" — only the literal-% row.
    r = client.get("/transactions", params={"q": "%"})
    assert _row_count(r.text) == 1
    assert "50% cashback bonus" in r.text

    # A bare '_' must match only the literal-underscore row, not any single char.
    r = client.get("/transactions", params={"q": "_"})
    assert _row_count(r.text) == 1
    assert "under_score co" in r.text


def test_keyset_pagination_pages_beyond_fifty(app_env):
    # Seed 60 newer rows so the ledger exceeds the 50-row page.
    with engine.write_tx(app_env) as conn:
        for i in range(60):
            day = f"{(i % 27) + 1:02d}"
            txn_id = conn.execute(
                "INSERT INTO transactions(account_id, posted_on, description, counterparty, "
                "amount_cents, source, external_id) "
                "VALUES (1, ?, ?, 'Seed Vendor', -1000, 'sample', ?)",
                (f"2026-05-{day}", f"seeded row {i}", f"seed-{i}"),
            ).lastrowid
            conn.execute(
                "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) "
                "VALUES (?, 4, -1000)",
                (txn_id,),
            )

    client = _client(app_env)

    # First page: exactly 50 rows and a load-more cursor.
    first = client.get("/transactions")
    assert _row_count(first.text) == 50
    cursor = _cursor_from(first.text)
    assert cursor is not None
    assert "load more" in first.text

    # Second page via the cursor: remaining 30 rows (80 total - 50), no further cursor.
    second = client.get("/transactions", params={"cursor": cursor})
    assert _row_count(second.text) == 30
    assert _cursor_from(second.text) is None
    # Append fragment must not re-emit the header wrapper.
    assert 'class="tx-list"' not in second.text

    # No row appears on both pages (keyset, not offset -> no dup/skip).
    first_ids = set(re.findall(r'data-why-url="/chat\?why=(\d+)"', first.text))
    second_ids = set(re.findall(r'data-why-url="/chat\?why=(\d+)"', second.text))
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 80


def test_pagination_carries_filters_into_cursor(app_env):
    # 60 Groceries rows + the 3 sample Groceries rows = 63 in-category.
    with engine.write_tx(app_env) as conn:
        for i in range(60):
            txn_id = conn.execute(
                "INSERT INTO transactions(account_id, posted_on, description, counterparty, "
                "amount_cents, source, external_id) "
                "VALUES (1, '2026-05-01', ?, '', -1000, 'sample', ?)",
                (f"filtered groceries {i}", f"seed-cat-{i}"),
            ).lastrowid
            conn.execute(
                "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) "
                "VALUES (?, 4, -1000)",
                (txn_id,),
            )

    client = _client(app_env)
    first = client.get("/transactions", params={"category_id": "4"})
    assert _row_count(first.text) == 50
    # The load-more URL round-trips the active filter.
    assert "category_id=4" in first.text

    m = re.search(r'/transactions\?(cursor=[^"]+)', first.text)
    assert m is not None
    second = client.get("/transactions?" + m.group(1).replace("&amp;", "&"))
    assert _row_count(second.text) == 13  # 63 total - 50 on the first page
    assert "rent payment" not in second.text
