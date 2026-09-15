"""Tests for deterministic, evidence-backed planning insight cards."""
from __future__ import annotations

from html import unescape

from fastapi.testclient import TestClient

from app.accounting import flows
from app.db import engine, repo_budgets, repo_ledger, repo_statements


def _client():
    from app.web.app import create_app

    return TestClient(create_app())


def _insert_category_activity(
    conn,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    merchant: str,
    signed_amount_cents: int,
    external_id: str,
    flow_kind: str,
) -> int:
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=f"{merchant} payment",
        counterparty=merchant,
        amount_cents=signed_amount_cents,
        source="test",
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind=flow_kind,
    )
    assert txn_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=signed_amount_cents,
    )
    repo_statements.mark_cleared(conn, txn_id, posted_on)
    return txn_id


def _insert_expense(
    conn,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    merchant: str,
    amount_cents: int,
    external_id: str,
) -> int:
    return _insert_category_activity(
        conn,
        account_id=account_id,
        category_id=category_id,
        posted_on=posted_on,
        merchant=merchant,
        signed_amount_cents=-abs(amount_cents),
        external_id=external_id,
        flow_kind="purchase",
    )


def _insert_refund(
    conn,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    merchant: str,
    amount_cents: int,
    external_id: str,
) -> int:
    return _insert_category_activity(
        conn,
        account_id=account_id,
        category_id=category_id,
        posted_on=posted_on,
        merchant=merchant,
        signed_amount_cents=abs(amount_cents),
        external_id=external_id,
        flow_kind="refund",
    )


def _insert_month_marker(conn, *, month: str, suffix: str) -> None:
    account_id = conn.execute(
        "INSERT INTO accounts(name, institution, kind, currency) VALUES (?,?, 'chequing','CAD')",
        (f"Month Marker {suffix}", "Test"),
    ).lastrowid
    category_id = conn.execute(
        "INSERT INTO categories(name, kind, brand_owner, color) VALUES (?, 'expense','nancy','#aaaaaa')",
        (f"Month Marker {suffix}",),
    ).lastrowid
    _insert_expense(
        conn,
        account_id=account_id,
        category_id=category_id,
        posted_on=f"{month}-01",
        merchant=f"Month Marker {suffix}",
        amount_cents=1,
        external_id=f"month-marker-{suffix}",
    )


def _insert_statement_doc(conn, name: str, *, status: str = "matched") -> int:
    return conn.execute(
        """INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
           VALUES ('statement', ?, ?, ?, 'application/pdf', ?)""",
        (name, f"blobs/{name}", f"sha-{name}", status),
    ).lastrowid


