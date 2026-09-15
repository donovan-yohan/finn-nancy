"""FN-107: YNAB-style forced reconciliation adjustment.

From a balance-assertion exception (FN-106), a one-click action books a clearly
tagged, reversible adjustment transaction that zeroes the assertion delta, clears the
exception, and writes a close_audit row. These tests cover the delta math and sign,
that the exception clears, that the adjustment is identifiable and reversible, the
audit trail, idempotency, the tie no-op, and the FN-103 closed-month guard.
"""
from __future__ import annotations

from urllib.parse import unquote_plus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import (
    engine,
    repo_assertions,
    repo_close,
    repo_close_inbox,
    repo_ledger,
    repo_period_policy,
)
from app.reconcile import adjust, assertions
from app.web.routes import close, recon

MONTH = "2026-06"
ASOF = f"{MONTH}-30"


# --- seeding ------------------------------------------------------------------

def _account(conn, name: str = "Chequing", kind: str = "chequing") -> int:
    return int(
        conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency) VALUES (?, 'Test', ?, 'CAD')",
            (name, kind),
        ).lastrowid
    )


def _category(conn, name: str = "Rent") -> int:
    existing = conn.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if existing:
        return int(existing["id"])
    return int(
        conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES (?, 'expense', 'nancy', '#ff9f43')",
            (name,),
        ).lastrowid
    )


def _expense(conn, *, account_id, category_id, amount_cents, posted_on=f"{MONTH}-02",
             external_id="rent-jun") -> int:
    txn_id = repo_ledger.insert_transaction(
        conn, account_id=account_id, posted_on=posted_on, description="Rent charge",
        counterparty="Landlord", amount_cents=-abs(amount_cents), source="test",
        external_id=external_id, source_document_id=None, source_confidence=1.0,
        flow_kind="purchase",
    )
    repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=category_id,
                             amount_cents=-abs(amount_cents))
    return int(txn_id)


def _seed_under_exception(db: str) -> int:
    """Ledger totals -100000 through ASOF; assertion says -90000 → under by 10000.

    Adjustment must book +10000 to move the ledger onto -90000. Returns account id.
    """
    with engine.write_tx(db) as conn:
        acct = _account(conn)
        cat = _category(conn)
        _expense(conn, account_id=acct, category_id=cat, amount_cents=100000)
        repo_assertions.record_assertion(conn, account_id=acct, asof_date=ASOF,
                                         asserted_cents=-90000)
    return acct


def _exception_count(db: str, account_id: int) -> int:
    with engine.read_conn(db) as conn:
        return len(assertions.scan_assertion_exceptions(conn, month=MONTH, account_id=account_id))


# --- delta math + exception clearing -----------------------------------------

def test_adjustment_zeroes_delta_and_clears_exception(empty_db):
    acct = _seed_under_exception(empty_db)
    assert _exception_count(empty_db, acct) == 1

    with engine.write_tx(empty_db) as conn:
        result = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)

    assert result.status == "created"
    assert result.delta_cents == -10000        # ledger - asserted
    assert result.amount_cents == 10000         # -delta_cents, moves ledger onto asserted
    # The exception is gone and the ledger now ties exactly to the asserted balance.
    assert _exception_count(empty_db, acct) == 0
    with engine.read_conn(empty_db) as conn:
        assert assertions.ledger_balance_cents(conn, acct, ASOF) == -90000


def test_adjustment_over_exception_books_negative(empty_db):
    # Ledger -100000, asserted -110000 → over by 10000; adjustment must be -10000.
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        cat = _category(conn)
        _expense(conn, account_id=acct, category_id=cat, amount_cents=100000)
        repo_assertions.record_assertion(conn, account_id=acct, asof_date=ASOF,
                                         asserted_cents=-110000)
        result = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)

    assert result.delta_cents == 10000
    assert result.amount_cents == -10000
    assert _exception_count(empty_db, acct) == 0


# --- tagged, reversible, audited ---------------------------------------------

def test_adjustment_is_tagged_and_split_signed(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        result = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)

    with engine.read_conn(empty_db) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (result.transaction_id,)).fetchone()
        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (result.transaction_id,)
        ).fetchone()
    assert txn["source"] == "adjustment"
    assert txn["counterparty"] == "Reconciliation adjustment"
    assert txn["amount_cents"] == 10000
    assert txn["posted_on"] == ASOF
    # A correctly-signed split so reports (which read transaction_splits) see it.
    assert split["amount_cents"] == 10000


