"""FN-101: the unified /close checklist page + sign-off/reopen lifecycle."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.close import checklist, period_exceptions
from app.config import get_settings
from app.db import (
    engine,
    repo_budgets,
    repo_close,
    repo_ledger,
    repo_merchant_knowledge,
    repo_period_policy,
    repo_statement_expectations,
)
from app.db.repo_merchant_knowledge import Evidence
from app.web.routes import close

FIXTURE_MONTH = "2026-05"  # no seed ledger data lands here — a clean slate to load.


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(close.router)
    return TestClient(app)


def _signoff_data(**overrides) -> dict[str, str]:
    data = {
        "month": FIXTURE_MONTH,
        "actor": "test:operator",
        "reason": "reviewed generated close evidence",
        "confirm_close": "1",
    }
    data.update(overrides)
    return data


def _reopen_data(**overrides) -> dict[str, str]:
    data = {
        "month": FIXTURE_MONTH,
        "actor": "test:operator",
        "reason": "reopen to correct evidence",
        "confirm_reopen": "1",
    }
    data.update(overrides)
    return data


def _add_uncategorized_expense(db: str, *, month: str = FIXTURE_MONTH) -> None:
    with engine.write_tx(db) as conn:
        cat_id = repo_ledger.ensure_uncategorized(conn)
        account_id = repo_ledger.ensure_default_account(conn)
        txn_id = repo_ledger.insert_transaction(
            conn, account_id=account_id, posted_on=f"{month}-09",
            description="MYSTERY CHARGE", counterparty="", amount_cents=-4200,
            source="manual", external_id=f"uncat-{month}", source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=cat_id, amount_cents=-4200)


def _add_categorized_expense(
    db: str,
    *,
    month: str = FIXTURE_MONTH,
    external_id: str = "confirmed-close-expense",
    confirm: bool = True,
) -> int:
    with engine.write_tx(db) as conn:
        category = repo_ledger.find_category_by_name(conn, "Groceries")
        account_id = repo_ledger.ensure_default_account(conn)
        txn_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=account_id,
                posted_on=f"{month}-12",
                description="CONFIRMED GROCER",
                counterparty="Confirmed Grocer",
                amount_cents=-3300,
                source="manual",
                external_id=f"{external_id}-{month}",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
        )
        split_id = repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=int(category["id"]),
            amount_cents=-3300,
        )
        if confirm:
            repo_merchant_knowledge.confirm_category(
                conn,
                descriptor="CONFIRMED GROCER",
                category_id=int(category["id"]),
                scope=repo_merchant_knowledge.scope_for_transaction(conn, txn_id),
                operation_key=f"test:close-category:{txn_id}",
                actor="test:operator",
                reason="operator confirmed close-page category evidence",
                evidence=Evidence(
                    transaction_id=txn_id,
                    transaction_split_id=split_id,
                ),
            )
        return txn_id


def _add_unmatched_statement_line(db: str, *, month: str = FIXTURE_MONTH) -> None:
    with engine.write_tx(db) as conn:
        cur = conn.execute(
            "INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status) "
            "VALUES ('statement','may.pdf','blobs/may','feed','application/pdf','needs_review')"
        )
        doc_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description, norm_merchant,
                 amount_cents, currency, is_pending, row_hash, match_status)
               VALUES (?,?,?,?,?,?, 'CAD', 0, ?, 'unmatched')""",
            (doc_id, 1, f"{month}-11", "UNKNOWN VENDOR", "UNKNOWN VENDOR", -9900, f"h-{month}"),
        )


def _add_unknown_flow(db: str, *, month: str = FIXTURE_MONTH) -> int:
    with engine.write_tx(db) as conn:
        account_id = repo_ledger.ensure_default_account(conn)
        return int(
            repo_ledger.insert_transaction(
                conn,
                account_id=account_id,
                posted_on=f"{month}-13",
                description="AMBIGUOUS MOVEMENT",
                counterparty="",
                amount_cents=-1700,
                source="manual",
                external_id=f"unknown-flow-{month}",
                source_document_id=None,
                source_confidence=0.5,
                flow_kind="unknown",
            )
        )


