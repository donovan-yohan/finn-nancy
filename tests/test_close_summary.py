"""FN-104/FN-147 immutable month-close report snapshots.

Signing off freezes completeness figures in ``period_close_snapshots``.
``closed_periods`` is only a compatibility projection. Reopening makes the old
snapshot historical; re-closing appends a fresh, independently numbered one.
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.accounting import flows
from app.close import checklist, summary
from app.db import (
    engine,
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
        "reason": "reviewed generated close report",
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


def _add_uncategorized_expense(db: str, *, month: str = FIXTURE_MONTH, tag: str = "a") -> int:
    with engine.write_tx(db) as conn:
        cat_id = repo_ledger.ensure_uncategorized(conn)
        account_id = repo_ledger.ensure_default_account(conn)
        txn_id = repo_ledger.insert_transaction(
            conn, account_id=account_id, posted_on=f"{month}-09",
            description="MYSTERY CHARGE", counterparty="", amount_cents=-4200,
            source="manual", external_id=f"uncat-{month}-{tag}", source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=cat_id, amount_cents=-4200)
        return int(txn_id)


def _add_categorized_expense(
    db: str,
    *,
    month: str = FIXTURE_MONTH,
    tag: str = "g",
) -> int:
    with engine.write_tx(db) as conn:
        cat = repo_ledger.find_category_by_name(conn, "Groceries")
        account_id = repo_ledger.ensure_default_account(conn)
        txn_id = int(
            repo_ledger.insert_transaction(
                conn, account_id=account_id, posted_on=f"{month}-12",
                description="GROCER", counterparty="", amount_cents=-3300,
                source="manual", external_id=f"cat-{month}-{tag}", source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
        )
        split_id = repo_ledger.insert_split(
            conn,
            transaction_id=txn_id,
            category_id=int(cat["id"]),
            amount_cents=-3300,
        )
        repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="GROCER",
            category_id=int(cat["id"]),
            scope=repo_merchant_knowledge.scope_for_transaction(conn, txn_id),
            operation_key=f"test:close-summary-category:{txn_id}",
            actor="test:operator",
            reason="operator confirmed close-summary category evidence",
            evidence=Evidence(
                transaction_id=txn_id,
                transaction_split_id=split_id,
            ),
        )
        return txn_id


def _waive_required_matrix(db: str) -> None:
    with engine.write_tx(db) as conn:
        rows = repo_statement_expectations.prepare_period(
            conn,
            month=FIXTURE_MONTH,
            actor="test:close",
            reason="materialize summary fixture",
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
                    reason="summary fixture explicitly has no statement",
                )


# --- compose_summary unit coverage -------------------------------------------

def test_compose_summary_snapshots_month_figures(app_env):
    _add_uncategorized_expense(app_env)
    _add_categorized_expense(app_env)
    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
        snap = summary.compose_summary(
            conn, FIXTURE_MONTH, check=check,
            inbox_count=3, inbox_ack=True, variance_ack=False, anomaly_count=2,
        )

    # Ledger-derived receipt fields.
    assert snap["transactions_reviewed"] == 2
    assert snap["categories_confirmed"] == 1
    assert snap["uncategorized_count"] == 1
    assert snap["expense_resolution_unresolved_count"] == 1
    assert snap["money_out_cents"] == 7500
    assert snap["resolved_expense_cents"] == 3300
    assert snap["excluded_expense_cents"] == 4200
    assert snap["net_delta_cents"] == check["net_delta_cents"]
    assert snap["coverage_pct"] == check["coverage_pct"]
    assert snap["flow_review_count"] == 0
    # Sign-off inputs are frozen verbatim.
    assert snap["inbox_count"] == 3
    assert snap["inbox_ack"] is True
    assert snap["anomalies_flagged"] == 2
    assert snap["anomalies_acknowledged"] is True
    assert snap["variance_ack"] is False
    # Preserves the FN-102 sign-off keys.
    assert "percent_complete" in snap
    assert "overspend_cents" in snap


def test_resolution_totals_do_not_rewrite_money_out(app_env):
    purchase_id = _add_categorized_expense(app_env, tag="refund-target")
    with engine.write_tx(app_env) as conn:
        purchase = conn.execute(
            """
            SELECT account_id
            FROM transactions
            WHERE id=?
            """,
            (purchase_id,),
        ).fetchone()
        category = repo_ledger.find_category_by_name(conn, "Groceries")
        refund_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=int(purchase["account_id"]),
                posted_on=f"{FIXTURE_MONTH}-20",
                description="GROCER REFUND",
                counterparty="",
                amount_cents=1000,
                source="manual",
                external_id="close-summary-refund",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="refund",
            )
        )
        repo_ledger.insert_split(
            conn,
            transaction_id=refund_id,
            category_id=int(category["id"]),
            amount_cents=1000,
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            source_transaction_id=refund_id,
            target_transaction_id=purchase_id,
            actor="test:operator",
            reason="operator confirmed the purchase refund",
        )

    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
        snap = summary.compose_summary(
            conn,
            FIXTURE_MONTH,
            check=check,
            inbox_count=0,
            inbox_ack=False,
            variance_ack=False,
            anomaly_count=0,
        )

    # FN-149 resolution totals describe purchase/fee category confidence only.
    # The established cashflow report still nets the accepted refund.
    assert snap["resolved_expense_cents"] == 3300
    assert snap["excluded_expense_cents"] == 0
    assert snap["money_out_cents"] == 2300


def test_compose_summary_reports_goals_funded(app_env):
    with engine.write_tx(app_env) as conn:
        # A goal with an applied funding row this month must surface in the receipt.
        conn.execute(
            "INSERT INTO goals(name, kind, target_cents, monthly_contribution_cents, start_month, status) "
            "VALUES ('Vacation', 'savings_target', 500000, 20000, ?, 'active')",
            (FIXTURE_MONTH,),
        )
        goal_id = int(conn.execute("SELECT id FROM goals WHERE name='Vacation'").fetchone()["id"])
        conn.execute(
            "INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, status) "
            "VALUES (?,?,?,?, 'applied')",
            (goal_id, FIXTURE_MONTH, 20000, 20000),
        )
    with engine.read_conn(app_env) as conn:
        check = checklist.build_checklist(conn, FIXTURE_MONTH)
        snap = summary.compose_summary(
            conn, FIXTURE_MONTH, check=check,
            inbox_count=0, inbox_ack=False, variance_ack=False, anomaly_count=0,
        )
    assert snap["goals_funded_count"] == 1
    assert snap["goals_funded_cents"] == 20000


# --- sign-off persistence ----------------------------------------------------

def test_signoff_writes_summary_snapshot_to_period(app_env):
    _waive_required_matrix(app_env)
    _add_categorized_expense(app_env, tag="1")
    _add_categorized_expense(app_env, tag="2")
    r = _client().post(
        "/close/signoff",
        data=_signoff_data(reason="receipt please"),
        follow_redirects=False,
    )
    assert r.status_code == 303
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            "SELECT snapshot_json FROM v_current_period_close_snapshot WHERE month=?",
            (FIXTURE_MONTH,),
        ).fetchone()
    snap = json.loads(row["snapshot_json"])
    assert snap["transactions_reviewed"] == 2
    assert snap["categories_confirmed"] == 2
    assert snap["uncategorized_count"] == 0
    assert snap["expense_resolution_unresolved_count"] == 0
    # The receipt fields all land in the persisted snapshot.
    for key in (
        "coverage_pct", "net_delta_cents", "anomalies_flagged",
        "goals_funded_count", "goals_funded_cents", "inbox_count",
        "flow_review_count", "money_out_cents", "resolved_expense_cents",
        "excluded_expense_cents",
    ):
        assert key in snap


# --- read-only render --------------------------------------------------------

def test_closed_month_renders_receipt(app_env):
    _waive_required_matrix(app_env)
    _add_categorized_expense(app_env)
    client = _client()
    client.post(
        "/close/signoff",
        data=_signoff_data(),
        follow_redirects=False,
    )
    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert page.status_code == 200
    assert "here's your month" in page.text
    assert "transactions reviewed" in page.text
    assert "categories confirmed" in page.text
    assert "money out" in page.text
    assert "confirmed category total" in page.text
    assert "not yet confirmed" in page.text
    assert "Goals funded" in page.text
    # Reopen control still present; sign-off form gone.
    assert 'action="/close/reopen"' in page.text
    assert 'action="/close/signoff"' not in page.text


def test_summary_reflects_close_time_not_live_data(app_env):
    _waive_required_matrix(app_env)
    transaction_id = _add_categorized_expense(app_env, tag="1")
    client = _client()
    client.post(
        "/close/signoff",
        data=_signoff_data(),
        follow_redirects=False,
    )
    with engine.read_conn(app_env) as conn:
        first_snapshot = repo_period_policy.list_snapshot_history(
            conn,
            FIXTURE_MONTH,
        )[0]
        snap_at_close = json.loads(first_snapshot["snapshot_json"])
    assert snap_at_close["expense_resolution_unresolved_count"] == 0
    assert snap_at_close["transactions_reviewed"] == 1

    # Reopen before changing the split. The old snapshot remains immutable and
    # historical while the live projection becomes unresolved.
    assert client.post(
        "/close/reopen",
        data=_reopen_data(reason="test immutable historical report"),
        follow_redirects=False,
    ).status_code == 303
    with engine.write_tx(app_env) as conn:
        restaurants = repo_ledger.find_category_by_name(conn, "Restaurants")
        conn.execute(
            """
            UPDATE transaction_splits
            SET category_id=?
            WHERE transaction_id=?
            """,
            (int(restaurants["id"]), transaction_id),
        )

    # The reopened page marks the old snapshot historical rather than presenting
    # it as a current report.
    page = client.get(f"/close?month={FIXTURE_MONTH}")
    assert 'data-close-state="reopened"' in page.text
    assert 'data-snapshot-current="false"' in page.text
    assert "historical snapshot" in page.text
    with engine.read_conn(app_env) as conn:
        live = checklist.build_checklist(conn, FIXTURE_MONTH)
        unchanged = json.loads(
            repo_period_policy.list_snapshot_history(conn, FIXTURE_MONTH)[0][
                "snapshot_json"
            ]
        )
    assert live["expense_resolution_count"] == 1
    assert unchanged == snap_at_close


def test_compatibility_close_preserves_variance_note_without_granting_authority(app_env):
    """The compatibility wrapper preserves old summary fields as evidence only."""
    legacy_summary = {
        "percent_complete": 100,
        "overspend_cents": 5000,
        "inbox_count": 0,
        "inbox_ack": False,
    }
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(
            conn, FIXTURE_MONTH, variance_ack=True, summary=legacy_summary,
        )

    page = _client().get(f"/close?month={FIXTURE_MONTH}")
    assert page.status_code == 200
    assert 'data-close-state="clean_closed"' in page.text
    with engine.read_conn(app_env) as conn:
        snapshot = json.loads(
            repo_period_policy.list_snapshot_history(conn, FIXTURE_MONTH)[0][
                "snapshot_json"
            ]
        )
    assert snapshot["overspend_cents"] == 5000
    assert snapshot["variance_ack"] is True


def test_reopen_reclose_produces_new_snapshot(app_env):
    _waive_required_matrix(app_env)
    _add_categorized_expense(app_env, tag="1")
    client = _client()
    client.post(
        "/close/signoff",
        data=_signoff_data(reason="first report"),
        follow_redirects=False,
    )
    with engine.read_conn(app_env) as conn:
        first_row = repo_period_policy.list_snapshot_history(
            conn,
            FIXTURE_MONTH,
        )[0]
        first = json.loads(first_row["snapshot_json"])
        first_id = int(first_row["snapshot_id"])
    assert first["transactions_reviewed"] == 1

    client.post(
        "/close/reopen",
        data=_reopen_data(reason="fix"),
        follow_redirects=False,
    )
    _add_categorized_expense(app_env, tag="2")
    client.post(
        "/close/signoff",
        data=_signoff_data(reason="second report"),
        follow_redirects=False,
    )

    with engine.read_conn(app_env) as conn:
        history = repo_period_policy.list_snapshot_history(conn, FIXTURE_MONTH)
        second = json.loads(history[0]["snapshot_json"])
        audit = repo_close.list_audit(conn, FIXTURE_MONTH)
    # Fresh snapshot reflects the added transaction.
    assert second["transactions_reviewed"] == 2
    assert second != first
    assert len(history) == 2
    assert int(history[0]["snapshot_id"]) != first_id
    assert [int(row["is_current"]) for row in history] == [1, 0]
    # The reopen is recorded exactly once (FN-100/103 own the audit; no double-log here).
    assert sum(1 for a in audit if a["new_value"] == "reopened") == 1
