from __future__ import annotations

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


def _seed_subscription_fixture(db_path: str) -> tuple[int, int]:
    with engine.write_tx(db_path) as conn:
        account_id = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Watch Card','Test','credit','CAD')"
        ).lastrowid
        category_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES ('Watchlist Services','expense','nancy','#ff9f43')"
        ).lastrowid

        first_txn = _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-05-15",
            merchant="Fresh Stream",
            amount_cents=1299,
            external_id="fresh-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-15",
            merchant="Fresh Stream",
            amount_cents=1299,
            external_id="fresh-jun",
        )

        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-20",
            merchant="One Off Shop",
            amount_cents=4200,
            external_id="one-off-jun",
        )

        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-04-03",
            merchant="Known Cloud",
            amount_cents=999,
            external_id="known-apr",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-05-03",
            merchant="Known Cloud",
            amount_cents=999,
            external_id="known-may",
        )
        _insert_expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-03",
            merchant="Known Cloud",
            amount_cents=999,
            external_id="known-jun",
        )

    return int(account_id), int(first_txn)


def test_subscription_watchlist_detects_new_monthly_charge_only(app_env):
    account_id, first_txn = _seed_subscription_fixture(app_env)

    with engine.read_conn(app_env) as conn:
        rows = repo_budgets.subscription_watchlist_rows(conn, "2026-06")

    assert [row["merchant"] for row in rows] == ["Fresh Stream"]
    row = rows[0]
    assert row["account_id"] == account_id
    assert row["first_month"] == "2026-05"
    assert row["last_month"] == "2026-06"
    assert row["first_seen_on"] == "2026-05-15"
    assert row["last_seen_on"] == "2026-06-15"
    assert row["expected_next_charge_on"] == "2026-07-15"
    assert row["estimated_amount_cents"] == 1299
    assert str(first_txn) in row["transaction_ids"]
    assert row["decision_label"] == "needs review"


def test_subscription_watchlist_excludes_unknown_and_transfer_flows(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('Semantic Watch Card', 'Test', 'credit', 'CAD')"""
            ).lastrowid
        )
        category_id = int(
            conn.execute(
                """INSERT INTO categories(name, kind, brand_owner, color)
                   VALUES ('Semantic Watch', 'expense', 'nancy', '#ff9f43')"""
            ).lastrowid
        )
        for flow_kind, merchant in (
            ("unknown", "Unknown Watch"),
            ("internal_transfer", "Transfer Watch"),
        ):
            for month in ("05", "06"):
                txn_id = repo_ledger.insert_transaction(
                    conn,
                    account_id=account_id,
                    posted_on=f"2026-{month}-15",
                    description=f"{merchant} payment",
                    counterparty=merchant,
                    amount_cents=-1299,
                    source="test",
                    external_id=f"watch-{flow_kind}-{month}",
                    source_document_id=None,
                    source_confidence=1.0,
                    flow_kind=flow_kind,
                )
                assert txn_id is not None
                repo_ledger.insert_split(
                    conn,
                    transaction_id=txn_id,
                    category_id=category_id,
                    amount_cents=-1299,
                )
                repo_statements.mark_cleared(conn, txn_id, f"2026-{month}-15")

    with engine.read_conn(app_env) as conn:
        merchants = {
            row["merchant"]
            for row in conn.execute(
                "SELECT merchant FROM v_subscription_watchlist_candidates"
            )
        }
    assert "Unknown Watch" not in merchants
    assert "Transfer Watch" not in merchants


def test_insights_page_renders_watchlist_and_persists_actions(app_env):
    account_id, _ = _seed_subscription_fixture(app_env)
    client = _client()

    page = client.get("/insights?month=2026-06")
    assert page.status_code == 200
    body = page.text
    assert "new recurring watchlist" in body
    assert "Fresh Stream" in body
    assert "$12.99" in body
    assert "2026-07-15" in body
    assert "One Off Shop" not in body.split("new recurring watchlist", 1)[1].split("recurring changes", 1)[0]
    assert "Known Cloud" not in body.split("new recurring watchlist", 1)[1].split("recurring changes", 1)[0]
    assert "marked subscription" in body
    assert "not a subscription" in body
    assert "already known" in body
    assert "watch next month" in body

    action = client.post(
        "/insights/subscriptions/action",
        data={
            "merchant": "Fresh Stream",
            "account_id": str(account_id),
            "decision": "subscription",
            "month": "2026-06",
        },
        follow_redirects=False,
    )
    assert action.status_code == 303
    assert action.headers["location"] == "/insights?month=2026-06"

    with engine.read_conn(app_env) as conn:
        decision = conn.execute(
            """
            SELECT decision
            FROM subscription_watchlist_decisions
            WHERE merchant='Fresh Stream' AND account_id=?
            """,
            (account_id,),
        ).fetchone()
        rows = repo_budgets.subscription_watchlist_rows(conn, "2026-06")

    assert decision["decision"] == "subscription"
    assert rows[0]["decision_label"] == "marked subscription"