def test_adjustment_is_reversible_by_deletion(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        result = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    assert _exception_count(empty_db, acct) == 0

    # Deleting the adjustment transaction (splits cascade) restores the exception —
    # this is the ordinary txn-delete path the FN-103 guard sits on.
    with engine.write_tx(empty_db) as conn:
        conn.execute("DELETE FROM transactions WHERE id=?", (result.transaction_id,))
    assert _exception_count(empty_db, acct) == 1


def test_adjustment_writes_audit_row(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        result = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)

    with engine.read_conn(empty_db) as conn:
        rows = repo_close.list_audit(conn, MONTH)
    adj = [r for r in rows if r["field"] == "reconciliation_adjustment"]
    assert len(adj) == 1
    assert adj[0]["entity"] == "transaction"
    assert adj[0]["entity_id"] == result.transaction_id
    assert adj[0]["new_value"] == "10000"
    assert "forced reconciliation adjustment" in adj[0]["reason"]


# --- idempotency + tie no-op --------------------------------------------------

def test_adjustment_is_idempotent(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        first = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    with engine.write_tx(empty_db) as conn:
        second = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)

    # Second call is a no-op: the ledger already ties, so it reports 'tie' and books
    # nothing new (one adjustment transaction total).
    assert first.status == "created"
    assert second.status == "tie"
    with engine.read_conn(empty_db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='adjustment'").fetchone()[0]
    assert n == 1


def test_tie_is_a_noop(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        cat = _category(conn)
        _expense(conn, account_id=acct, category_id=cat, amount_cents=90000)
        repo_assertions.record_assertion(conn, account_id=acct, asof_date=ASOF,
                                         asserted_cents=-90000)
        result = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    assert result.status == "tie"
    with engine.read_conn(empty_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE source='adjustment'").fetchone()[0] == 0


def test_missing_assertion_returns_no_assertion(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        result = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    assert result.status == "no_assertion"


# --- amount-aware re-booking after drift --------------------------------------

def _backdate_expense(conn, *, account_id, category_id, amount_cents, external_id):
    """An expense posted on ASOF that arrives *after* an adjustment cleared the
    exception — on-or-before the asof, so it re-opens the delta."""
    return _expense(conn, account_id=account_id, category_id=category_id,
                    amount_cents=amount_cents, posted_on=ASOF, external_id=external_id)


def test_rebook_after_backdated_txn_clears_exception(empty_db):
    acct = _seed_under_exception(empty_db)  # ledger -100000, asserted -90000
    with engine.write_tx(empty_db) as conn:
        first = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    assert first.status == "created"
    assert first.amount_cents == 10000
    assert _exception_count(empty_db, acct) == 0

    # A backdated expense lands on-or-before ASOF, so the delta re-opens (under by 5000).
    with engine.write_tx(empty_db) as conn:
        cat = _category(conn)
        _backdate_expense(conn, account_id=acct, category_id=cat, amount_cents=5000,
                          external_id="late-arrival")
    assert _exception_count(empty_db, acct) == 1

    # Re-invoking re-books the *same* adjustment in place instead of no-op'ing on the
    # amount-blind key: the exception clears and exactly one adjustment txn remains,
    # moved to the corrected amount (+15000).
    with engine.write_tx(empty_db) as conn:
        second = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    assert second.status == "rebooked"
    assert second.transaction_id == first.transaction_id
    assert second.amount_cents == 15000
    assert _exception_count(empty_db, acct) == 0

    with engine.read_conn(empty_db) as conn:
        txns = conn.execute(
            "SELECT id, amount_cents FROM transactions WHERE source='adjustment'"
        ).fetchall()
        split = conn.execute(
            "SELECT amount_cents FROM transaction_splits WHERE transaction_id=?",
            (first.transaction_id,),
        ).fetchone()
        assert assertions.ledger_balance_cents(conn, acct, ASOF) == -90000
        audit = [r for r in repo_close.list_audit(conn, MONTH)
                 if r["field"] == "reconciliation_adjustment"]
    assert len(txns) == 1                          # exactly one adjustment txn, still
    assert int(txns[0]["amount_cents"]) == 15000
    assert int(split["amount_cents"]) == 15000     # split moves too (reports read splits)
    # Two audit rows: the original booking, then the revision.
    assert len(audit) == 2
    assert audit[-1]["old_value"] == "10000"
    assert audit[-1]["new_value"] == "15000"
    assert "revised from" in audit[-1]["reason"]


def test_rebook_in_closed_month_respects_lock(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        first = adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    # Drift re-opens the exception; then the month closes.
    with engine.write_tx(empty_db) as conn:
        cat = _category(conn)
        _backdate_expense(conn, account_id=acct, category_id=cat, amount_cents=5000,
                          external_id="late-arrival")
        repo_close.mark_closed(conn, MONTH)
    assert _exception_count(empty_db, acct) == 1

    # Even with an adjustment already on file, a re-book into a closed month refuses
    # without override (FN-103) and changes nothing.
    with engine.write_tx(empty_db) as conn:
        with pytest.raises(repo_close.MonthLockedError):
            adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    with engine.read_conn(empty_db) as conn:
        amt = conn.execute("SELECT amount_cents FROM transactions WHERE id=?",
                           (first.transaction_id,)).fetchone()[0]
    assert int(amt) == 10000
    assert _exception_count(empty_db, acct) == 1

    # With override it re-books in place and records an override-tagged audit row.
    with engine.write_tx(empty_db) as conn:
        res = adjust.create_adjustment(
            conn,
            account_id=acct,
            asof_date=ASOF,
            override=True,
            actor="test:override-operator",
            override_reason="rebook a reviewed closed-period balance adjustment",
            operation_key="test:adjustment:rebook-override",
        )
    assert res.status == "rebooked"
    assert res.amount_cents == 15000
    assert _exception_count(empty_db, acct) == 0
    with engine.read_conn(empty_db) as conn:
        audit = [r for r in repo_close.list_audit(conn, MONTH)
                 if r["field"] == "reconciliation_adjustment"]
        state = repo_period_policy.current_state(conn, MONTH)
        override_count = conn.execute(
            "SELECT COUNT(*) FROM period_write_overrides WHERE month=?",
            (MONTH,),
        ).fetchone()[0]
    assert any("override" in r["reason"] for r in audit)
    assert state == "reopened"
    assert override_count == 1


# --- FN-103 closed-month guard ------------------------------------------------

def test_closed_month_refuses_without_override(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        repo_close.mark_closed(conn, MONTH)

    with engine.write_tx(empty_db) as conn:
        with pytest.raises(repo_close.MonthLockedError):
            adjust.create_adjustment(conn, account_id=acct, asof_date=ASOF)
    assert _exception_count(empty_db, acct) == 1  # nothing booked


def test_closed_month_override_requires_caller_stable_operation_key(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        repo_close.mark_closed(conn, MONTH)
    with engine.read_conn(empty_db) as conn:
        state_before = repo_period_policy.current_state(conn, MONTH)

    with engine.write_tx(empty_db) as conn:
        with pytest.raises(repo_period_policy.PeriodPolicyError, match="operation_key is required"):
            adjust.create_adjustment(
                conn,
                account_id=acct,
                asof_date=ASOF,
                override=True,
                actor="test:override-operator",
                override_reason="book a reviewed closed-period balance adjustment",
                operation_key="   ",
            )

    assert _exception_count(empty_db, acct) == 1
    with engine.read_conn(empty_db) as conn:
        assert repo_period_policy.current_state(conn, MONTH) == state_before
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM period_write_overrides WHERE month=?",
                (MONTH,),
            ).fetchone()[0]
            == 0
        )


def test_closed_month_override_books_and_audits(empty_db):
    acct = _seed_under_exception(empty_db)
    with engine.write_tx(empty_db) as conn:
        repo_close.mark_closed(conn, MONTH)
        result = adjust.create_adjustment(
            conn,
            account_id=acct,
            asof_date=ASOF,
            override=True,
            actor="test:override-operator",
            override_reason="book a reviewed closed-period balance adjustment",
            operation_key="test:adjustment:create-override",
        )

    assert result.status == "created"
    assert _exception_count(empty_db, acct) == 0
    with engine.read_conn(empty_db) as conn:
        rows = repo_close.list_audit(conn, MONTH)
        state = repo_period_policy.current_state(conn, MONTH)
        override_count = conn.execute(
            "SELECT COUNT(*) FROM period_write_overrides WHERE month=?",
            (MONTH,),
        ).fetchone()[0]
    adj = [r for r in rows if r["field"] == "reconciliation_adjustment"]
    assert len(adj) == 1
    assert "override" in adj[0]["reason"]
    assert state == "reopened"
    assert override_count == 1


# --- route --------------------------------------------------------------------

def _client() -> TestClient:
    app = FastAPI()
    app.include_router(recon.router)
    return TestClient(app, follow_redirects=False)


def _full_client() -> TestClient:
    """recon + close routers together, so a POST-redirect-GET can land on /close."""
    app = FastAPI()
    app.include_router(recon.router)
    app.include_router(close.router)
    return TestClient(app, follow_redirects=False)


def test_route_books_adjustment_and_clears_inbox(app_env):
    with engine.write_tx(app_env) as conn:
        acct = _account(conn)
        cat = _category(conn)
        _expense(conn, account_id=acct, category_id=cat, amount_cents=100000)
        repo_assertions.record_assertion(conn, account_id=acct, asof_date=ASOF,
                                         asserted_cents=-90000)

    with engine.read_conn(app_env) as conn:
        before = repo_close_inbox.build_inbox(conn, MONTH)
    assert any(i.source == "assertion" and i.account_id == acct for i in before)

    resp = _client().post("/recon/assertion/adjust",
                          data={"account_id": str(acct), "asof_date": ASOF})
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/close?month={MONTH}"

    with engine.read_conn(app_env) as conn:
        after = repo_close_inbox.build_inbox(conn, MONTH)
    assert not [i for i in after if i.source == "assertion" and i.account_id == acct]


def test_route_404_when_no_assertion(app_env):
    with engine.write_tx(app_env) as conn:
        acct = _account(conn)
    resp = _client().post("/recon/assertion/adjust",
                          data={"account_id": str(acct), "asof_date": ASOF})
    assert resp.status_code == 404


def _seed_locked_exception(db: str) -> int:
    with engine.write_tx(db) as conn:
        acct = _account(conn)
        cat = _category(conn)
        _expense(conn, account_id=acct, category_id=cat, amount_cents=100000)
        repo_assertions.record_assertion(conn, account_id=acct, asof_date=ASOF,
                                         asserted_cents=-90000)
        repo_close.mark_closed(conn, MONTH)
    return acct


def test_route_locked_month_without_override_shows_notice(app_env):
    acct = _seed_locked_exception(app_env)

    resp = _client().post("/recon/assertion/adjust",
                          data={"account_id": str(acct), "asof_date": ASOF})
    # Graceful FN-103 handling: no raw 400 — POST-redirect-GET to /close with a notice.
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert location.startswith(f"/close?month={MONTH}")
    assert "is closed" in location
    assert "notice=" in resp.headers["location"]
    # Nothing booked while the lock stands.
    assert _exception_count(app_env, acct) == 1


def test_route_override_books_and_audits_on_locked_month(app_env):
    acct = _seed_locked_exception(app_env)

    base = {
        "account_id": str(acct),
        "asof_date": ASOF,
        "override": "1",
        "actor": "test:legacy-recon-operator",
        "reason": "accept reviewed balance difference",
        "confirm_adjustment": "1",
        "operation_key": "test:legacy-recon:assertion-adjust",
    }
    for missing in ("actor", "reason", "confirm_adjustment", "operation_key"):
        refused_data = dict(base)
        refused_data.pop(missing)
        refused = _client().post(
            "/recon/assertion/adjust",
            data=refused_data,
        )
        assert refused.status_code == 400
    blank_key_data = dict(base)
    blank_key_data["operation_key"] = "   "
    blank_key = _client().post(
        "/recon/assertion/adjust",
        data=blank_key_data,
    )
    assert blank_key.status_code == 400

    resp = _client().post(
        "/recon/assertion/adjust",
        data=base,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/close?month={MONTH}"  # plain success, no notice
    assert _exception_count(app_env, acct) == 0
    with engine.read_conn(app_env) as conn:
        audit = [r for r in repo_close.list_audit(conn, MONTH)
                 if r["field"] == "reconciliation_adjustment"]
        state = repo_period_policy.current_state(conn, MONTH)
        override_count = conn.execute(
            "SELECT COUNT(*) FROM period_write_overrides WHERE month=?",
            (MONTH,),
        ).fetchone()[0]
    assert len(audit) == 1
    assert "override" in audit[0]["reason"]
    assert state == "reopened"
    assert override_count == 1


def test_route_surfaces_residual_exists_as_notice(app_env, monkeypatch):
    # A residual 'exists' (the adjustment could not be (re)booked) must not 303 as if it
    # had succeeded — it comes back as an inline notice.
    def _fake(
        conn,
        *,
        account_id,
        asof_date,
        override=False,
        actor="",
        override_reason="",
        operation_key="",
    ):
        return adjust.AdjustmentResult(status="exists", account_id=account_id,
                                       asof_date=asof_date, delta_cents=-10000,
                                       amount_cents=10000)

    monkeypatch.setattr(adjust, "create_adjustment", _fake)
    resp = _client().post("/recon/assertion/adjust",
                          data={"account_id": "1", "asof_date": ASOF})
    assert resp.status_code == 303
    location = unquote_plus(resp.headers["location"])
    assert location.startswith(f"/close?month={MONTH}")
    assert "still open" in location
    assert "notice=" in resp.headers["location"]


def test_close_page_locked_exception_uses_audited_override_and_signed_amount(
    app_env,
):
    acct = _seed_locked_exception(app_env)

    client = _full_client()
    page = client.get(f"/close?month={MONTH}")
    assert page.status_code == 200
    assert 'action="/close/assertion/adjust"' in page.text
    assert 'name="actor"' in page.text
    assert 'name="reason"' in page.text
    assert 'name="confirm_adjustment"' in page.text
    assert 'name="operation_key"' in page.text
    assert 'name="override"' not in page.text
    assert "reopens the month before booking" in page.text
    # Confirm dialog shows the *booked* amount (+$100.00), not the raw delta (finding 3).
    assert "adjustment for $100.00" in page.text
    assert "adjustment for -$100.00" not in page.text

    base = {
        "month": MONTH,
        "account_id": str(acct),
        "asof_date": ASOF,
        "actor": "test:close-operator",
        "reason": "accept reviewed balance difference",
        "confirm_adjustment": "1",
        "operation_key": "test:close-page:assertion-adjust",
    }
    for missing in ("actor", "reason", "confirm_adjustment", "operation_key"):
        refused_data = dict(base)
        refused_data.pop(missing)
        refused = client.post(
            "/close/assertion/adjust",
            data=refused_data,
            follow_redirects=False,
        )
        assert refused.status_code == 400
    blank_key_data = dict(base)
    blank_key_data["operation_key"] = "   "
    blank_key = client.post(
        "/close/assertion/adjust",
        data=blank_key_data,
        follow_redirects=False,
    )
    assert blank_key.status_code == 400

    with engine.read_conn(app_env) as conn:
        assert repo_period_policy.current_state(conn, MONTH) == "clean_closed"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM period_write_overrides WHERE month=?",
                (MONTH,),
            ).fetchone()[0]
            == 0
        )

    adjusted = client.post(
        "/close/assertion/adjust",
        data=base,
        follow_redirects=False,
    )
    assert adjusted.status_code == 303
    location = unquote_plus(adjusted.headers["location"])
    assert location.startswith(f"/close?month={MONTH}")
    assert "audited override" in location
    assert _exception_count(app_env, acct) == 0

    retry = client.post(
        "/close/assertion/adjust",
        data=base,
        follow_redirects=False,
    )
    assert retry.status_code == 409
    assert "month is no longer closed" in retry.text

    with engine.read_conn(app_env) as conn:
        assert repo_period_policy.current_state(conn, MONTH) == "reopened"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM period_write_overrides WHERE month=?",
                (MONTH,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM transactions
                WHERE source='adjustment' AND account_id=? AND posted_on=?
                """,
                (acct, ASOF),
            ).fetchone()[0]
            == 1
        )
        history = repo_period_policy.list_snapshot_history(conn, MONTH)
    assert len(history) == 1
    assert int(history[0]["is_current"]) == 0


def test_close_page_renders_redirected_notice(app_env):
    _seed_locked_exception(app_env)
    page = _full_client().get(f"/close?month={MONTH}&notice=Chequing+is+closed")
    assert page.status_code == 200
    assert "Chequing is closed" in page.text
    assert 'class="banner warn' in page.text
