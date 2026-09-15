from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import engine


def _client():
    from app.web.app import create_app

    return TestClient(create_app())


def test_budget_page_sets_leisure_budget_and_insights(app_env):
    client = _client()
    with engine.read_conn(app_env) as conn:
        sample_member_a = conn.execute(
            "SELECT * FROM household_members WHERE name='Sample Member A'"
        ).fetchone()

    r = client.post(
        "/categories/5/budget",
        data={"amount": "150.00", "owner_member_id": str(sample_member_a["id"]), "is_leisure": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/categories"

    with engine.read_conn(app_env) as conn:
        budget = conn.execute(
            "SELECT * FROM budgets WHERE category_id=5 AND period_month=''"
        ).fetchone()
        category = conn.execute("SELECT is_leisure FROM categories WHERE id=5").fetchone()
        actual = conn.execute(
            """
            SELECT * FROM v_budget_vs_actual
            WHERE month='2026-03' AND category_id=5
            """
        ).fetchone()
        underspend = conn.execute(
            """
            SELECT * FROM v_category_underspend_monthly
            WHERE month='2026-03' AND category_id=5
            """
        ).fetchone()
        leisure = conn.execute(
            "SELECT * FROM v_leisure_vs_bigticket WHERE month='2026-03'"
        ).fetchone()

    assert budget["amount_cents"] == 15000
    assert budget["owner_member_id"] == sample_member_a["id"]
    assert category["is_leisure"] == 1
    assert actual["budget_cents"] == 15000
    assert actual["budget_owner"] == "Sample Member A"
    assert actual["actual_cents"] == 6760
    assert actual["remaining_cents"] == 8240
    assert underspend["underspend_cents"] == 8240
    assert leisure["leisure_cents"] == 6760
    assert leisure["bigticket_cents"] == 210000

    page = client.get("/insights?month=2026-03")
    assert page.status_code == 200
    body = page.text
    assert "Restaurants" in body
    assert "$150.00" in body
    assert "$67.60" in body
    assert "$82.40" in body
    assert "leisure $67.60" in body


def test_budget_members_can_be_added_and_assigned(app_env):
    client = _client()

    r = client.post("/categories/members", data={"name": "Alex"}, follow_redirects=False)
    assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        alex = conn.execute("SELECT * FROM household_members WHERE name='Alex'").fetchone()
    assert alex is not None

    r2 = client.post(
        "/categories/4/budget",
        data={"amount": "500.00", "owner_member_id": str(alex["id"])},
        follow_redirects=False,
    )
    assert r2.status_code == 303

    with engine.read_conn(app_env) as conn:
        budget = conn.execute(
            "SELECT * FROM budgets WHERE category_id=4 AND period_month=''"
        ).fetchone()
        view_row = conn.execute(
            "SELECT * FROM v_budget_vs_actual WHERE month='2026-03' AND category_id=4"
        ).fetchone()

    assert budget["owner_member_id"] == alex["id"]
    assert view_row["budget_owner"] == "Alex"


def test_big_ticket_threshold_update_changes_classification(app_env):
    client = _client()

    r = client.post(
        "/categories/settings/big-ticket",
        data={"amount": "170.00"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        setting = conn.execute(
            "SELECT value FROM app_settings WHERE key='big_ticket_threshold_cents'"
        ).fetchone()
        jan = conn.execute(
            "SELECT * FROM v_leisure_vs_bigticket WHERE month='2026-01'"
        ).fetchone()

    assert setting["value"] == "17000"
    # January has only rent at or above the new threshold.
    assert jan["bigticket_cents"] == 210000