def _insert_statement_line(
    conn,
    *,
    doc_id: int,
    account_id: int,
    posted_on: str,
    description: str,
    amount_cents: int,
    row_hash: str,
    match_status: str = "unmatched",
    matched_transaction_id: int | None = None,
) -> int:
    return conn.execute(
        """INSERT INTO statement_lines(
             source_document_id, account_id, posted_on, raw_description, norm_merchant,
             amount_cents, currency, is_pending, statement_period, row_hash,
             match_status, matched_transaction_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            doc_id,
            account_id,
            posted_on,
            description,
            description,
            amount_cents,
            "CAD",
            0,
            posted_on[:7],
            row_hash,
            match_status,
            matched_transaction_id,
        ),
    ).lastrowid


def _seed_planning_cards_fixture(db_path: str) -> None:
    with engine.write_tx(db_path) as conn:
        recurring_account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Recurring Card','Test','credit','CAD')"
        ).lastrowid
        groceries_account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Groceries Card','Test','credit','CAD')"
        ).lastrowid

        recurring_category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Recurring Services Test','expense','nancy','#ff9f43')"
        ).lastrowid
        groceries_category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Groceries Test','expense','finn','#4EA1FF')"
        ).lastrowid
        dining_category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Dining Test','expense','nancy','#ff6b6b')"
        ).lastrowid

        _insert_expense(
            conn,
            account_id=recurring_account_id,
            category_id=recurring_category_id,
            posted_on="2026-05-04",
            merchant="Koodo",
            amount_cents=4633,
            external_id="koodo-may-test",
        )
        _insert_expense(
            conn,
            account_id=recurring_account_id,
            category_id=recurring_category_id,
            posted_on="2026-06-04",
            merchant="Koodo",
            amount_cents=5210,
            external_id="koodo-jun-test",
        )

        conn.execute(
            "INSERT INTO budgets(category_id, period_month, amount_cents, owner, owner_member_id, updated_at) "
            "VALUES (?, '', 10000, 'shared', NULL, CURRENT_TIMESTAMP)",
            (groceries_category_id,),
        )
        for idx, posted_on in enumerate(("2026-06-01", "2026-06-02", "2026-06-03"), start=1):
            _insert_expense(
                conn,
                account_id=groceries_account_id,
                category_id=groceries_category_id,
                posted_on=posted_on,
                merchant="TTC",
                amount_cents=5000,
                external_id=f"grocery-{idx}-test",
            )

        for idx, posted_on in enumerate(("2026-05-01", "2026-05-15"), start=1):
            _insert_expense(
                conn,
                account_id=groceries_account_id,
                category_id=dining_category_id,
                posted_on=posted_on,
                merchant=f"Dining May {idx}",
                amount_cents=2000,
                external_id=f"dining-may-{idx}-test",
            )
        for idx, posted_on in enumerate(("2026-06-01", "2026-06-05", "2026-06-10"), start=1):
            _insert_expense(
                conn,
                account_id=groceries_account_id,
                category_id=dining_category_id,
                posted_on=posted_on,
                merchant=f"Dining Jun {idx}",
                amount_cents=5000,
                external_id=f"dining-jun-{idx}-test",
            )


def _article_segment(body: str, card_key: str) -> str:
    marker = f'data-card-key="{card_key}"'
    marker_pos = body.index(marker)
    start = body.rfind("<article", 0, marker_pos)
    end = body.index("</article>", marker_pos)
    return body[start:end]


def test_planning_cards_include_recurring_budget_overrun_and_category_trend(app_env):
    _seed_planning_cards_fixture(app_env)

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    koodo_card = [
        c for c in cards if c["card_key"].startswith("recurring_price_increase:") and "Koodo" in c["title"]
    ][0]
    assert koodo_card["reason_code"] == "recurring_price_increase"
    assert koodo_card["confidence_label"] == "high"
    assert koodo_card["severity_class"] == "negative"
    assert koodo_card["suggested_action"] == "Review the monthly budget impact"
    assert koodo_card["current_action"] is None
    assert koodo_card["current_action_label"] == "needs review"
    assert koodo_card["previous_transaction_ids"]
    assert koodo_card["current_transaction_ids"]
    assert koodo_card["transaction_evidence_groups"][0]["label"] == "2026-05"
    assert koodo_card["transaction_evidence_groups"][1]["label"] == "2026-06"

    overrun_card = [c for c in cards if "Groceries Test budget overrun" in c["title"]][0]
    assert overrun_card["reason_code"] == "budget_overrun"
    assert overrun_card["body"] == (
        "Groceries Test is $50.00 over budget for 2026-06: "
        "$150.00 spent against $100.00 planned."
    )
    assert overrun_card["category_ids"]
    assert overrun_card["transaction_ids"]
    assert overrun_card["evidence_notes"] == [
        "Evidence links cover all 3 transactions from 2026-06 netting $150.00."
    ]

    trend_card = [c for c in cards if c["card_key"].startswith("category_increase:") and "Dining Test" in c["title"]][0]
    assert trend_card["reason_code"] == "category_increase"
    assert "Net spend $150.00 this month, up from $40.00 last month" in trend_card["body"]
    assert trend_card["category_ids"]
    assert trend_card["transaction_ids"]


def test_unmatched_statement_card_aggregates_and_renders_inspectable_links(app_env):
    with engine.write_tx(app_env) as conn:
        _insert_month_marker(conn, month="2026-06", suffix="coverage-june")
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Coverage Card','Test','credit','CAD')"
        ).lastrowid
        doc_id = _insert_statement_doc(conn, "coverage-june.pdf")
        line_id = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-05",
            description="NEW MERCHANT",
            amount_cents=-4000,
            row_hash="h-unmatched",
            match_status="needs_review",
        )

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    unmatched_card = [c for c in cards if c["card_key"] == f"unmatched_statement:{doc_id}:2026-06"][0]
    assert unmatched_card["title"] == "Unmatched statement spend: coverage-june.pdf"
    assert unmatched_card["body"] == "1 unmatched line totaling $40.00 in coverage-june.pdf for 2026-06."
    assert unmatched_card["suggested_action"] == "Add receipts or categorize transactions"
    assert unmatched_card["statement_line_ids"] == [str(line_id)]

    insights = _client().get("/insights?month=2026-06")
    assert insights.status_code == 200
    assert f'href="/recon?month=2026-06#line-{line_id}"' in insights.text

    recon_page = _client().get("/recon?month=2026-06")
    assert recon_page.status_code == 200
    assert recon_page.text.count(f'id="line-{line_id}"') == 1
    target = recon_page.text[recon_page.text.index(f'id="line-{line_id}"'):]
    assert "NEW MERCHANT" in target
    assert "coverage-june.pdf" in target


def test_statement_only_future_month_does_not_shift_planning_defaults(app_env):
    with engine.write_tx(app_env) as conn:
        doc_id = _insert_statement_doc(conn, "future-import.pdf", status="needs_review")
        line_id = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=1,
            posted_on="2027-01-04",
            description="FUTURE IMPORT",
            amount_cents=-1234,
            row_hash="h-future-default-month",
            match_status="needs_review",
        )

    with engine.read_conn(app_env) as conn:
        assert repo_budgets.default_month(conn) == "2026-03"
        assert repo_budgets.budget_management_context(conn)["selected_month"] == "2026-03"
        insights_ctx = repo_budgets.insights_context(conn)
        assert insights_ctx["selected_month"] == "2026-03"
        assert insights_ctx["months"][0] == "2026-03"
        assert "2027-01" not in insights_ctx["months"]
        recon_months = repo_budgets.reconciliation_months(conn)
        assert recon_months[0] == "2027-01"
        assert "2026-03" in recon_months

    client = _client()
    categories = client.get("/categories")
    assert categories.status_code == 200
    assert "actuals from 2026-03" in categories.text
    assert "actuals from 2027-01" not in categories.text

    insights = client.get("/insights")
    assert insights.status_code == 200
    assert '<option value="2026-03" selected' in insights.text
    assert 'value="2027-01"' not in insights.text

    recon = client.get("/recon?month=2027-01")
    assert recon.status_code == 200
    assert '<option value="2027-01" selected' in recon.text
    assert f'id="line-{line_id}"' in recon.text
    assert "FUTURE IMPORT" in recon.text


def test_planning_cards_action_persists_and_updates_target_card_label(app_env):
    _seed_planning_cards_fixture(app_env)
    client = _client()

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")
        overrun_card = [c for c in cards if "Groceries Test budget overrun" in c["title"]][0]
        card_key = overrun_card["card_key"]

    before_page = client.get("/insights?month=2026-06")
    before_segment = _article_segment(before_page.text, card_key)
    assert "Groceries Test budget overrun" in before_segment
    assert "Review status: <strong>needs review</strong>" in before_segment
    assert "Review status: <strong>snoozed</strong>" not in before_segment

    action = client.post(
        "/insights/cards/action",
        data={"card_key": card_key, "action": "snoozed", "month": "2026-06"},
        follow_redirects=False,
    )
    assert action.status_code == 303
    assert action.headers["location"] == "/insights?month=2026-06"

    with engine.read_conn(app_env) as conn:
        saved = conn.execute(
            "SELECT action FROM planning_insight_card_actions WHERE card_key=?",
            (card_key,),
        ).fetchone()
        updated_card = [
            c for c in repo_budgets.planning_insight_cards(conn, "2026-06") if c["card_key"] == card_key
        ][0]

    assert saved["action"] == "snoozed"
    assert updated_card["current_action"] == "snoozed"
    assert updated_card["current_action_label"] == "snoozed"

    updated_page = client.get("/insights?month=2026-06")
    updated_segment = _article_segment(updated_page.text, card_key)
    assert "Groceries Test budget overrun" in updated_segment
    assert "Review status: <strong>snoozed</strong>" in updated_segment


def test_insights_page_renders_planning_cards_with_evidence_links(app_env):
    _seed_planning_cards_fixture(app_env)

    r = _client().get("/insights?month=2026-06")
    assert r.status_code == 200
    body = r.text
    body_text = unescape(body)

    assert "planning cards" in body
    assert "All planning cards" in body
    assert "Koodo increased from $46.33 to $52.10" in body
    assert "Groceries Test budget overrun" in body
    assert "Dining Test spending increased" in body

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")
    checked_cards = [
        c for c in cards if "Groceries Test budget overrun" in c["title"] or "Dining Test" in c["title"]
    ]
    assert checked_cards
    for card in checked_cards:
        for txn_id in card["transaction_ids"]:
            assert f'href="/txn/{txn_id}/edit"' in body
        for category_id in card["category_ids"]:
            assert f'href="/categories#category-{category_id}"' in body
        for note in card["evidence_notes"]:
            assert note in body_text

    assert 'action="/insights/cards/action"' in body
    assert 'value="accepted"' in body
    assert 'value="dismissed"' in body
    assert 'value="snoozed"' in body


def test_recurring_card_without_statement_lines_suppresses_lines_pill(app_env):
    _seed_planning_cards_fixture(app_env)

    with engine.read_conn(app_env) as conn:
        card = [
            c
            for c in repo_budgets.planning_insight_cards(conn, "2026-06")
            if c["card_key"].startswith("recurring_price_increase:") and "Koodo" in c["title"]
        ][0]

    assert card["transaction_ids"]
    assert card["statement_line_ids"] == []
    assert card["statement_line_evidence_groups"] == []

    page = _client().get("/insights?month=2026-06")
    assert page.status_code == 200
    segment = _article_segment(page.text, card["card_key"])
    assert "Koodo increased from $46.33 to $52.10" in segment
    assert "<span>txns</span>" in segment
    assert "<span>lines</span>" not in segment


def test_budget_overrun_uses_signed_net_refunds_and_suppresses_zero_net(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Refund Card','Test','credit','CAD')"
        ).lastrowid
        net_category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Refund Budget Test','expense','nancy','#ff9f43')"
        ).lastrowid
        zero_category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Zero Net Budget Test','expense','nancy','#ff9f43')"
        ).lastrowid
        conn.execute(
            "INSERT INTO budgets(category_id, period_month, amount_cents, owner, owner_member_id, updated_at) "
            "VALUES (?, '', 5000, 'shared', NULL, CURRENT_TIMESTAMP)",
            (net_category_id,),
        )
        conn.execute(
            "INSERT INTO budgets(category_id, period_month, amount_cents, owner, owner_member_id, updated_at) "
            "VALUES (?, '', 1000, 'shared', NULL, CURRENT_TIMESTAMP)",
            (zero_category_id,),
        )
        net_charge_id = _insert_expense(
            conn,
            account_id=account_id,
            category_id=net_category_id,
            posted_on="2026-06-01",
            merchant="Refund Store",
            amount_cents=12000,
            external_id="refund-budget-charge",
        )
        net_refund_id = _insert_refund(
            conn,
            account_id=account_id,
            category_id=net_category_id,
            posted_on="2026-06-02",
            merchant="Refund Store",
            amount_cents=5000,
            external_id="refund-budget-refund",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            source_transaction_id=net_refund_id,
            target_transaction_id=net_charge_id,
            actor="test:planning-insights",
            reason="refund belongs to budget test charge",
        )
        zero_charge_id = _insert_expense(
            conn,
            account_id=account_id,
            category_id=zero_category_id,
            posted_on="2026-06-03",
            merchant="Zero Store",
            amount_cents=3000,
            external_id="zero-budget-charge",
        )
        zero_refund_id = _insert_refund(
            conn,
            account_id=account_id,
            category_id=zero_category_id,
            posted_on="2026-06-04",
            merchant="Zero Store",
            amount_cents=3000,
            external_id="zero-budget-refund",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            source_transaction_id=zero_refund_id,
            target_transaction_id=zero_charge_id,
            actor="test:planning-insights",
            reason="refund belongs to zero-net charge",
        )

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    refund_card = [c for c in cards if "Refund Budget Test budget overrun" in c["title"]][0]
    assert refund_card["body"] == (
        "Refund Budget Test is $20.00 over budget for 2026-06: "
        "$70.00 spent against $50.00 planned."
    )
    assert "Evidence links cover all 2 transactions from 2026-06 netting $70.00." in refund_card["evidence_notes"]
    assert not [c for c in cards if "Zero Net Budget Test" in c["title"]]


def test_category_trend_uses_signed_net_and_suppresses_refund_dominated_month(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Trend Refund Card','Test','credit','CAD')"
        ).lastrowid
        category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Refund Trend Test','expense','nancy','#ff9f43')"
        ).lastrowid
        prior_charge_id = _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-04-03",
            merchant="Trend Store",
            amount_cents=14000,
            external_id="refund-trend-april-charge",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-05-03",
            merchant="Trend Store",
            amount_cents=500,
            external_id="refund-trend-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-03",
            merchant="Trend Store",
            amount_cents=10000,
            external_id="refund-trend-june-charge",
        )
        refund_id = _insert_refund(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-04",
            merchant="Trend Store",
            amount_cents=14000,
            external_id="refund-trend-june-refund",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            source_transaction_id=refund_id,
            target_transaction_id=prior_charge_id,
            actor="test:planning-insights",
            reason="June refund belongs to the April purchase",
        )

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    assert not [c for c in cards if "Refund Trend Test" in c["title"]]


def test_category_trend_emits_new_spending_when_previous_month_is_zero(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('New Spend Card','Test','credit','CAD')"
        ).lastrowid
        category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('New Spend Test','expense','nancy','#ff9f43')"
        ).lastrowid
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-08",
            merchant="New Store",
            amount_cents=50000,
            external_id="new-spend-june",
        )

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    card = [c for c in cards if c["card_key"].startswith("category_new_spending:") and "New Spend Test" in c["title"]][0]
    assert card["reason_code"] == "category_new_spending"
    assert card["title"] == "New Spend Test new spending"
    assert card["body"] == (
        "Spent $500.00 this month after no positive net spend last month ($500.00 new spending)."
    )


def test_negative_cases_do_not_emit_under_threshold_or_non_unmatched_cards(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Negative Case Card','Test','credit','CAD')"
        ).lastrowid
        budget_category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Under Budget Test','expense','nancy','#ff9f43')"
        ).lastrowid
        trend_category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Tiny Trend Test','expense','nancy','#ff9f43')"
        ).lastrowid
        conn.execute(
            "INSERT INTO budgets(category_id, period_month, amount_cents, owner, owner_member_id, updated_at) "
            "VALUES (?, '', 20000, 'shared', NULL, CURRENT_TIMESTAMP)",
            (budget_category_id,),
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=budget_category_id,
            posted_on="2026-05-02",
            merchant="Under Budget",
            amount_cents=10000,
            external_id="under-budget-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=budget_category_id,
            posted_on="2026-06-02",
            merchant="Under Budget",
            amount_cents=9000,
            external_id="under-budget",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=trend_category_id,
            posted_on="2026-05-02",
            merchant="Tiny Trend",
            amount_cents=10000,
            external_id="tiny-trend-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=trend_category_id,
            posted_on="2026-06-02",
            merchant="Tiny Trend",
            amount_cents=10500,
            external_id="tiny-trend-june",
        )
        doc_id = _insert_statement_doc(conn, "negative-lines.pdf")
        matched_txn = _insert_expense(
            conn,
            account_id=account_id,
            category_id=budget_category_id,
            posted_on="2026-06-06",
            merchant="Matched Line",
            amount_cents=1111,
            external_id="matched-line-txn",
        )
        _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-06",
            description="MATCHED LINE",
            amount_cents=-1111,
            row_hash="h-matched-line",
            match_status="matched",
            matched_transaction_id=matched_txn,
        )
        _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-07",
            description="IGNORED LINE",
            amount_cents=-2222,
            row_hash="h-ignored-line",
            match_status="ignored",
        )

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    assert not [c for c in cards if "Under Budget Test" in c["title"]]
    assert not [c for c in cards if "Tiny Trend Test" in c["title"]]
    unmatched = [c for c in cards if c["card_key"].startswith("unmatched_statement:")]
    assert not [c for c in unmatched if "negative-lines.pdf" in c["title"]]


def test_unmatched_statement_cards_are_bounded_and_honest_about_truncation(app_env):
    with engine.write_tx(app_env) as conn:
        _insert_month_marker(conn, month="2026-06", suffix="flood-june")
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Flood Card','Test','credit','CAD')"
        ).lastrowid
        doc_id = _insert_statement_doc(conn, "flood-june.pdf")
        for idx in range(10):
            _insert_statement_line(
                conn,
                doc_id=doc_id,
                account_id=account_id,
                posted_on=f"2026-06-{idx + 1:02d}",
                description=f"FLOOD MERCHANT {idx}",
                amount_cents=-(1000 + idx),
                row_hash=f"h-flood-{idx}",
            )

    with engine.read_conn(app_env) as conn:
        cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    unmatched_cards = [c for c in cards if c["card_key"] == f"unmatched_statement:{doc_id}:2026-06"]
    assert len(unmatched_cards) == 1
    card = unmatched_cards[0]
    assert card["body"] == "10 unmatched lines totaling $100.45 in flood-june.pdf for 2026-06."
    assert len(card["statement_line_ids"]) == 8
    assert card["evidence_notes"] == [
        "Evidence links show top 8 of 10 statement lines totaling $80.44 of $100.45."
    ]

    page = _client().get("/insights?month=2026-06")
    assert page.status_code == 200
    assert card["evidence_notes"][0] in unescape(page.text)


def test_recon_line_ids_are_unique_and_evidence_targets_actionable_review_card(app_env):
    with engine.write_tx(app_env) as conn:
        _insert_month_marker(conn, month="2026-06", suffix="review-anchor")
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Review Anchor Card','Test','credit','CAD')"
        ).lastrowid
        doc_id = _insert_statement_doc(conn, "review-anchor.pdf", status="needs_review")
        line_id = _insert_statement_line(
            conn,
            doc_id=doc_id,
            account_id=account_id,
            posted_on="2026-06-09",
            description="REVIEW ANCHOR",
            amount_cents=-9900,
            row_hash="h-review-anchor",
            match_status="needs_review",
        )

    insights = _client().get("/insights?month=2026-06")
    assert f'href="/recon?month=2026-06#line-{line_id}"' in insights.text

    recon_page = _client().get("/recon?month=2026-06")
    body = recon_page.text
    assert body.count(f'id="line-{line_id}"') == 1
    # The actionable review card owns this line; the coverage evidence table
    # excludes it so one statement row is never rendered twice.
    assert body.count(f'id="attn-line-{line_id}"') == 0
    target = body[body.index(f'id="line-{line_id}"'):]
    assert "REVIEW ANCHOR" in target
    assert f'action="/recon/line/{line_id}/promote"' in target
    assert f'action="/recon/line/{line_id}/ignore"' in target


def test_previous_month_rolls_over_january():
    assert repo_budgets._previous_month("2026-01") == "2025-12"


def test_planning_cards_no_duplicate_recurring_cards(app_env):
    _seed_planning_cards_fixture(app_env)

    with engine.read_conn(app_env) as conn:
        recurring_cards = repo_budgets.recurring_insight_cards(conn, "2026-06")
        planning_cards = repo_budgets.planning_insight_cards(conn, "2026-06")

    recurring_keys = {c["card_key"] for c in recurring_cards}
    planning_keys = {c["card_key"] for c in planning_cards}
    assert recurring_keys.issubset(planning_keys)
    assert len(planning_keys) == len(planning_cards)