def _waive_required_matrix(db: str, month: str = FIXTURE_MONTH) -> None:
    with engine.write_tx(db) as conn:
        rows = repo_statement_expectations.prepare_period(
            conn,
            month=month,
            actor="test:close",
            reason="materialize unrelated close-page fixture",
        )
        for row in rows:
            if (
                row["requirement_state"] == "required"
                and row["lifecycle_state"] == "expected"
            ):
                repo_statement_expectations.waive(
                    conn,
                    int(row["id"]),
                    actor="test:waiver",
                    reason="synthetic fixture explicitly has no statement",
                )


def _acknowledge_all_preclose(
    db: str,
    *,
    month: str = FIXTURE_MONTH,
) -> None:
    with engine.write_tx(db) as conn:
        items = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            month,
            period_exceptions.collect_period_exceptions(conn, month),
        )
        for index, item in enumerate(items):
            if item["is_acknowledged"]:
                continue
            repo_period_policy.acknowledge_preclose_exception(
                conn,
                month,
                item,
                actor="test:preclose-reviewer",
                reason="reviewed active typed exception before close",
                operation_key=f"test:preclose-ack:{month}:{index}",
                evidence={"fixture": "generated close evidence"},
            )


# --- read-model unit coverage -------------------------------------------------

def test_build_checklist_flags_backlog_and_coverage(app_env):
    _add_uncategorized_expense(app_env)
    _add_unmatched_statement_line(app_env)
    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)

    rows = {r["key"]: r for r in check["rows"]}
    assert set(rows) == {"coverage", "flow_review", "backlog", "variance", "goals"}
    assert rows["backlog"]["complete"] is False
    assert rows["backlog"]["count"] == 1
    assert rows["backlog"]["blocking"] is True
    assert rows["coverage"]["complete"] is False
    assert rows["coverage"]["count"] == 3
    assert rows["flow_review"]["complete"] is True
    # Two signals are red, so the page is not fully ready.
    assert check["percent_complete"] < 100
    assert check["all_complete"] is False
    # Deep links point at the existing resolve surfaces.
    assert rows["backlog"]["href"] == "/backlog"
    assert rows["coverage"]["href"] == f"/recon?month={FIXTURE_MONTH}"
    assert rows["flow_review"]["href"] == (
        f"/recon?month={FIXTURE_MONTH}#positive-flow-review"
    )
    assert rows["variance"]["href"] == "/insights"
    assert rows["goals"]["href"].startswith("/goals/close")


def test_build_checklist_without_statement_evidence_is_incomplete(app_env):
    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
    coverage = next(row for row in check["rows"] if row["key"] == "coverage")
    assert coverage["complete"] is False
    assert coverage["coverage_pct"] == 0.0
    assert coverage["detail"] == "no statement evidence uploaded"
    assert check["all_complete"] is False


# --- route-level rendering ----------------------------------------------------

def test_close_page_renders_rows_and_percent(app_env):
    _add_uncategorized_expense(app_env)
    _add_unmatched_statement_line(app_env)
    r = _client().get(f"/close?month={FIXTURE_MONTH}")
    assert r.status_code == 200
    assert "Statement coverage" in r.text
    assert "Transaction meaning" in r.text
    assert "Expense categories confirmed" in r.text
    assert "Budget variance" in r.text
    assert "Goal funding" in r.text
    assert "% ready" in r.text
    # Deep links present.
    assert f'href="/recon?month={FIXTURE_MONTH}"' in r.text
    assert 'href="/backlog"' in r.text
    # Category-evidence items prevent a clean label but remain truthfully
    # closable as typed exceptions.
    assert 'action="/close/signoff"' in r.text
    assert "cannot turn an exception into a clean close" in " ".join(
        r.text.split()
    )
    assert 'name="inbox_ack"' not in r.text
    assert 'disabled aria-disabled="true"' in r.text
    assert 'data-policy-blocked="true"' in r.text
    assert "exception" in r.text
    assert "first" in r.text
    assert 'action="/close/prepare"' in r.text
    assert "Prepare audited matrix" in r.text
    assert "/close/preclose-exception/exception:" not in r.text
    assert "sign off" in r.text.lower()
    assert 'data-close-state="open"' in r.text
    assert 'data-exception-class="unconfirmed_merchant_category"' in r.text


