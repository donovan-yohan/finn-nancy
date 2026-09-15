"""FN-103 — soft lock + post-close edit audit guard.

Covers the guard primitive, the three write paths it protects (manual ledger
edit/delete, approval-queue recategorization apply, bulk-recategorize sweep) and
the audit rows an override produces.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.actions.recategorization import RecategorizationHandler
from app.backlog.suggest import list_uncategorized_expense_backlog
from app.db import engine, repo_actions, repo_close, repo_ledger


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _account(conn) -> int:
    cur = conn.execute(
        "INSERT INTO accounts(name, institution, kind, currency) VALUES ('Acct','','cash','CAD')"
    )
    return int(cur.lastrowid)


def _txn(conn, *, account_id, posted_on, amount_cents, category_id,
         source="manual", recon_status="uncleared", flow_kind="unknown") -> int:
    cur = conn.execute(
        """INSERT INTO transactions(account_id, posted_on, description, counterparty,
             amount_cents, source, external_id, recon_status, flow_kind)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (account_id, posted_on, "desc", "", amount_cents, source,
         f"ext:{posted_on}:{amount_cents}:{category_id}", recon_status, flow_kind),
    )
    txn_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,?)",
        (txn_id, category_id, amount_cents),
    )
    return txn_id


# ---------------------------------------------------------------------------
# guard primitive
# ---------------------------------------------------------------------------

