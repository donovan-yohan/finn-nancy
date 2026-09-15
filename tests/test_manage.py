from __future__ import annotations

import re
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import (
    engine,
    repo_captures,
    repo_close,
    repo_ledger,
    repo_statement_expectations,
)
from app.web.routes.manage import router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_create_account(app_env):
    client = _client()
    r = client.post(
        "/manage/accounts",
        data={"name": "Travel card", "institution": "Fake Bank", "kind": "credit",
              "external_ref": "fake:credit:1234"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE name='Travel card'").fetchone()
    assert row is not None
    assert row["kind"] == "credit"
    assert row["external_ref"] == "fake:credit:1234"
    with engine.read_conn(app_env) as conn:
        policy = conn.execute(
            """SELECT * FROM account_statement_policies
               WHERE account_id=? ORDER BY effective_from_month DESC, id DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
    assert policy["configuration_state"] == "unconfigured"
    assert policy["requirement_mode"] is None


def test_update_account_is_active(app_env):
    client = _client()
    r = client.post(
        "/manage/accounts/1",
        data={"name": "Day-to-day chequing", "institution": "Fake Credit Union", "kind": "chequing",
              "external_ref": "", "is_active": "0"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id=1").fetchone()
    assert row["is_active"] == 0


def test_manage_renders_separate_statement_policy_controls(app_env):
    response = _client().get("/manage")
    assert response.status_code == 200
    assert "Account statement policies" in response.text
    assert "already prepared months keep their audited snapshot" in response.text
    assert 'action="/manage/accounts/3/statement-policy"' in response.text
    assert 'name="effective_from_month"' in response.text
    assert 'name="active_from"' in response.text
    assert 'name="active_to"' in response.text
    assert 'name="reason"' in response.text


def test_manage_discloses_capture_transport_classes_and_content_free_metrics(app_env):
    response = _client().get("/manage")

    assert response.status_code == 200
    assert 'id="capture-privacy"' in response.text
    assert "Capture transports" in response.text
    assert "strict-local on" in response.text
    assert response.text.count('class="capture-transport-class local_only"') == 4
    assert 'class="capture-transport-class direct_network"' in response.text
    assert "Direct network" in response.text
    assert "Third-party" in response.text
    assert "Blocked by strict-local mode." in response.text
    assert 'aria-label="Content-free capture reliability"' in response.text
    assert "It excludes receipt text, merchants, account details, filenames, paths," in response.text
    assert "chat identifiers, tokens, and raw errors." in response.text


def test_telegram_consent_requires_ack_and_never_overrides_strict_local(
    app_env, monkeypatch
):
    client = _client()

    missing_ack = client.post(
        "/manage/capture-transports/telegram",
        data={"decision": "consented"},
        follow_redirects=False,
    )
    assert missing_ack.status_code == 400
    with engine.read_conn(app_env) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM capture_transport_consents").fetchone()[0]
            == 0
        )

    consented = client.post(
        "/manage/capture-transports/telegram",
        data={"decision": "consented", "acknowledge": "1"},
        follow_redirects=False,
    )
    assert consented.status_code == 303
    assert consented.headers["location"] == "/manage#capture-privacy"
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            "SELECT * FROM capture_transport_consents ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["decision"] == "consented"
        assert row["disclosure_version"] == repo_captures.TRANSPORT_DISCLOSURE_VERSION
        assert not repo_captures.transport_allowed(
            conn, "telegram", strict_local_mode=True
        )

    strict_page = client.get("/manage")
    assert "Revoke Telegram consent" in strict_page.text
    assert "Blocked by strict-local mode." in strict_page.text

    monkeypatch.setenv("STRICT_LOCAL_MODE", "false")
    get_settings.cache_clear()
    enabled_page = client.get("/manage")
    assert "third-party opt-in allowed" in enabled_page.text
    assert "Allowed by consent, but Telegram credentials are incomplete." in enabled_page.text

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    get_settings.cache_clear()
    configured_page = client.get("/manage")
    assert "Allowed and configured; the poller is starting or paused." in configured_page.text

    from app.channels.telegram import TelegramRuntimeState

    running_app = FastAPI()
    running_app.state.telegram_runtime = TelegramRuntimeState(
        configured=True, allowed=True, running=True
    )
    running_app.include_router(router)
    running_page = TestClient(running_app).get("/manage")
    assert "Allowed, configured, and running." in running_page.text

    revoked = client.post(
        "/manage/capture-transports/telegram",
        data={"decision": "revoked"},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    with engine.read_conn(app_env) as conn:
        decisions = conn.execute(
            """
            SELECT decision
            FROM capture_transport_consents
            WHERE transport='telegram'
            ORDER BY id
            """
        ).fetchall()
    assert [row["decision"] for row in decisions] == ["consented", "revoked"]


def test_manage_uses_current_policy_and_labels_future_version(app_env):
    current_month = date.today().strftime("%Y-%m")
    future_month = f"{date.today().year + 1:04d}-01"
    with engine.write_tx(app_env) as conn:
        repo_statement_expectations.record_policy(
            conn,
            account_id=3,
            effective_from_month=current_month,
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            actor="test:current-policy",
            reason="current synthetic policy",
        )
        repo_statement_expectations.record_policy(
            conn,
            account_id=3,
            effective_from_month=future_month,
            configuration_state="configured",
            requirement_mode="no_statement",
            cadence="none",
            actor="test:future-policy",
            reason="future synthetic policy",
        )

    response = _client().get("/manage")
    assert response.status_code == 200
    assert f"current for {current_month}: configured" in response.text
    assert re.search(
        rf"future from {future_month}: configured\s*· no_statement\s*· none",
        response.text,
    )
    assert re.search(
        r'<select form="statement-policy-3" name="requirement_mode".*?'
        r'<option value="required"\s+selected>',
        response.text,
        re.DOTALL,
    )
    assert re.search(
        r'<select form="statement-policy-3" name="cadence".*?'
        r'<option value="monthly"\s+selected>',
        response.text,
        re.DOTALL,
    )


def test_record_statement_policy_appends_audited_version(app_env):
    response = _client().post(
        "/manage/accounts/3/statement-policy",
        data={
            "effective_from_month": "2026-04",
            "configuration_state": "configured",
            "requirement_mode": "required",
            "cadence": "quarterly",
            "anchor_month": "1",
            "active_from": "2026-01-15",
            "active_to": "",
            "reason": "issuer produces quarterly statements",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        policy = conn.execute(
            """SELECT * FROM account_statement_policies
               WHERE account_id=3
               ORDER BY effective_from_month DESC, id DESC
               LIMIT 1"""
        ).fetchone()
        audit = conn.execute(
            """SELECT * FROM statement_expectation_audit
               WHERE policy_id=? AND event_kind='policy_recorded'""",
            (policy["id"],),
        ).fetchone()
    assert policy["effective_from_month"] == "2026-04"
    assert policy["configuration_state"] == "configured"
    assert policy["requirement_mode"] == "required"
    assert policy["cadence"] == "quarterly"
    assert policy["anchor_month"] == 1
    assert policy["active_from"] == "2026-01-15"
    assert policy["created_by"] == "web:manage"
    assert audit["reason"] == "issuer produces quarterly statements"


@pytest.mark.parametrize(
    ("configuration_state", "requirement_mode", "cadence", "anchor_month", "expected"),
    (
        ("unconfigured", "required", "quarterly", "4", (None, None, None)),
        ("configured", "no_statement", "quarterly", "4", ("no_statement", "none", None)),
        ("configured", "required", "monthly", "4", ("required", "monthly", None)),
    ),
)
def test_statement_policy_post_canonicalizes_inapplicable_fields(
    app_env,
    configuration_state,
    requirement_mode,
    cadence,
    anchor_month,
    expected,
):
    response = _client().post(
        "/manage/accounts/3/statement-policy",
        data={
            "effective_from_month": "2026-08",
            "configuration_state": configuration_state,
            "requirement_mode": requirement_mode,
            "cadence": cadence,
            "anchor_month": anchor_month,
            "reason": "canonicalize stale form controls",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        policy = conn.execute(
            """SELECT requirement_mode, cadence, anchor_month
               FROM account_statement_policies
               WHERE account_id=3
               ORDER BY id DESC
               LIMIT 1"""
        ).fetchone()
    assert tuple(policy) == expected


def test_statement_policy_rejects_invalid_or_retroactive_closed_configuration(app_env):
    client = _client()
    invalid = client.post(
        "/manage/accounts/3/statement-policy",
        data={
            "effective_from_month": "2026-04",
            "configuration_state": "configured",
            "requirement_mode": "required",
            "cadence": "quarterly",
            "anchor_month": "",
            "reason": "missing anchor mutant",
        },
    )
    assert invalid.status_code == 400

    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, "2026-04", reason="synthetic close")
    closed = client.post(
        "/manage/accounts/3/statement-policy",
        data={
            "effective_from_month": "2026-04",
            "configuration_state": "configured",
            "requirement_mode": "required",
            "cadence": "monthly",
            "anchor_month": "",
            "reason": "must not reach into a closed month",
        },
    )
    assert closed.status_code == 409


def test_create_category(app_env):
    client = _client()
    r = client.post(
        "/manage/categories",
        data={"name": "Pet care", "kind": "expense", "brand_owner": "nancy", "color": "#FF0000"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT * FROM categories WHERE name='Pet care'").fetchone()
    assert row is not None
    assert row["brand_owner"] == "nancy"


def test_create_category_bad_kind_rejected(app_env):
    client = _client()
    r = client.post(
        "/manage/categories",
        data={"name": "Nonsense", "kind": "bogus", "brand_owner": "finn", "color": "#FFFFFF"},
    )
    assert r.status_code == 400


def test_create_duplicate_category_name_rejected(app_env):
    client = _client()
    r = client.post(
        "/manage/categories",
        data={"name": "Groceries", "kind": "expense", "brand_owner": "finn", "color": "#000000"},
    )
    assert r.status_code == 400
    with engine.read_conn(app_env) as conn:
        n = conn.execute("SELECT COUNT(*) FROM categories WHERE name='Groceries'").fetchone()[0]
    assert n == 1


def test_rename_category_to_existing_name_rejected(app_env):
    client = _client()
    # id 5 = Restaurants; renaming it to the already-taken "Groceries" must 400, not 500.
    r = client.post(
        "/manage/categories/5",
        data={"name": "Groceries", "brand_owner": "nancy", "color": "#FFFFFF"},
    )
    assert r.status_code == 400
    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT name FROM categories WHERE id=5").fetchone()
    assert row["name"] == "Restaurants"


def test_uncategorized_rename_rejected(app_env):
    with engine.write_tx(app_env) as conn:
        uncategorized_id = repo_ledger.ensure_uncategorized(conn)

    client = _client()
    r = client.post(
        f"/manage/categories/{uncategorized_id}",
        data={"name": "Renamed", "brand_owner": "finn", "color": "#9AA5B1"},
    )
    assert r.status_code == 400

    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT name FROM categories WHERE id=?", (uncategorized_id,)).fetchone()
    assert row["name"] == "Uncategorized"

    # recoloring (same name) is fine
    r2 = client.post(
        f"/manage/categories/{uncategorized_id}",
        data={"name": "Uncategorized", "brand_owner": "finn", "color": "#123456"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT color FROM categories WHERE id=?", (uncategorized_id,)).fetchone()
    assert row["color"] == "#123456"


def test_opening_balance_insert_and_update(app_env):
    client = _client()
    r = client.post(
        "/manage/accounts/1/opening",
        data={"amount": "1234.56", "posted_on": "2026-01-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE source='opening' AND external_id='open:1'"
        ).fetchone()
        splits = conn.execute(
            "SELECT s.*, c.name AS category_name, c.kind AS category_kind FROM transaction_splits s "
            "JOIN categories c ON c.id = s.category_id WHERE s.transaction_id=?",
            (txn["id"],),
        ).fetchall()
    assert txn["amount_cents"] == 123456
    assert len(splits) == 1
    assert splits[0]["amount_cents"] == 123456
    assert splits[0]["category_name"] == "Opening Balance"
    assert splits[0]["category_kind"] == "transfer"

    # posting again updates the same txn rather than inserting a new one
    r2 = client.post(
        "/manage/accounts/1/opening",
        data={"amount": "999.00", "posted_on": "2026-02-01"},
        follow_redirects=False,
    )
    assert r2.status_code == 303

    with engine.read_conn(app_env) as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE source='opening' AND external_id='open:1'"
        ).fetchall()
        all_opening = conn.execute(
            "SELECT * FROM transactions WHERE account_id=1 AND source='opening'"
        ).fetchall()
    assert len(rows) == 1
    assert len(all_opening) == 1
    assert rows[0]["id"] == txn["id"]
    assert rows[0]["amount_cents"] == 99900
    assert rows[0]["posted_on"] == "2026-02-01"


def test_opening_balance_negative_for_credit_card(app_env):
    client = _client()
    r = client.post(
        "/manage/accounts/3/opening",
        data={"amount": "-500.25", "posted_on": "2026-01-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        txn = conn.execute(
            "SELECT * FROM transactions WHERE source='opening' AND external_id='open:3'"
        ).fetchone()
    assert txn["amount_cents"] == -50025


def test_opening_balance_category_kind_conflict_rejected(app_env):
    # Name 'Opening Balance' already taken by a non-transfer category: must 400, not
    # silently reuse it (that would leak an opening balance into income/expense totals).
    with engine.write_tx(app_env) as conn:
        conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) "
            "VALUES ('Opening Balance','expense','shared','#000000')"
        )
    with engine.read_conn(app_env) as conn:
        before = {r["month"]: (r["income_cents"], r["expense_cents"])
                  for r in conn.execute("SELECT * FROM v_cashflow_monthly")}

    client = _client()
    r = client.post(
        "/manage/accounts/1/opening",
        data={"amount": "500.00", "posted_on": "2026-01-01"},
    )
    assert r.status_code == 400

    with engine.read_conn(app_env) as conn:
        after = {r["month"]: (r["income_cents"], r["expense_cents"])
                 for r in conn.execute("SELECT * FROM v_cashflow_monthly")}
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source='opening' AND external_id='open:1'"
        ).fetchone()[0]
    assert before == after
    assert n == 0


def test_opening_balance_does_not_affect_cashflow(app_env):
    with engine.read_conn(app_env) as conn:
        before = {r["month"]: (r["income_cents"], r["expense_cents"])
                  for r in conn.execute("SELECT * FROM v_cashflow_monthly")}

    client = _client()
    r = client.post(
        "/manage/accounts/1/opening",
        data={"amount": "1000.00", "posted_on": "2026-01-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        after = {r["month"]: (r["income_cents"], r["expense_cents"])
                 for r in conn.execute("SELECT * FROM v_cashflow_monthly")}

    assert before == after