def test_unknown_flow_is_disclosed_and_forces_exception_close(app_env):
    _waive_required_matrix(app_env)
    transaction_id = _add_unknown_flow(app_env)
    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
        row = next(item for item in check["rows"] if item["key"] == "flow_review")
        pending = conn.execute(
            """SELECT COUNT(*) FROM transaction_flow_reviews
               WHERE transaction_id=? AND status='pending'""",
            (transaction_id,),
        ).fetchone()[0]

    assert row["complete"] is False
    assert row["count"] == 1
    assert row["missing_review_count"] == 0
    assert check["all_complete"] is False
    assert check["has_blocking_flow_reviews"] is True
    assert pending == 1

    client = _client()
    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert "unresolved item" in page.text
    assert 'disabled aria-disabled="true"' in page.text
    response = client.post(
        "/close/signoff",
        data=_signoff_data(reason="close with typed flow exception"),
        follow_redirects=False,
    )
    assert response.status_code == 400
    with engine.read_conn(app_env) as conn:
        assert repo_close.is_month_locked(conn, FIXTURE_MONTH) is False

    _acknowledge_all_preclose(app_env)
    response = client.post(
        "/close/signoff",
        data=_signoff_data(
            reason="close with reviewed typed flow exception",
            operation_key="test:unknown-flow:close",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        assert repo_close.is_month_locked(conn, FIXTURE_MONTH) is True
        assert (
            conn.execute(
                "SELECT state FROM v_current_period_close_state WHERE month=?",
                (FIXTURE_MONTH,),
            ).fetchone()["state"]
            == "closed_with_exceptions"
        )


def test_close_page_defaults_to_a_valid_month(app_env):
    r = _client().get("/close")
    assert r.status_code == 200
    assert "% ready" in r.text


# --- sign-off / reopen lifecycle ---------------------------------------------

def test_signoff_then_reopen_transition_and_audit(app_env):
    _add_categorized_expense(app_env)
    _waive_required_matrix(app_env)
    with engine.read_conn(app_env) as conn:
        assert (
            checklist.build_checklist(conn, FIXTURE_MONTH)[
                "has_blocking_expense_resolutions"
            ]
            is False
        )
    client = _client()

    # Sign off closes and locks the month, snapshotting metrics.
    r = client.post(
        "/close/signoff",
        data=_signoff_data(reason="clean month"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        assert repo_close.is_month_locked(conn, FIXTURE_MONTH) is True
        period = repo_close.get_period(conn, FIXTURE_MONTH)
    assert period["status"] == "closed"

    # A closed month renders read-only with a reopen control (no sign-off form).
    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert 'action="/close/reopen"' in page.text
    assert 'action="/close/signoff"' not in page.text
    assert 'data-close-state="clean_closed"' in page.text
    assert 'data-snapshot-current="true"' in page.text

    # Reopen unlocks it and records a reopen audit row.
    r = client.post(
        "/close/reopen",
        data=_reopen_data(reason="needs a fix"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        assert repo_close.is_month_locked(conn, FIXTURE_MONTH) is False
        period = repo_close.get_period(conn, FIXTURE_MONTH)
        audit = repo_close.list_audit(conn, FIXTURE_MONTH)
    assert period["status"] == "reopened"
    reopen_rows = [a for a in audit if a["new_value"] == "reopened"]
    assert len(reopen_rows) == 1
    assert reopen_rows[0]["reason"] == "needs a fix"


def test_signoff_keeps_planning_variance_non_authoritative(app_env):
    transaction_id = _add_categorized_expense(app_env)
    _waive_required_matrix(app_env)
    with engine.write_tx(app_env) as conn:
        category_id = int(
            conn.execute(
                """
                SELECT category_id
                FROM transaction_splits
                WHERE transaction_id=?
                """,
                (transaction_id,),
            ).fetchone()[0]
        )
        repo_budgets.set_category_budget(
            conn,
            category_id=category_id,
            amount_cents=1000,
            period_month=FIXTURE_MONTH,
        )
    client = _client()
    r = client.post(
        "/close/signoff",
        data=_signoff_data(reason="accounting evidence is complete"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        period = repo_close.get_period(conn, FIXTURE_MONTH)
        snapshot = conn.execute(
            """SELECT snapshot_json
               FROM v_current_period_close_snapshot
               WHERE month=?""",
            (FIXTURE_MONTH,),
        ).fetchone()
    assert period["variance_ack"] == 0
    assert period["status"] == "closed"
    assert '"planning_signals_are_non_authoritative":true' in snapshot["snapshot_json"]


def test_category_evidence_forces_exception_close_not_clean_close(app_env):
    _add_uncategorized_expense(app_env)
    _waive_required_matrix(app_env)
    response = _client().post(
        "/close/signoff",
        data=_signoff_data(reason="close with unresolved category evidence"),
        follow_redirects=False,
    )
    assert response.status_code == 400
    _acknowledge_all_preclose(app_env)
    response = _client().post(
        "/close/signoff",
        data=_signoff_data(
            reason="close with reviewed unresolved category evidence",
            operation_key="test:category-exception:close",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
        assert check["has_blocking_expense_resolutions"] is True
        state = conn.execute(
            "SELECT state FROM v_current_period_close_state WHERE month=?",
            (FIXTURE_MONTH,),
        ).fetchone()["state"]
        assert state == "closed_with_exceptions"
        assert repo_close.is_month_locked(conn, FIXTURE_MONTH) is True


def test_assigned_category_without_accepted_evidence_is_a_hard_blocker(app_env):
    _add_categorized_expense(
        app_env,
        external_id="assigned-without-evidence",
        confirm=False,
    )
    _waive_required_matrix(app_env)
    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
    assert check["expense_resolution_count"] == 1
    assert check["uncategorized_count"] == 0
    assert check["has_blocking_expense_resolutions"] is True

    response = _client().post(
        "/close/signoff",
        data=_signoff_data(reason="close with assigned but unproven category"),
        follow_redirects=False,
    )
    assert response.status_code == 400
    _acknowledge_all_preclose(app_env)
    response = _client().post(
        "/close/signoff",
        data=_signoff_data(
            reason="close with reviewed assigned category exception",
            operation_key="test:assigned-category-exception:close",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        state = conn.execute(
            "SELECT state FROM v_current_period_close_state WHERE month=?",
            (FIXTURE_MONTH,),
        ).fetchone()["state"]
    assert state == "closed_with_exceptions"


def test_each_purchase_split_requires_its_own_accepted_category_evidence(app_env):
    with engine.write_tx(app_env) as conn:
        account_id = repo_ledger.ensure_default_account(conn)
        groceries = repo_ledger.find_category_by_name(conn, "Groceries")
        restaurants = repo_ledger.find_category_by_name(conn, "Restaurants")
        txn_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=account_id,
                posted_on=f"{FIXTURE_MONTH}-18",
                description="SPLIT PURCHASE",
                counterparty="Split Merchant",
                amount_cents=-5000,
                source="manual",
                external_id="close-multi-split",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
        )
        accepted_split_id = repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=int(groceries["id"]),
            amount_cents=-3000,
        )
        repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=int(restaurants["id"]),
            amount_cents=-2000,
        )
        repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="SPLIT PURCHASE",
            category_id=int(groceries["id"]),
            scope=repo_merchant_knowledge.scope_for_transaction(conn, txn_id),
            operation_key=f"test:close-multi-split:{txn_id}",
            actor="test:operator",
            reason="operator confirmed only one split category",
            evidence=Evidence(
                transaction_id=txn_id,
                transaction_split_id=accepted_split_id,
            ),
        )

    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
    assert check["expense_resolution_count"] == 1
    assert check["expense_resolution_transaction_count"] == 1
    assert check["expense_resolution_excluded_cents"] == 2000
    assert check["has_blocking_expense_resolutions"] is True


def test_reopen_rejects_a_month_that_is_not_closed(app_env):
    r = _client().post(
        "/close/reopen",
        data=_reopen_data(),
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_out_of_range_month_is_handled_cleanly(app_env):
    # An impossible month component must not 500 (repo_goals raises ValueError deep down).
    client = _client()
    assert client.get("/close?month=2026-00").status_code == 200  # falls back to a valid month
    assert client.post("/close/signoff", data={"month": "2026-13"},
                       follow_redirects=False).status_code == 400
    assert client.post("/close/reopen", data={"month": "2026-00"},
                       follow_redirects=False).status_code == 400


def test_signoff_rejects_already_closed_month(app_env):
    client = _client()
    _waive_required_matrix(app_env)
    client.post(
        "/close/signoff",
        data=_signoff_data(),
        follow_redirects=False,
    )
    r = client.post(
        "/close/signoff",
        data=_signoff_data(reason="duplicate close"),
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_close_form_requires_actor_reason_and_confirmation(app_env):
    _waive_required_matrix(app_env)
    client = _client()

    missing_actor = _signoff_data()
    missing_actor["actor"] = ""
    assert client.post(
        "/close/signoff",
        data=missing_actor,
        follow_redirects=False,
    ).status_code == 400

    missing_reason = _signoff_data()
    missing_reason["reason"] = ""
    assert client.post(
        "/close/signoff",
        data=missing_reason,
        follow_redirects=False,
    ).status_code == 400

    missing_confirmation = _signoff_data()
    missing_confirmation.pop("confirm_close")
    assert client.post(
        "/close/signoff",
        data=missing_confirmation,
        follow_redirects=False,
    ).status_code == 400

    with engine.read_conn(app_env) as conn:
        assert repo_period_policy.current_state(conn, FIXTURE_MONTH) == "open"


def test_exception_close_requires_durable_per_item_acknowledgement(app_env):
    _add_uncategorized_expense(app_env)
    _waive_required_matrix(app_env)
    client = _client()

    with engine.read_conn(app_env) as conn:
        prospective = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            FIXTURE_MONTH,
            period_exceptions.collect_period_exceptions(
                conn,
                FIXTURE_MONTH,
            ),
        )
    assert len(prospective) == 1
    token = str(prospective[0]["exception_token"])
    assert prospective[0]["is_acknowledged"] is False

    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert 'data-close-state="open"' in page.text
    assert 'data-policy-blocked="true"' in page.text
    assert "exception close · 0 of 1 acknowledged" in page.text.lower()
    assert f"/close/preclose-exception/{token}/acknowledge" in page.text
    assert "Durable acknowledgement is required before exception close" in page.text

    refused = client.post(
        "/close/signoff",
        data=_signoff_data(reason="freeze one unresolved category"),
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert (
        refused.json()["detail"]
        == "acknowledge every active exception before exception close"
    )
    with engine.read_conn(app_env) as conn:
        assert repo_period_policy.current_state(conn, FIXTURE_MONTH) == "open"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM period_close_cycles WHERE month=?",
                (FIXTURE_MONTH,),
            ).fetchone()[0]
            == 0
        )

    missing_evidence = client.post(
        f"/close/preclose-exception/{token}/acknowledge",
        data={
            "month": FIXTURE_MONTH,
            "actor": "test:reviewer",
            "reason": "waiting for a merchant receipt",
            "confirm_acknowledgement": "1",
        },
        follow_redirects=False,
    )
    assert missing_evidence.status_code == 400

    acknowledged = client.post(
        f"/close/preclose-exception/{token}/acknowledge",
        data={
            "month": FIXTURE_MONTH,
            "actor": "test:reviewer",
            "reason": "waiting for a merchant receipt",
            "evidence_note": "reviewed statement row and receipt inbox",
            "confirm_acknowledgement": "1",
            "operation_key": "test:preclose-ack:category",
        },
        follow_redirects=False,
    )
    assert acknowledged.status_code == 303

    with engine.read_conn(app_env) as conn:
        reviewed = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            FIXTURE_MONTH,
            period_exceptions.collect_period_exceptions(
                conn,
                FIXTURE_MONTH,
            ),
        )
    assert len(reviewed) == 1
    assert reviewed[0]["is_acknowledged"] is True
    assert reviewed[0]["acknowledgement_actor"] == "test:reviewer"

    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert "exception close · 1 of 1 acknowledged" in page.text.lower()
    assert "acknowledged · unresolved" in page.text
    assert "live close evidence" in page.text
    assert 'data-policy-blocked="false"' in page.text
    assert (
        "/close/preclose-acknowledgement/"
        f"{int(reviewed[0]['acknowledgement_id'])}/withdraw"
    ) in page.text

    response = client.post(
        "/close/signoff",
        data=_signoff_data(
            reason="freeze reviewed unresolved category",
            operation_key="test:exception-close:reviewed",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303

    with engine.read_conn(app_env) as conn:
        state = repo_period_policy.get_state(conn, FIXTURE_MONTH)
        items = repo_period_policy.list_current_exceptions(conn, FIXTURE_MONTH)
    assert state["state"] == "closed_with_exceptions"
    assert len(items) == 1
    assert items[0]["is_acknowledged"] is True
    assert items[0]["acknowledgement_actor"] == "test:reviewer"
    exception_id = int(items[0]["id"])
    acknowledgement_id = int(items[0]["acknowledgement_id"])
    snapshot_id = int(state["snapshot_id"])

    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert 'data-close-state="closed_with_exceptions"' in page.text
    assert f'data-snapshot-id="{snapshot_id}"' in page.text
    assert 'data-snapshot-current="true"' in page.text
    assert f'data-exception-id="{exception_id}"' in page.text
    assert 'data-exception-class="unconfirmed_merchant_category"' in page.text
    assert "exception close · 1 of 1 acknowledged" in page.text.lower()
    assert f"snapshot #{snapshot_id}" in page.text
    assert (
        f"/close/exception/{exception_id}/acknowledgement/"
        f"{acknowledgement_id}/withdraw"
    ) in page.text

    withdrawn = client.post(
        (
            f"/close/exception/{exception_id}/acknowledgement/"
            f"{acknowledgement_id}/withdraw"
        ),
        data={
            "month": FIXTURE_MONTH,
            "actor": "test:reviewer",
            "reason": "the review note was premature",
            "evidence_note": "new receipt evidence conflicts with the first note",
            "confirm_withdrawal": "1",
            "operation_key": f"test:ack-withdrawal:{acknowledgement_id}",
        },
        follow_redirects=False,
    )
    assert withdrawn.status_code == 303

    with engine.read_conn(app_env) as conn:
        final_state = repo_period_policy.get_state(conn, FIXTURE_MONTH)
        final_items = repo_period_policy.list_current_exceptions(
            conn,
            FIXTURE_MONTH,
        )
        acknowledgement_events = conn.execute(
            """SELECT event_kind, reverses_acknowledgement_id
               FROM period_close_acknowledgements
               WHERE exception_id=?
               ORDER BY id""",
            (exception_id,),
        ).fetchall()
    assert final_state["state"] == "closed_with_exceptions"
    assert int(final_state["snapshot_id"]) == snapshot_id
    assert len(final_items) == 1
    assert final_items[0]["is_acknowledged"] is False
    assert [row["event_kind"] for row in acknowledgement_events] == [
        "acknowledged",
        "withdrawn",
    ]
    assert (
        int(acknowledgement_events[1]["reverses_acknowledgement_id"])
        == acknowledgement_id
    )

    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert "exception close · 0 of 1 acknowledged" in page.text.lower()
    assert "acknowledged · unresolved" not in page.text


def test_preclose_acknowledgement_withdrawal_is_append_only(app_env):
    _add_uncategorized_expense(app_env)
    _waive_required_matrix(app_env)
    client = _client()
    with engine.read_conn(app_env) as conn:
        item = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            FIXTURE_MONTH,
            period_exceptions.collect_period_exceptions(
                conn,
                FIXTURE_MONTH,
            ),
        )[0]
    token = str(item["exception_token"])
    assert client.post(
        f"/close/preclose-exception/{token}/acknowledge",
        data={
            "month": FIXTURE_MONTH,
            "actor": "test:reviewer",
            "reason": "reviewed for a possible exception close",
            "evidence_note": "generated statement and receipt evidence",
            "confirm_acknowledgement": "1",
            "operation_key": "test:preclose-ack:withdrawal-case",
        },
        follow_redirects=False,
    ).status_code == 303

    with engine.read_conn(app_env) as conn:
        reviewed = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            FIXTURE_MONTH,
            period_exceptions.collect_period_exceptions(
                conn,
                FIXTURE_MONTH,
            ),
        )[0]
    acknowledgement_id = int(reviewed["acknowledgement_id"])

    missing_evidence = client.post(
        f"/close/preclose-acknowledgement/{acknowledgement_id}/withdraw",
        data={
            "month": FIXTURE_MONTH,
            "actor": "test:reviewer",
            "reason": "review evidence changed",
            "confirm_withdrawal": "1",
        },
        follow_redirects=False,
    )
    assert missing_evidence.status_code == 400

    withdrawn = client.post(
        f"/close/preclose-acknowledgement/{acknowledgement_id}/withdraw",
        data={
            "month": FIXTURE_MONTH,
            "actor": "test:reviewer",
            "reason": "review evidence changed",
            "evidence_note": "new generated evidence invalidates the prior review",
            "confirm_withdrawal": "1",
            "operation_key": "test:preclose-ack:withdraw",
        },
        follow_redirects=False,
    )
    assert withdrawn.status_code == 303

    with engine.read_conn(app_env) as conn:
        current = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            FIXTURE_MONTH,
            period_exceptions.collect_period_exceptions(
                conn,
                FIXTURE_MONTH,
            ),
        )
        events = conn.execute(
            """SELECT event_kind, reverses_acknowledgement_id
               FROM period_close_preacknowledgements
               WHERE month=? AND exception_token=?
               ORDER BY id""",
            (FIXTURE_MONTH, token),
        ).fetchall()
        state = repo_period_policy.current_state(conn, FIXTURE_MONTH)
    assert state == "open"
    assert current[0]["is_acknowledged"] is False
    assert [row["event_kind"] for row in events] == [
        "acknowledged",
        "withdrawn",
    ]
    assert int(events[1]["reverses_acknowledgement_id"]) == acknowledgement_id
    assert client.post(
        "/close/signoff",
        data=_signoff_data(reason="cannot close after review withdrawal"),
        follow_redirects=False,
    ).status_code == 400


def test_reopen_makes_snapshot_historical_and_reclose_creates_a_new_one(app_env):
    _add_categorized_expense(app_env)
    _waive_required_matrix(app_env)
    client = _client()
    assert client.post(
        "/close/signoff",
        data=_signoff_data(reason="first clean close"),
        follow_redirects=False,
    ).status_code == 303

    with engine.read_conn(app_env) as conn:
        first = repo_period_policy.get_state(conn, FIXTURE_MONTH)
    first_snapshot_id = int(first["snapshot_id"])

    assert client.post(
        "/close/reopen",
        data=_reopen_data(reason="correct one reviewed item"),
        follow_redirects=False,
    ).status_code == 303

    reopened = client.get(f"/close?month={FIXTURE_MONTH}")
    assert 'data-close-state="reopened"' in reopened.text
    assert 'data-snapshot-current="false"' in reopened.text
    assert f'data-snapshot-id="{first_snapshot_id}"' in reopened.text
    assert "historical snapshot" in reopened.text

    assert client.post(
        "/close/signoff",
        data=_signoff_data(reason="second clean close"),
        follow_redirects=False,
    ).status_code == 303

    with engine.read_conn(app_env) as conn:
        state = repo_period_policy.get_state(conn, FIXTURE_MONTH)
        history = repo_period_policy.list_snapshot_history(conn, FIXTURE_MONTH)
    assert state["state"] == "clean_closed"
    assert int(state["snapshot_id"]) != first_snapshot_id
    assert len(history) == 2
    assert [int(row["is_current"]) for row in history] == [1, 0]