def test_transaction_month_reads_posted_on(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
    with engine.read_conn(empty_db) as conn:
        assert repo_close.transaction_month(conn, txn_id) == "2026-03"
        assert repo_close.transaction_month(conn, 999999) is None


def test_guard_blocks_write_in_closed_month(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
        repo_close.mark_closed(conn, "2026-03")
    with engine.read_conn(empty_db) as conn:
        with pytest.raises(repo_close.MonthLockedError) as exc:
            repo_close.guard_transaction_write(conn, txn_id)
        assert exc.value.month == "2026-03"
        # MonthLockedError degrades through existing ValueError handling.
        assert isinstance(exc.value, ValueError)


def test_guard_override_returns_locked_months(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
        repo_close.mark_closed(conn, "2026-03")
    # An override is a write capability: it atomically reopens the period and
    # appends its receipt before the guarded mutation can proceed.
    with engine.write_tx(empty_db) as conn:
        assert repo_close.guard_transaction_write(conn, txn_id, override=True) == ["2026-03"]
        assert repo_close.is_month_locked(conn, "2026-03") is False
        assert conn.execute(
            "SELECT COUNT(*) FROM period_write_overrides WHERE month='2026-03'"
        ).fetchone()[0] == 1


def test_guard_open_and_reopened_months_are_writable(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        open_txn = _txn(conn, account_id=acct, posted_on="2026-03-11",
                        amount_cents=-500, category_id=uncat)
        reopened_txn = _txn(conn, account_id=acct, posted_on="2026-02-11",
                            amount_cents=-500, category_id=uncat)
        repo_close.mark_closed(conn, "2026-02")
        repo_close.reopen(conn, "2026-02")
    with engine.read_conn(empty_db) as conn:
        assert repo_close.guard_transaction_write(conn, open_txn) == []
        assert repo_close.guard_transaction_write(conn, reopened_txn) == []


def test_guard_flags_target_month_when_edit_moves_date(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        # Transaction currently lives in an open month, but is being moved into a
        # closed one — the guard must catch the destination too.
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
        repo_close.mark_closed(conn, "2026-02")
    with engine.read_conn(empty_db) as conn:
        with pytest.raises(repo_close.MonthLockedError):
            repo_close.guard_transaction_write(conn, txn_id, extra_month="2026-02")


def test_central_repository_blocks_new_transaction_in_closed_month(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        category_id = repo_ledger.ensure_uncategorized(conn)
        repo_close.mark_closed(conn, "2026-03")
    with pytest.raises(repo_close.MonthLockedError):
        with engine.write_tx(empty_db) as conn:
            txn_id = repo_ledger.insert_transaction(
                conn,
                account_id=account_id,
                posted_on="2026-03-12",
                description="closed insert",
                counterparty="",
                amount_cents=-500,
                source="api",
                external_id="central-closed-insert",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
            if txn_id is not None:
                repo_ledger.insert_split(
                    conn,
                    transaction_id=txn_id,
                    category_id=category_id,
                    amount_cents=-500,
                )
    with engine.read_conn(empty_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE external_id='central-closed-insert'"
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# approval-queue recategorization apply
# ---------------------------------------------------------------------------

def test_recategorization_apply_blocked_in_closed_month(empty_db):
    handler = RecategorizationHandler()
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        target = int(conn.execute(
            "INSERT INTO categories(name, kind) VALUES ('Groceries','expense')"
        ).lastrowid)
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
        repo_close.mark_closed(conn, "2026-03")
    with engine.write_tx(empty_db) as conn:
        with pytest.raises(repo_close.MonthLockedError):
            handler.apply(conn, {"transaction_id": txn_id, "to_category_id": target})
    # The blocked apply must not have moved the split.
    with engine.read_conn(empty_db) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=?", (txn_id,)
        ).fetchone()
        assert int(split["category_id"]) == uncat
        assert [r for r in repo_close.list_audit(conn, "2026-03")
                if r["entity"] == "transaction"] == []


def test_recategorization_apply_override_writes_single_audit_row(empty_db):
    handler = RecategorizationHandler()
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        target = int(conn.execute(
            "INSERT INTO categories(name, kind) VALUES ('Groceries','expense')"
        ).lastrowid)
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
        repo_close.mark_closed(conn, "2026-03")
    with engine.write_tx(empty_db) as conn:
        result = handler.apply(
            conn, {"transaction_id": txn_id, "to_category_id": target, "override": True}
        )
        assert result["noop"] is False
    with engine.read_conn(empty_db) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=?", (txn_id,)
        ).fetchone()
        assert int(split["category_id"]) == target
        audit = [r for r in repo_close.list_audit(conn, "2026-03") if r["entity"] == "transaction"]
        assert len(audit) == 1
        row = audit[0]
        assert row["entity_id"] == txn_id
        assert row["field"] == "category_id"
        assert (row["old_value"], row["new_value"]) == (str(uncat), str(target))


def test_recategorization_revert_blocked_in_closed_month(empty_db):
    handler = RecategorizationHandler()
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        target = int(conn.execute(
            "INSERT INTO categories(name, kind) VALUES ('Groceries','expense')"
        ).lastrowid)
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
        # Applied while the month was still open, then the month is closed.
        revert = handler.apply(conn, {"transaction_id": txn_id, "to_category_id": target})["revert"]
        repo_close.mark_closed(conn, "2026-03")
    with engine.write_tx(empty_db) as conn:
        with pytest.raises(repo_close.MonthLockedError):
            handler.revert(conn, revert)
    # A blocked revert must not have moved the split back or written an audit row.
    with engine.read_conn(empty_db) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=?", (txn_id,)
        ).fetchone()
        assert int(split["category_id"]) == target
        assert [r for r in repo_close.list_audit(conn, "2026-03")
                if r["entity"] == "transaction"] == []


def test_recategorization_revert_override_restores_and_audits(empty_db):
    handler = RecategorizationHandler()
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        uncat = repo_ledger.ensure_uncategorized(conn)
        target = int(conn.execute(
            "INSERT INTO categories(name, kind) VALUES ('Groceries','expense')"
        ).lastrowid)
        txn_id = _txn(conn, account_id=acct, posted_on="2026-03-11",
                      amount_cents=-500, category_id=uncat)
        revert = handler.apply(conn, {"transaction_id": txn_id, "to_category_id": target})["revert"]
        repo_close.mark_closed(conn, "2026-03")
    with engine.write_tx(empty_db) as conn:
        handler.revert(conn, {**revert, "override": True})
    with engine.read_conn(empty_db) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=?", (txn_id,)
        ).fetchone()
        assert int(split["category_id"]) == uncat  # restored
        audit = [r for r in repo_close.list_audit(conn, "2026-03") if r["entity"] == "transaction"]
        assert len(audit) == 1
        assert audit[0]["field"] == "category_id"
        assert (audit[0]["old_value"], audit[0]["new_value"]) == (str(target), str(uncat))


# ---------------------------------------------------------------------------
# bulk-recategorize sweep exclusions
# ---------------------------------------------------------------------------

def _seed_backlog(conn):
    acct = _account(conn)
    uncat = repo_ledger.ensure_uncategorized(conn)
    open_txn = _txn(conn, account_id=acct, posted_on="2026-03-11",
                    amount_cents=-900, category_id=uncat, flow_kind="purchase")
    closed_txn = _txn(conn, account_id=acct, posted_on="2026-01-11",
                      amount_cents=-800, category_id=uncat, flow_kind="purchase")
    reconciled_txn = _txn(conn, account_id=acct, posted_on="2026-03-12",
                          amount_cents=-700, category_id=uncat, recon_status="cleared",
                          flow_kind="purchase")
    repo_close.mark_closed(conn, "2026-01")
    return {"open": open_txn, "closed": closed_txn, "reconciled": reconciled_txn}


def test_bulk_sweep_excludes_closed_but_keeps_cleared_unresolved_by_default(empty_db):
    with engine.write_tx(empty_db) as conn:
        ids = _seed_backlog(conn)
    with engine.read_conn(empty_db) as conn:
        got = {int(r["id"]) for r in list_uncategorized_expense_backlog(conn, limit=None)}
    assert ids["open"] in got
    assert ids["closed"] not in got
    assert ids["reconciled"] in got


def test_bulk_sweep_includes_closed_month_when_toggled(empty_db):
    with engine.write_tx(empty_db) as conn:
        ids = _seed_backlog(conn)
    with engine.read_conn(empty_db) as conn:
        got = {int(r["id"]) for r in
               list_uncategorized_expense_backlog(conn, limit=None, include_closed=True)}
    assert ids["open"] in got
    assert ids["closed"] in got
    # Cleared rows still need explicit category evidence before they leave the backlog.
    assert ids["reconciled"] in got


# ---------------------------------------------------------------------------
# manual ledger route (blocked + inline message + audited override)
# ---------------------------------------------------------------------------

def _ledger_client():
    from app.web.routes.ledger import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _close_month(db_path, month):
    with engine.write_tx(db_path) as conn:
        repo_close.mark_closed(conn, month)


def test_ledger_edit_blocked_in_closed_month_shows_inline_message(app_env):
    # Sample txn 3 posts 2026-01-05, single split in Groceries (id 4).
    _close_month(app_env, "2026-01")
    client = _ledger_client()
    r = client.post(
        "/txn/3/edit",
        data={"posted_on": "2026-01-05", "description": "weekly groceries",
              "counterparty": "", "notes": "", "amount": "162.43", "category_id": "5"},
        follow_redirects=False,
    )
    # Inline re-render (200) carrying the lock message — not a raw 400 or a redirect.
    assert r.status_code == 200
    body = r.text.lower()
    assert "2026-01 is closed" in body
    assert 'name="override"' in body
    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=3"
        ).fetchone()
        assert int(split["category_id"]) == 4  # unchanged
        assert [r for r in repo_close.list_audit(conn, "2026-01")
                if r["entity"] == "transaction"] == []


def test_ledger_edit_override_succeeds_and_audits(app_env):
    _close_month(app_env, "2026-01")
    client = _ledger_client()
    r = client.post(
        "/txn/3/edit",
        data={"posted_on": "2026-01-05", "description": "weekly groceries",
              "counterparty": "", "notes": "", "amount": "162.43",
              "category_id": "5", "override": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=3"
        ).fetchone()
        assert int(split["category_id"]) == 5
        audit = [r for r in repo_close.list_audit(conn, "2026-01") if r["entity"] == "transaction"]
        assert len(audit) == 1
        assert audit[0]["entity_id"] == 3
        assert (audit[0]["field"], audit[0]["old_value"], audit[0]["new_value"]) == (
            "category_id", "4", "5")


def test_ledger_delete_blocked_then_override_audits(app_env):
    _close_month(app_env, "2026-01")
    client = _ledger_client()
    blocked = client.post("/txn/3/delete", data={}, follow_redirects=False)
    assert blocked.status_code == 200
    assert "2026-01 is closed" in blocked.text.lower()
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT 1 FROM transactions WHERE id=3").fetchone() is not None

    ok = client.post("/txn/3/delete", data={"override": "1"}, follow_redirects=False)
    assert ok.status_code == 303
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT 1 FROM transactions WHERE id=3").fetchone() is None
        audit = [r for r in repo_close.list_audit(conn, "2026-01") if r["field"] == "deleted"]
        assert len(audit) == 1
        assert audit[0]["entity_id"] == 3


def test_ledger_override_date_move_audits_posted_on(app_env):
    # An override edit that only moves the date must log posted_on (not a misleading
    # amount no-op) — the guard's own extra_month case, surfaced through the route.
    _close_month(app_env, "2026-01")
    with engine.read_conn(app_env) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=3").fetchone()
        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=3"
        ).fetchone()
    amount = f"{abs(int(split['amount_cents'])) / 100:.2f}"
    client = _ledger_client()
    r = client.post(
        "/txn/3/edit",
        data={"posted_on": "2026-01-20", "description": txn["description"],
              "counterparty": txn["counterparty"] or "", "notes": txn["notes"] or "",
              "amount": amount, "category_id": str(int(split["category_id"])),
              "override": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        audit = [r for r in repo_close.list_audit(conn, "2026-01") if r["entity"] == "transaction"]
        assert len(audit) == 1
        assert audit[0]["field"] == "posted_on"
        assert (audit[0]["old_value"], audit[0]["new_value"]) == ("2026-01-05", "2026-01-20")


# ---------------------------------------------------------------------------
# approval-queue route: override reachable through the UI, blocked otherwise
# ---------------------------------------------------------------------------

def _actions_client():
    from app.web.app import create_app

    return TestClient(create_app())


def _enqueue_recat(db_path, *, transaction_id, to_category_id):
    with engine.write_tx(db_path) as conn:
        return repo_actions.enqueue_proposal(
            conn,
            kind="recategorization",
            payload={"transaction_id": transaction_id, "to_category_id": to_category_id},
            evidence={"transaction_ids": [transaction_id]},
            confidence=0.9,
            rationale="test recat",
            agent_run_id="run-guard",
        )


def test_approvals_queue_shows_override_for_locked_proposal(app_env):
    # Sample txn 3 posts 2026-01-05 (Groceries id 4); target category 5 exists.
    _enqueue_recat(app_env, transaction_id=3, to_category_id=5)
    _close_month(app_env, "2026-01")
    body = _actions_client().get("/actions").text
    assert 'name="override"' in body
    assert "2026-01 is closed" in body


def test_approve_route_blocked_then_override_via_form(app_env):
    proposal_id = _enqueue_recat(app_env, transaction_id=3, to_category_id=5)
    _close_month(app_env, "2026-01")
    client = _actions_client()
    blocked = client.post(f"/actions/{proposal_id}/approve", follow_redirects=False)
    assert blocked.status_code == 400
    assert "closed" in blocked.text.lower()
    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=3"
        ).fetchone()
        assert int(split["category_id"]) == 4  # unchanged
    ok = client.post(
        f"/actions/{proposal_id}/approve", data={"override": "1"}, follow_redirects=False
    )
    assert ok.status_code == 303
    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=3"
        ).fetchone()
        assert int(split["category_id"]) == 5
        audit = [r for r in repo_close.list_audit(conn, "2026-01") if r["entity"] == "transaction"]
        assert len(audit) == 1
        assert audit[0]["entity_id"] == 3


def test_revert_route_blocked_then_override_via_form(app_env):
    proposal_id = _enqueue_recat(app_env, transaction_id=3, to_category_id=5)
    client = _actions_client()
    # Approve while the month is still open, then close it.
    assert client.post(
        f"/actions/{proposal_id}/approve", follow_redirects=False
    ).status_code == 303
    _close_month(app_env, "2026-01")
    blocked = client.post(f"/actions/{proposal_id}/revert", follow_redirects=False)
    assert blocked.status_code == 400
    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=3"
        ).fetchone()
        assert int(split["category_id"]) == 5  # still applied
    ok = client.post(
        f"/actions/{proposal_id}/revert", data={"override": "1"}, follow_redirects=False
    )
    assert ok.status_code == 303
    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT category_id FROM transaction_splits WHERE transaction_id=3"
        ).fetchone()
        assert int(split["category_id"]) == 4  # restored to original
        audit = [r for r in repo_close.list_audit(conn, "2026-01")
                 if r["reason"] == "override revert recategorization"]
        assert len(audit) == 1
