from __future__ import annotations

from html import unescape

from fastapi.testclient import TestClient

from app.db import engine, repo_budgets, repo_ledger, repo_statements


def _client():
    from app.web.app import create_app

    return TestClient(create_app())


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
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=f"{merchant} payment",
        counterparty=merchant,
        amount_cents=-abs(amount_cents),
        source="test",
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind="purchase",
    )
    assert txn_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=-abs(amount_cents),
    )
    repo_statements.mark_cleared(conn, txn_id, posted_on)
    return txn_id


def _seed_recurring_delta_fixture(db_path: str) -> None:
    with engine.write_tx(db_path) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Recurring Card','Test','credit','CAD')"
        ).lastrowid
        category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Recurring Services','expense','nancy','#ff9f43')"
        ).lastrowid

        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-05-04",
            merchant="Koodo",
            amount_cents=4633,
            external_id="koodo-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-04",
            merchant="Koodo",
            amount_cents=5210,
            external_id="koodo-jun",
        )

        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-05-08",
            merchant="Tiny Variance Cloud",
            amount_cents=10000,
            external_id="tiny-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-08",
            merchant="Tiny Variance Cloud",
            amount_cents=10100,
            external_id="tiny-jun",
        )

        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-12",
            merchant="One Off Shop",
            amount_cents=8800,
            external_id="one-off",
        )


def test_recurring_payment_deltas_detect_meaningful_changes_only(app_env):
    _seed_recurring_delta_fixture(app_env)

    with engine.read_conn(app_env) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM v_recurring_payment_deltas
            WHERE month='2026-06'
            ORDER BY merchant
            """
        ).fetchall()
        meaningful = repo_budgets.recurring_delta_rows(conn, "2026-06")
        cards = repo_budgets.recurring_insight_cards(conn, "2026-06")

    by_merchant = {row["merchant"]: dict(row) for row in rows}
    assert by_merchant["Koodo"]["previous_amount_cents"] == 4633
    assert by_merchant["Koodo"]["current_amount_cents"] == 5210
    assert by_merchant["Koodo"]["amount_delta_cents"] == 577
    assert by_merchant["Koodo"]["pct_change"] == 12.5
    assert by_merchant["Koodo"]["is_meaningful_delta"] == 1

    assert by_merchant["Tiny Variance Cloud"]["amount_delta_cents"] == 100
    assert by_merchant["Tiny Variance Cloud"]["is_meaningful_delta"] == 0
    assert "One Off Shop" not in by_merchant

    assert [row["merchant"] for row in meaningful] == ["Koodo"]
    assert meaningful[0]["direction_label"] == "increased"
    assert meaningful[0]["delta_class"] == "negative"

    assert len(cards) == 1
    card = cards[0]
    assert card["card_key"] == (
        f"recurring_price_increase:{by_merchant['Koodo']['account_id']}:Koodo:2026-05:2026-06"
    )
    assert card["title"] == "Koodo increased from $46.33 to $52.10"
    assert "by $5.77 (12.5%)" in card["body"]
    assert card["reason_code"] == "recurring_price_increase"
    assert card["confidence_label"] == "high"
    assert card["suggested_action"] == "Review the monthly budget impact"
    assert by_merchant["Koodo"]["previous_transaction_ids"]
    assert by_merchant["Koodo"]["current_transaction_ids"]
    assert card["previous_transaction_ids"] == by_merchant["Koodo"]["previous_transaction_ids"].split(",")
    assert card["current_transaction_ids"] == by_merchant["Koodo"]["current_transaction_ids"].split(",")
    assert card["transaction_evidence"] == (
        f"{by_merchant['Koodo']['previous_transaction_ids']} -> "
        f"{by_merchant['Koodo']['current_transaction_ids']}"
    )
    assert card["transaction_evidence_groups"] == [
        {"label": "2026-05", "transaction_ids": card["previous_transaction_ids"]},
        {"label": "2026-06", "transaction_ids": card["current_transaction_ids"]},
    ]
    assert card["current_action"] is None
    assert card["current_action_label"] == "needs review"


def test_recurring_views_exclude_unknown_and_transfer_flows(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('Semantic Card', 'Test', 'credit', 'CAD')"""
            ).lastrowid
        )
        category_id = int(
            conn.execute(
                """INSERT INTO categories(name, kind, brand_owner, color)
                   VALUES ('Semantic Recurring', 'expense', 'nancy', '#ff9f43')"""
            ).lastrowid
        )
        for flow_kind, merchant in (
            ("unknown", "Unknown Monthly"),
            ("internal_transfer", "Transfer Monthly"),
        ):
            for month in ("05", "06"):
                txn_id = repo_ledger.insert_transaction(
                    conn,
                    account_id=account_id,
                    posted_on=f"2026-{month}-04",
                    description=f"{merchant} payment",
                    counterparty=merchant,
                    amount_cents=-2500,
                    source="test",
                    external_id=f"{flow_kind}-{month}",
                    source_document_id=None,
                    source_confidence=1.0,
                    flow_kind=flow_kind,
                )
                assert txn_id is not None
                repo_ledger.insert_split(
                    conn,
                    transaction_id=txn_id,
                    category_id=category_id,
                    amount_cents=-2500,
                )
                repo_statements.mark_cleared(conn, txn_id, f"2026-{month}-04")

    with engine.read_conn(app_env) as conn:
        merchants = {
            row["merchant"]
            for row in conn.execute(
                "SELECT merchant FROM v_recurring_payment_deltas"
            )
        }
    assert "Unknown Monthly" not in merchants
    assert "Transfer Monthly" not in merchants


