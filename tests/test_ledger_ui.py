from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import engine, repo_close


def _client():
    from app.web.routes.ledger import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _new(client, **overrides):
    data = {
        "posted_on": "2026-04-01",
        "description": "test txn",
        "counterparty": "",
        "amount": "10.00",
        "account_id": "1",
        "category_id": "4",  # Groceries, expense
        "notes": "",
    }
    data.update(overrides)
    return client.post("/txn/new", data=data, follow_redirects=False)


def test_new_form_renders(app_env):
    r = _client().get("/txn/new")
    assert r.status_code == 200
    body = r.text.lower()
    assert "amount" in body and "account" in body and "category" in body


def test_edit_form_renders(app_env):
    r = _client().get("/txn/3/edit")
    assert r.status_code == 200
    assert "weekly groceries" in r.text.lower()


def test_delete_form_requires_confirm_naming_txn(app_env):
    # FN-116: the delete transaction form must guard the mutating POST with a
    # confirm step whose copy names the specific transaction being removed.
    r = _client().get("/txn/3/edit")
    assert r.status_code == 200
    assert '/txn/3/delete' in r.text
    assert 'onsubmit=\'return confirm(' in r.text
    assert 'confirm("Delete this transaction? weekly groceries")' in r.text


def test_add_expense(app_env):
    client = _client()
    r = _new(client, description="corner store", amount="12.34", category_id="4")
    assert r.status_code == 303
    assert r.headers["location"] == "/activity"

    with engine.read_conn(app_env) as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE source='manual' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        splits = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn["id"],)
        ).fetchall()

    assert txn["source"] == "manual"
    assert txn["amount_cents"] == -1234
    assert len(splits) == 1
    assert splits[0]["amount_cents"] == -1234
    assert splits[0]["category_id"] == 4


def test_add_income(app_env):
    client = _client()
    r = _new(client, description="side gig", amount="500.00", category_id="1")  # Salary, income
    assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE source='manual' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert txn["amount_cents"] == 50000


def test_add_transaction_refuses_closed_posted_month(app_env):
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, "2026-04")
    response = _new(
        _client(),
        description="closed month manual",
        flow_kind="purchase",
    )
    assert response.status_code == 409
    assert "2026-04 is closed" in response.text
    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE description='closed month manual'"
        ).fetchone()[0] == 0


def test_add_refund_keeps_expense_purpose_but_uses_positive_flow_direction(app_env):
    client = _client()
    response = _new(
        client,
        description="grocery refund",
        amount="12.34",
        category_id="4",
        flow_kind="refund",
    )
    assert response.status_code == 303

    with engine.read_conn(app_env) as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE source='manual' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?",
            (txn["id"],),
        ).fetchone()
    assert txn["flow_kind"] == "refund"
    assert txn["amount_cents"] == 1234
    assert split["category_id"] == 4
    assert split["amount_cents"] == 1234


def test_edit_single_split_rewrites_amount_and_category(app_env):
    client = _client()
    # txn id 3: weekly groceries, -16243 cents, category 4 (Groceries)
    r = client.post(
        "/txn/3/edit",
        data={
            "posted_on": "2026-01-05",
            "description": "weekly groceries (updated)",
            "counterparty": "Synthetic Market",
            "notes": "corrected",
            "amount": "99.99",
            "category_id": "5",  # Restaurants, expense
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/activity"

    with engine.read_conn(app_env) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=3").fetchone()
        splits = conn.execute("SELECT * FROM transaction_splits WHERE transaction_id=3").fetchall()

    assert txn["description"] == "weekly groceries (updated)"
    assert txn["amount_cents"] == -9999
    assert len(splits) == 1
    assert splits[0]["amount_cents"] == -9999
    assert splits[0]["category_id"] == 5


def test_edit_opening_txn_amount_category_rejected(app_env):
    with engine.write_tx(app_env) as conn:
        cat_id = conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) "
            "VALUES ('Opening Balance','transfer','shared','#9AA5B1')"
        ).lastrowid
        txn_id = conn.execute(
            "INSERT INTO transactions(account_id, posted_on, description, counterparty, "
            "amount_cents, source, external_id) "
            "VALUES (1, '2026-01-01', 'Opening balance', '', 100000, 'opening', 'open:1')"
        ).lastrowid
        conn.execute(
            "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,100000)",
            (txn_id, cat_id),
        )

    with engine.read_conn(app_env) as conn:
        before = {r["month"]: r["income_cents"] for r in conn.execute("SELECT * FROM v_cashflow_monthly")}

    client = _client()
    r = client.post(
        f"/txn/{txn_id}/edit",
        data={
            "posted_on": "2026-01-01",
            "description": "Opening balance",
            "counterparty": "",
            "notes": "",
            "amount": "1000.00",
            "category_id": "1",  # Salary, income — would reclassify the opening balance
        },
    )
    assert r.status_code == 400

    with engine.read_conn(app_env) as conn:
        after = {r["month"]: r["income_cents"] for r in conn.execute("SELECT * FROM v_cashflow_monthly")}
        txn = conn.execute("SELECT amount_cents FROM transactions WHERE id=?", (txn_id,)).fetchone()
    assert before == after
    assert txn["amount_cents"] == 100000


def test_dollars_to_cents_half_cent_rounds_up():
    from fastapi import HTTPException

    from app.web.forms import dollars_to_cents

    assert dollars_to_cents("19.995") == 2000
    with pytest.raises(HTTPException) as exc_info:
        dollars_to_cents("abc")
    assert exc_info.value.status_code == 400


def test_add_expense_half_cent_rounds_via_shared_helper(app_env):
    client = _client()
    r = _new(client, description="half cent", amount="19.995", category_id="4")
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE source='manual' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert txn["amount_cents"] == -2000


def test_delete(app_env):
    client = _client()
    r = client.post("/txn/4/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/activity"

    with engine.read_conn(app_env) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=4").fetchone()
        splits = conn.execute("SELECT * FROM transaction_splits WHERE transaction_id=4").fetchall()

    assert txn is None
    assert splits == []


def test_bad_dollars_is_400(app_env):
    client = _client()
    r = _new(client, amount="abc")
    assert r.status_code == 400


def test_unknown_category_is_400_or_404(app_env):
    client = _client()
    r = _new(client, category_id="999")
    assert r.status_code in (400, 404)


def test_unknown_account_is_400_or_404(app_env):
    client = _client()
    r = _new(client, account_id="999")
    assert r.status_code in (400, 404)


def test_two_manual_txns_do_not_collide(app_env):
    client = _client()
    for i in range(2):
        r = _new(client, description=f"manual {i}")
        assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        rows = conn.execute("SELECT * FROM transactions WHERE source='manual'").fetchall()

    assert len(rows) == 2
    assert len({row["external_id"] for row in rows}) == 2