def test_insights_page_renders_recurring_changes(app_env):
    _seed_recurring_delta_fixture(app_env)
    with engine.read_conn(app_env) as conn:
        card = repo_budgets.recurring_insight_cards(conn, "2026-06")[0]

    r = _client().get("/insights?month=2026-06")
    assert r.status_code == 200
    body = r.text
    body_text = unescape(body)
    assert "planning cards" in body
    assert "recurring changes" in body
    assert "Koodo" in body
    assert "$46.33" in body
    assert "$52.10" in body
    assert "$5.77" in body
    assert "increased 12.5%" in body
    assert "Koodo increased from $46.33 to $52.10" in body
    assert "Your Koodo recurring payment increased by $5.77 (12.5%)" in body
    assert "recurring_price_increase" in body
    assert "high confidence" in body
    assert "Suggested action: Review the monthly budget impact" in body
    assert "Review status:" in body
    assert "needs review" in body
    assert 'action="/insights/cards/action"' in body
    assert f'name="card_key" value="{card["card_key"]}"' in body
    assert 'value="accepted"' in body
    assert 'value="dismissed"' in body
    assert 'value="snoozed"' in body
    assert "txns" in body_text
    for txn_id in card["previous_transaction_ids"] + card["current_transaction_ids"]:
        assert f'href="/txn/{txn_id}/edit"' in body
        assert f"#{txn_id}" in body_text
    planning_segment = body.split(f'data-card-key="{card["card_key"]}"', 1)[1].split("</article>", 1)[0]
    planning_segment_text = unescape(planning_segment)
    assert "2026-05" in planning_segment_text
    assert "2026-06" in planning_segment_text
    assert "→" in planning_segment_text
    changes_section = body.split("recurring changes", 1)[1].split("recurring candidates", 1)[0]
    assert "Tiny Variance Cloud" not in changes_section

    action = _client().post(
        "/insights/cards/action",
        data={
            "card_key": card["card_key"],
            "action": "snoozed",
            "month": "2026-06",
        },
        follow_redirects=False,
    )
    assert action.status_code == 303
    assert action.headers["location"] == "/insights?month=2026-06"

    with engine.read_conn(app_env) as conn:
        saved = conn.execute(
            "SELECT action FROM planning_insight_card_actions WHERE card_key=?",
            (card["card_key"],),
        ).fetchone()
        updated_card = repo_budgets.recurring_insight_cards(conn, "2026-06")[0]

    assert saved["action"] == "snoozed"
    assert updated_card["current_action"] == "snoozed"
    assert updated_card["current_action_label"] == "snoozed"
    updated_page = _client().get("/insights?month=2026-06")
    assert "snoozed" in updated_page.text
