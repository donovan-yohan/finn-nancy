from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.db import engine, repo_goals, repo_ledger


def _client():
    from app.web.app import create_app

    return TestClient(create_app())


@pytest.fixture
def route_empty_db(empty_db, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.config import get_settings

    get_settings.cache_clear()
    yield empty_db
    get_settings.cache_clear()


def _account(conn) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO accounts(name, institution, kind, currency)
            VALUES ('Goal Test Card', 'Test', 'credit', 'CAD')
            """
        ).lastrowid
    )


def _category(conn, name: str, *, color: str = "#FF9F43") -> int:
    return int(
        conn.execute(
            "INSERT INTO categories(name, kind, brand_owner, color) VALUES (?, 'expense', 'nancy', ?)",
            (name, color),
        ).lastrowid
    )


def _budget(conn, category_id: int, cents: int, period_month: str = "") -> None:
    conn.execute(
        """
        INSERT INTO budgets(category_id, period_month, amount_cents)
        VALUES (?, ?, ?)
        ON CONFLICT(category_id, period_month) DO UPDATE SET amount_cents=excluded.amount_cents
        """,
        (category_id, period_month, cents),
    )


def _expense(
    conn,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    amount_cents: int,
    external_id: str,
    merchant: str = "Goal Merchant",
) -> int:
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=f"{merchant} expense",
        counterparty=merchant,
        amount_cents=-abs(amount_cents),
        source="goal-test",
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
    return int(txn_id)


def _seed_month(
    db_path: str,
    month: str,
    specs: list[tuple[str, int, int]],
) -> dict[str, int]:
    """Seed categories with ``budget - spend`` underspend for a month."""
    with engine.write_tx(db_path) as conn:
        conn.execute(
            """
            INSERT INTO app_settings(key, value)
            VALUES ('big_ticket_threshold_cents', '30000')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """
        )
        account_id = _account(conn)
        category_ids: dict[str, int] = {}
        for idx, (name, budget_cents, spend_cents) in enumerate(specs, start=1):
            cat_id = _category(conn, f"{name} {month}")
            category_ids[name] = cat_id
            _budget(conn, cat_id, budget_cents)
            # Also pin the same amount to this month. A default budget
            # (period_month='') only resolves against months the ledger already
            # knows about, so a month seeded with no spending would otherwise
            # depend on which month the suite happens to run in. The month-
            # scoped row resolves to the same amount, and makes the month
            # visible to v_month_spine.
            _budget(conn, cat_id, budget_cents, month)
            if spend_cents:
                _expense(
                    conn,
                    account_id=account_id,
                    category_id=cat_id,
                    posted_on=f"{month}-10",
                    amount_cents=spend_cents,
                    external_id=f"{month}:{name}:{idx}",
                    merchant=name,
                )
    return category_ids


def _insert_goal(
    conn,
    *,
    name: str = "Goal",
    kind: str = "payoff",
    target_cents: int = 100_000,
    start_month: str = "2026-07",
    target_month: str | None = "2026-12",
    monthly_contribution_cents: int = 10_000,
    auto_fund: int = 1,
    priority: int = 100,
    linked_transaction_id: int | None = None,
    linked_category_id: int | None = None,
    status: str = "active",
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO goals(
              name, kind, target_cents, start_month, target_month,
              monthly_contribution_cents, linked_transaction_id, linked_category_id,
              auto_fund, priority, status, brand_owner, color
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'nancy', '#FF9F43')
            """,
            (
                name,
                kind,
                target_cents,
                start_month,
                target_month,
                monthly_contribution_cents,
                linked_transaction_id,
                linked_category_id,
                auto_fund,
                priority,
                status,
            ),
        ).lastrowid
    )


def _ledger_rows(conn, goal_id: int) -> list[tuple[str, int, int]]:
    return [
        (row["month"], row["planned_cents"], row["actual_cents"])
        for row in conn.execute(
            """
            SELECT month, planned_cents, actual_cents
            FROM goal_ledger
            WHERE goal_id=?
            ORDER BY month
            """,
            (goal_id,),
        ).fetchall()
    ]


def _ledger_detail(conn, goal_id: int, month: str):
    return conn.execute(
        """
        SELECT month, planned_cents, actual_cents, source, status, note
        FROM goal_ledger
        WHERE goal_id=? AND month=?
        """,
        (goal_id, month),
    ).fetchone()


def test_goal_schema_constraints(empty_db):
    with engine.read_conn(empty_db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert {"goals", "goal_ledger"} <= tables
    assert "v_goal_progress" in views

    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(conn, name="Unique Goal")
        conn.execute(
            """
            INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents)
            VALUES (?, '2026-07', 1000, 1000)
            """,
            (goal_id,),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(empty_db) as conn:
            conn.execute(
                """
                INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents)
                VALUES (?, '2026-07', 1000, 1000)
                """,
                (goal_id,),
            )

    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(empty_db) as conn:
            _insert_goal(conn, name="Bad Kind", kind="wish")

    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(empty_db) as conn:
            _insert_goal(conn, name="Bad Target", target_cents=0)


def test_close_month_respects_start_month_window_and_review_rows(empty_db):
    _seed_month(empty_db, "2026-07", [("Window Pool", 8_000, 0)])
    with engine.write_tx(empty_db) as conn:
        future = _insert_goal(
            conn,
            name="September goal",
            target_cents=50_000,
            start_month="2026-09",
            monthly_contribution_cents=5_000,
            priority=1,
        )
        current = _insert_goal(
            conn,
            name="July goal",
            target_cents=50_000,
            start_month="2026-07",
            monthly_contribution_cents=5_000,
            priority=2,
        )
        repo_goals.close_month(conn, "2026-07")

    with engine.read_conn(empty_db) as conn:
        assert _ledger_detail(conn, future, "2026-07") is None
        current_row = _ledger_detail(conn, current, "2026-07")
        review_names = {row["name"] for row in repo_goals.close_review_rows(conn, "2026-07")}

    assert current_row["planned_cents"] == 5_000
    assert current_row["actual_cents"] == 5_000
    assert "September goal" not in review_names
    assert "July goal" in review_names


def test_close_month_is_deterministic_and_idempotent(empty_db):
    _seed_month(empty_db, "2026-07", [("Groceries", 10_000, 2_000), ("Dining", 5_000, 1_000)])
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="July payoff",
            target_cents=20_000,
            monthly_contribution_cents=7_000,
            priority=10,
        )
        repo_goals.close_month(conn, "2026-07")
    with engine.read_conn(empty_db) as conn:
        before = conn.execute(
            """
            SELECT planned_cents, actual_cents, note
            FROM goal_ledger
            WHERE goal_id=? AND month='2026-07'
            """,
            (goal_id,),
        ).fetchone()
        assert before["planned_cents"] == 7_000
        assert before["actual_cents"] == 7_000

    with engine.write_tx(empty_db) as conn:
        repo_goals.close_month(conn, "2026-07")
    with engine.read_conn(empty_db) as conn:
        rows = conn.execute(
            "SELECT planned_cents, actual_cents, note FROM goal_ledger WHERE goal_id=? AND month='2026-07'",
            (goal_id,),
        ).fetchall()

    assert len(rows) == 1
    assert tuple(rows[0]) == tuple(before)


def test_reclose_keeps_applied_planned_and_note_immutable_after_plan_change(empty_db):
    _seed_month(empty_db, "2026-07", [("History Pool", 120_000, 0)])
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Frozen July",
            target_cents=600_000,
            start_month="2026-07",
            target_month="2026-12",
            monthly_contribution_cents=100_000,
        )
        repo_goals.close_month(conn, "2026-07")
        before = _ledger_detail(conn, goal_id, "2026-07")
        catchup = repo_goals.catch_up(conn, goal_id, "2026-09")
        assert catchup["required_monthly_cents"] == 125_000
        repo_goals.close_month(conn, "2026-07")
        after = _ledger_detail(conn, goal_id, "2026-07")

    assert tuple(after) == tuple(before)


def test_corrective_reclose_recomputes_actual_but_freezes_planned(empty_db):
    categories = _seed_month(empty_db, "2026-07", [("Corrective Pool", 100_000, 0)])
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Corrected July",
            target_cents=500_000,
            monthly_contribution_cents=100_000,
        )
        repo_goals.close_month(conn, "2026-07")
        account_id = _account(conn)
        _expense(
            conn,
            account_id=account_id,
            category_id=categories["Corrective Pool"],
            posted_on="2026-07-25",
            amount_cents=60_000,
            external_id="corrective-late-expense",
            merchant="Late Expense",
        )
        repo_goals.close_month(conn, "2026-07")
        row = _ledger_detail(conn, goal_id, "2026-07")

    note = json.loads(row["note"])
    assert row["planned_cents"] == 100_000
    assert row["actual_cents"] == 40_000
    assert note["planned_cents"] == 100_000
    assert note["pool_before_cents"] == 40_000
    assert note["alloc_cents"] == 40_000
    assert sum(source["cents"] for source in note["sources"]) == 40_000


def test_out_of_order_close_caps_total_applied_at_target(empty_db):
    _seed_month(empty_db, "2026-07", [("July Cap Pool", 120_000, 20_000)])
    _seed_month(empty_db, "2026-08", [("August Cap Pool", 120_000, 20_000)])
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Out of order",
            target_cents=100_000,
            monthly_contribution_cents=60_000,
        )
        repo_goals.close_month(conn, "2026-08")
        repo_goals.close_month(conn, "2026-07")

    with engine.read_conn(empty_db) as conn:
        rows = _ledger_rows(conn, goal_id)
        total = conn.execute(
            "SELECT SUM(actual_cents) AS actual FROM goal_ledger WHERE goal_id=? AND status='applied'",
            (goal_id,),
        ).fetchone()
        goal = conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()

    assert rows == [("2026-07", 40_000, 40_000), ("2026-08", 60_000, 60_000)]
    assert total["actual"] == 100_000
    assert goal["status"] == "completed"


def test_corrective_reclose_can_revert_and_later_recomplete_goal(empty_db):
    categories = _seed_month(empty_db, "2026-07", [("Revert Pool", 8_001, 1)])
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Revert completion",
            target_cents=8_000,
            monthly_contribution_cents=8_000,
        )
        repo_goals.close_month(conn, "2026-07")
        assert conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()["status"] == "completed"
        account_id = _account(conn)
        _expense(
            conn,
            account_id=account_id,
            category_id=categories["Revert Pool"],
            posted_on="2026-07-26",
            amount_cents=5_000,
            external_id="revert-late-expense",
            merchant="Late Expense",
        )
        august_spine = _category(conn, "August Spine")
        _expense(
            conn,
            account_id=account_id,
            category_id=august_spine,
            posted_on="2026-08-02",
            amount_cents=1,
            external_id="revert-august-spine",
            merchant="August Spine",
        )
        repo_goals.close_month(conn, "2026-07")
        july = _ledger_detail(conn, goal_id, "2026-07")
        reverted = conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        repo_goals.close_month(conn, "2026-08")
        august = _ledger_detail(conn, goal_id, "2026-08")
        completed = conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()

    assert july["actual_cents"] == 3_000
    assert reverted["status"] == "active"
    assert august["actual_cents"] == 5_000
    assert completed["status"] == "completed"


def test_manual_applied_row_is_not_rewritten_and_counts_for_future_remaining(empty_db):
    _seed_month(empty_db, "2026-07", [("Manual Pool", 10_000, 0)])
    _seed_month(empty_db, "2026-08", [("Manual August Spine", 3_001, 1)])
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Manual July",
            target_cents=10_000,
            monthly_contribution_cents=10_000,
        )
        conn.execute(
            """
            INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status, note)
            VALUES (?, '2026-07', 7000, 7000, 'manual', 'applied', 'manual history')
            """,
            (goal_id,),
        )
        before = _ledger_detail(conn, goal_id, "2026-07")
        repo_goals.close_month(conn, "2026-07")
        after = _ledger_detail(conn, goal_id, "2026-07")
        repo_goals.close_month(conn, "2026-08")
        august = _ledger_detail(conn, goal_id, "2026-08")

    assert tuple(after) == tuple(before)
    assert august["planned_cents"] == 3_000
    assert august["actual_cents"] == 3_000


def test_close_month_pool_exhaustion_and_priority(empty_db):
    _seed_month(empty_db, "2026-07", [("Shared Pool", 12_000, 2_000)])
    with engine.write_tx(empty_db) as conn:
        first = _insert_goal(conn, name="First", target_cents=50_000, monthly_contribution_cents=8_000, priority=1)
        second = _insert_goal(conn, name="Second", target_cents=50_000, monthly_contribution_cents=8_000, priority=2)
        repo_goals.close_month(conn, "2026-07")

    with engine.read_conn(empty_db) as conn:
        rows = {
            row["goal_id"]: row
            for row in conn.execute(
                "SELECT goal_id, planned_cents, actual_cents FROM goal_ledger ORDER BY goal_id"
            ).fetchall()
        }
    assert rows[first]["planned_cents"] == 8_000
    assert rows[first]["actual_cents"] == 8_000
    assert rows[second]["planned_cents"] == 8_000
    assert rows[second]["actual_cents"] == 2_000


def test_overspent_categories_do_not_contribute_to_goal_pool(empty_db):
    _seed_month(empty_db, "2026-07", [("Overspent Pool", 10_000, 12_000)])
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="No overspend draw",
            target_cents=10_000,
            monthly_contribution_cents=10_000,
        )
        repo_goals.close_month(conn, "2026-07")
        row = _ledger_detail(conn, goal_id, "2026-07")

    note = json.loads(row["note"])
    assert row["planned_cents"] == 10_000
    assert row["actual_cents"] == 0
    assert note["pool_before_cents"] == 0
    assert note["sources"] == []


def test_three_goals_compete_by_priority_and_allocations_sum_to_pool(empty_db):
    _seed_month(empty_db, "2026-07", [("Small Pool", 10_000, 0)])
    with engine.write_tx(empty_db) as conn:
        first = _insert_goal(conn, name="First small", monthly_contribution_cents=6_000, priority=1)
        second = _insert_goal(conn, name="Second small", monthly_contribution_cents=6_000, priority=2)
        third = _insert_goal(conn, name="Third small", monthly_contribution_cents=6_000, priority=3)
        repo_goals.close_month(conn, "2026-07")

    with engine.read_conn(empty_db) as conn:
        rows = {
            row["goal_id"]: row["actual_cents"]
            for row in conn.execute(
                """
                SELECT goal_id, actual_cents
                FROM goal_ledger
                WHERE month='2026-07'
                ORDER BY goal_id
                """
            ).fetchall()
        }

    assert rows == {first: 6_000, second: 4_000, third: 0}
    assert sum(rows.values()) == 10_000


def test_zero_pool_close_still_records_planned_rows_and_close_page_renders(route_empty_db):
    today = date.today().strftime("%Y-%m")
    with engine.write_tx(route_empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Zero pool",
            start_month=today,
            target_month=repo_goals.add_months(today, 5),
            monthly_contribution_cents=5_000,
        )
        repo_goals.close_month(conn, today)
        row = _ledger_detail(conn, goal_id, today)
    client = _client()
    page = client.get(f"/goals/close?month={today}")

    assert row["planned_cents"] == 5_000
    assert row["actual_cents"] == 0
    assert page.status_code == 200
    assert "Zero pool" in page.text


def test_goal_progress_view_defaults_for_goal_with_zero_ledger_rows(empty_db):
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(conn, name="No ledger")
    with engine.read_conn(empty_db) as conn:
        progress = conn.execute("SELECT * FROM v_goal_progress WHERE goal_id=?", (goal_id,)).fetchone()

    assert progress["contributed_cents"] == 0
    assert progress["pct_complete"] == 0.0
    assert progress["months_funded"] == 0
    assert progress["last_funded_month"] is None


def test_close_month_caps_remaining_and_auto_completes_idempotently(empty_db):
    _seed_month(empty_db, "2026-07", [("Completion Pool", 12_000, 2_000)])
    with engine.write_tx(empty_db) as conn:
        finishing = _insert_goal(
            conn,
            name="Almost done",
            target_cents=10_000,
            monthly_contribution_cents=5_000,
            priority=1,
        )
        later = _insert_goal(
            conn,
            name="Later goal",
            target_cents=50_000,
            monthly_contribution_cents=5_000,
            priority=2,
        )
        conn.execute(
            """
            INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status)
            VALUES (?, '2026-06', 8000, 8000, 'manual', 'applied')
            """,
            (finishing,),
        )
        repo_goals.close_month(conn, "2026-07")
    with engine.read_conn(empty_db) as conn:
        final_row = conn.execute(
            "SELECT actual_cents FROM goal_ledger WHERE goal_id=? AND month='2026-07'",
            (finishing,),
        ).fetchone()
        status = conn.execute("SELECT status FROM goals WHERE id=?", (finishing,)).fetchone()
        later_before = conn.execute(
            "SELECT planned_cents, actual_cents, note FROM goal_ledger WHERE goal_id=? AND month='2026-07'",
            (later,),
        ).fetchone()
    assert final_row["actual_cents"] == 2_000
    assert status["status"] == "completed"
    assert later_before["actual_cents"] == 5_000

    with engine.write_tx(empty_db) as conn:
        repo_goals.close_month(conn, "2026-07")
    with engine.read_conn(empty_db) as conn:
        total = conn.execute(
            "SELECT SUM(actual_cents) AS actual FROM goal_ledger WHERE goal_id=?",
            (finishing,),
        ).fetchone()
        later_after = conn.execute(
            "SELECT planned_cents, actual_cents, note FROM goal_ledger WHERE goal_id=? AND month='2026-07'",
            (later,),
        ).fetchone()
    assert total["actual"] == 10_000
    assert tuple(later_after) == tuple(later_before)


def test_close_month_note_prefers_linked_category_and_balances_sources(empty_db):
    categories = _seed_month(
        empty_db,
        "2026-07",
        [("Largest", 10_000, 2_000), ("Preferred", 6_000, 2_000)],
    )
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Linked source",
            target_cents=50_000,
            monthly_contribution_cents=9_000,
            linked_category_id=categories["Preferred"],
        )
        repo_goals.close_month(conn, "2026-07")
    with engine.read_conn(empty_db) as conn:
        row = conn.execute(
            "SELECT planned_cents, actual_cents, note FROM goal_ledger WHERE goal_id=?",
            (goal_id,),
        ).fetchone()

    note = json.loads(row["note"])
    assert note["pool_before_cents"] == 12_000
    assert note["planned_cents"] == 9_000
    assert note["alloc_cents"] == 9_000
    assert sum(source["cents"] for source in note["sources"]) == row["actual_cents"]
    assert note["sources"][0]["category_id"] == categories["Preferred"]
    assert note["sources"][0]["category_name"] == "Preferred 2026-07"
    assert note["sources"][0]["cents"] == 4_000
    assert note["sources"][1]["category_id"] == categories["Largest"]
    assert note["sources"][1]["cents"] == 5_000


def test_payoff_math_states_and_month_helpers():
    assert repo_goals.add_months("2026-01", 13) == "2027-02"
    assert repo_goals.add_months("2026-12", 1) == "2027-01"
    assert repo_goals.diff_months("2026-01", "2026-06") == 5
    assert repo_goals.diff_months("2026-12", "2027-02") == 2
    assert repo_goals.elapsed_months("2026-01", "2026-06") == 6
    assert repo_goals.elapsed_months("2026-12", "2027-02") == 3
    assert repo_goals.elapsed_months("2026-03", "2026-02") == 0
    assert repo_goals.planned_total_months("2026-01", "2026-06") == 6
    assert repo_goals.planned_total_months(
        "2026-01", None, target_cents=60_000, monthly_contribution_cents=10_000
    ) == 6

    base = {
        "target_cents": 60_000,
        "monthly_contribution_cents": 10_000,
        "start_month": "2026-01",
        "target_month": "2026-06",
        "as_of_month": "2026-03",
    }
    assert repo_goals.payoff_status(contributed_cents=30_000, **base)["state"] == "on_track"
    assert repo_goals.payoff_status(contributed_cents=20_000, **base)["state"] == "behind"
    assert repo_goals.payoff_status(contributed_cents=45_000, **base)["state"] == "ahead"
    completed = repo_goals.payoff_status(contributed_cents=60_000, **base)
    assert completed["state"] == "completed"
    assert completed["remaining_cents"] == 0


def test_payoff_as_of_unclosed_month_can_still_fund_and_extend_stays_in_horizon(empty_db):
    current_month = date.today().strftime("%Y-%m")
    with engine.write_tx(empty_db) as conn:
        category_id = _category(conn, "Laptop Pool")
        _budget(conn, category_id, 33_335)
        account_id = _account(conn)
        _expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-07-15",
            amount_cents=1,
            external_id="laptop-july-spine",
            merchant="July Spine",
        )
        _expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-08-15",
            amount_cents=1,
            external_id="laptop-august-spine",
            merchant="August Spine",
        )
        goal_id = _insert_goal(
            conn,
            name="Design laptop",
            target_cents=200_000,
            start_month="2026-07",
            target_month="2026-12",
            monthly_contribution_cents=33_334,
        )
        repo_goals.close_month(conn, "2026-07")
        repo_goals.close_month(conn, "2026-08")
        goal = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        preclose = repo_goals.payoff_status(
            target_cents=goal["target_cents"],
            contributed_cents=repo_goals._contributed_through(conn, goal_id, "2026-09"),
            monthly_contribution_cents=goal["monthly_contribution_cents"],
            start_month=goal["start_month"],
            target_month=goal["target_month"],
            as_of_month="2026-09",
            as_of_funded=False,
        )
        extend_status = repo_goals.extend(conn, goal_id, "2026-09")
        _expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-09-15",
            amount_cents=33_335,
            external_id="laptop-september-short",
            merchant="September Short",
        )
        repo_goals.close_month(conn, "2026-09")
        postclose = repo_goals.payoff_status(
            target_cents=goal["target_cents"],
            contributed_cents=repo_goals._contributed_through(conn, goal_id, "2026-09"),
            monthly_contribution_cents=goal["monthly_contribution_cents"],
            start_month=goal["start_month"],
            target_month=goal["target_month"],
            as_of_month="2026-09",
            as_of_funded=True,
        )

    created_this_month = repo_goals.payoff_status(
        target_cents=10_000,
        contributed_cents=0,
        monthly_contribution_cents=10_000,
        start_month=current_month,
        target_month=current_month,
        as_of_month=current_month,
        as_of_funded=False,
    )

    assert preclose["state"] == "on_track"
    assert preclose["remaining_cents"] == 133_332
    assert preclose["required_monthly_cents"] == 33_333
    assert preclose["projected_target_month"] == "2026-12"
    assert extend_status["target_month"] == "2026-12"
    assert postclose["state"] == "behind"
    assert created_this_month["state"] == "on_track"


def test_catch_up_guards_at_target_null_target_double_press_and_completed(empty_db):
    with engine.write_tx(empty_db) as conn:
        at_target = _insert_goal(
            conn,
            name="At target",
            target_cents=10_000,
            monthly_contribution_cents=5_000,
        )
        conn.execute(
            """
            INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status)
            VALUES (?, '2026-07', 5000, 10000, 'manual', 'applied')
            """,
            (at_target,),
        )
        at_target_status = repo_goals.catch_up(conn, at_target, "2026-07")
        at_target_monthly = conn.execute(
            "SELECT monthly_contribution_cents FROM goals WHERE id=?", (at_target,)
        ).fetchone()

        flexible = _insert_goal(
            conn,
            name="Flexible target",
            target_cents=120_000,
            start_month="2026-01",
            target_month=None,
            monthly_contribution_cents=10_000,
        )
        for month in ("2026-01", "2026-02", "2026-03"):
            conn.execute(
                """
                INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status)
                VALUES (?, ?, 10000, 10000, 'manual', 'applied')
                """,
                (flexible, month),
            )
        first = repo_goals.catch_up(conn, flexible, "2026-07")
        first_goal = conn.execute(
            "SELECT monthly_contribution_cents, target_month FROM goals WHERE id=?", (flexible,)
        ).fetchone()
        second = repo_goals.catch_up(conn, flexible, "2026-07")
        second_goal = conn.execute(
            "SELECT monthly_contribution_cents, target_month FROM goals WHERE id=?", (flexible,)
        ).fetchone()

        completed = _insert_goal(
            conn,
            name="Manual complete",
            target_cents=100_000,
            monthly_contribution_cents=10_000,
            status="completed",
        )
        completed_status = repo_goals.catch_up(conn, completed, "2026-07")
        completed_goal = conn.execute(
            "SELECT monthly_contribution_cents FROM goals WHERE id=?", (completed,)
        ).fetchone()

        spanning = _insert_goal(
            conn,
            name="Boundary target",
            target_cents=120_000,
            start_month="2026-11",
            target_month=None,
            monthly_contribution_cents=30_000,
        )
        conn.execute(
            """
            INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status)
            VALUES (?, '2026-11', 30000, 30000, 'manual', 'applied')
            """,
            (spanning,),
        )
        spanning_status = repo_goals.catch_up(conn, spanning, "2026-12")
        spanning_goal = conn.execute(
            "SELECT monthly_contribution_cents, target_month FROM goals WHERE id=?", (spanning,)
        ).fetchone()

    assert at_target_status["remaining_cents"] == 0
    assert at_target_monthly["monthly_contribution_cents"] == 5_000
    assert first["required_monthly_cents"] == 15_000
    assert second["required_monthly_cents"] == 15_000
    assert tuple(first_goal) == (15_000, "2026-12")
    assert tuple(second_goal) == (15_000, "2026-12")
    assert completed_status["remaining_cents"] == 100_000
    assert completed_goal["monthly_contribution_cents"] == 10_000
    assert spanning_status["required_monthly_cents"] == 30_000
    assert tuple(spanning_goal) == (30_000, "2027-02")


def test_catch_up_and_extend_do_not_rewrite_history(empty_db):
    with engine.write_tx(empty_db) as conn:
        catchup_goal = _insert_goal(
            conn,
            name="Catch up",
            target_cents=60_000,
            start_month="2026-01",
            target_month="2026-06",
            monthly_contribution_cents=10_000,
        )
        extend_goal = _insert_goal(
            conn,
            name="Extend",
            target_cents=60_000,
            start_month="2026-01",
            target_month="2026-06",
            monthly_contribution_cents=10_000,
        )
        for goal_id in (catchup_goal, extend_goal):
            conn.execute(
                "INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status) VALUES (?, '2026-01', 10000, 10000, 'manual', 'applied')",
                (goal_id,),
            )
            conn.execute(
                "INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status) VALUES (?, '2026-02', 10000, 5000, 'manual', 'applied')",
                (goal_id,),
            )
        before_catchup = _ledger_rows(conn, catchup_goal)
        before_extend = _ledger_rows(conn, extend_goal)
        catchup_status = repo_goals.catch_up(conn, catchup_goal, "2026-03")
        extend_status = repo_goals.extend(conn, extend_goal, "2026-03")
        catchup_row = conn.execute("SELECT monthly_contribution_cents FROM goals WHERE id=?", (catchup_goal,)).fetchone()
        extend_row = conn.execute("SELECT target_month FROM goals WHERE id=?", (extend_goal,)).fetchone()
        after_catchup = _ledger_rows(conn, catchup_goal)
        after_extend = _ledger_rows(conn, extend_goal)

    assert catchup_status["required_monthly_cents"] == 11_250
    assert catchup_row["monthly_contribution_cents"] == 11_250
    assert extend_status["projected_target_month"] == "2026-07"
    assert extend_row["target_month"] == "2026-07"
    assert after_catchup == before_catchup
    assert after_extend == before_extend


def test_goals_routes_create_close_idempotent_catch_up_and_extend(app_env):
    _seed_month(app_env, "2026-07", [("Route Pool", 12_000, 4_000)])
    client = _client()

    created = client.post(
        "/goals",
        data={
            "name": "Route Payoff",
            "kind": "payoff",
            "target": "200.00",
            "start_month": "2026-07",
            "target_month": "2026-08",
            "monthly_contribution": "50.00",
            "priority": "5",
            "brand_owner": "nancy",
            "color": "#FF9F43",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with engine.read_conn(app_env) as conn:
        goal = conn.execute("SELECT * FROM goals WHERE name='Route Payoff'").fetchone()
    assert goal is not None
    goal_id = int(goal["id"])
    assert goal["auto_fund"] == 1

    page = client.get("/goals?month=2026-07")
    assert page.status_code == 200
    assert "Route Payoff" in page.text
    assert "behind/ahead" in page.text

    first_close = client.post("/goals/close", data={"month": "2026-07"}, follow_redirects=False)
    assert first_close.status_code == 303
    with engine.read_conn(app_env) as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS n, SUM(actual_cents) AS actual FROM goal_ledger WHERE goal_id=?",
            (goal_id,),
        ).fetchone()
    second_close = client.post("/goals/close", data={"month": "2026-07"}, follow_redirects=False)
    assert second_close.status_code == 303
    with engine.read_conn(app_env) as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS n, SUM(actual_cents) AS actual FROM goal_ledger WHERE goal_id=?",
            (goal_id,),
        ).fetchone()
    assert tuple(after) == tuple(before) == (1, 5_000)

    catchup = client.post(
        f"/goals/{goal_id}/catch-up",
        data={"month": "2026-07"},
        follow_redirects=False,
    )
    assert catchup.status_code == 303
    with engine.read_conn(app_env) as conn:
        bumped = conn.execute("SELECT monthly_contribution_cents FROM goals WHERE id=?", (goal_id,)).fetchone()
    assert bumped["monthly_contribution_cents"] == 15_000

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE goals SET monthly_contribution_cents=5000, target_month='2026-08' WHERE id=?",
            (goal_id,),
        )
    extended = client.post(
        f"/goals/{goal_id}/extend",
        data={"month": "2026-07"},
        follow_redirects=False,
    )
    assert extended.status_code == 303
    with engine.read_conn(app_env) as conn:
        pushed = conn.execute("SELECT target_month FROM goals WHERE id=?", (goal_id,)).fetchone()
    assert pushed["target_month"] == "2026-10"


def test_goal_pages_default_to_current_month_and_reject_future_close(route_empty_db):
    today = date.today().strftime("%Y-%m")
    future = repo_goals.add_months(today, 2)
    _seed_month(route_empty_db, today, [("Current Route Pool", 5_000, 0)])
    with engine.write_tx(route_empty_db) as conn:
        _insert_goal(
            conn,
            name="Future only",
            start_month=future,
            target_month=future,
            monthly_contribution_cents=5_000,
        )
    client = _client()

    goals_page = client.get("/goals")
    close_page = client.get("/goals/close")
    future_close = client.post("/goals/close", data={"month": future})

    assert goals_page.status_code == 200
    assert close_page.status_code == 200
    assert f'<option value="{today}" selected' in goals_page.text
    assert f'<option value="{today}" selected' in close_page.text
    assert f'<option value="{future}"' not in goals_page.text
    assert f'<option value="{future}"' not in close_page.text
    assert future_close.status_code == 400


def test_goal_edit_includes_linked_transaction_outside_recent_window_and_round_trips(route_empty_db):
    with engine.write_tx(route_empty_db) as conn:
        account_id = _account(conn)
        category_id = _category(conn, "Edit Linked Transaction")
        linked_txn = _expense(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-01-01",
            amount_cents=1_000,
            external_id="linked-oldest",
            merchant="Oldest",
        )
        for idx in range(85):
            month = 2 + idx // 28
            day = 1 + idx % 28
            _expense(
                conn,
                account_id=account_id,
                category_id=category_id,
                posted_on=f"2026-{month:02d}-{day:02d}",
                amount_cents=1_000 + idx,
                external_id=f"newer-{idx}",
                merchant=f"Newer {idx}",
            )
        goal_id = _insert_goal(
            conn,
            name="Linked goal",
            target_cents=50_000,
            start_month="2026-07",
            target_month="2026-12",
            monthly_contribution_cents=5_000,
            linked_transaction_id=linked_txn,
            linked_category_id=category_id,
            priority=7,
        )
    client = _client()

    edit_page = client.get(f"/goals/{goal_id}/edit?month=2026-07")
    assert edit_page.status_code == 200
    assert f'<option value="{linked_txn}" selected' in edit_page.text

    saved = client.post(
        f"/goals/{goal_id}",
        data={
            "name": "Linked goal",
            "kind": "payoff",
            "target": "500.00",
            "start_month": "2026-07",
            "target_month": "2026-12",
            "monthly_contribution": "50.00",
            "linked_transaction_id": str(linked_txn),
            "linked_category_id": str(category_id),
            "priority": "7",
            "brand_owner": "nancy",
            "color": "#FF9F43",
            "notes": "",
            "auto_fund": "1",
        },
        follow_redirects=False,
    )

    with engine.read_conn(route_empty_db) as conn:
        goal = conn.execute("SELECT linked_transaction_id FROM goals WHERE id=?", (goal_id,)).fetchone()

    assert saved.status_code == 303
    assert goal["linked_transaction_id"] == linked_txn


def test_goal_routes_validate_linked_foreign_keys(route_empty_db):
    with engine.write_tx(route_empty_db) as conn:
        goal_id = _insert_goal(conn, name="Validation target")
    client = _client()
    base_form = {
        "name": "Validation target",
        "kind": "payoff",
        "target": "500.00",
        "start_month": "2026-07",
        "target_month": "2026-12",
        "monthly_contribution": "50.00",
        "linked_transaction_id": "",
        "linked_category_id": "",
        "priority": "7",
        "brand_owner": "nancy",
        "color": "#FF9F43",
        "notes": "",
        "auto_fund": "1",
    }

    create_bad_txn = client.post("/goals", data={**base_form, "linked_transaction_id": "999999"})
    create_bad_category = client.post("/goals", data={**base_form, "linked_category_id": "999999"})
    update_bad_txn = client.post(f"/goals/{goal_id}", data={**base_form, "linked_transaction_id": "999999"})
    update_bad_category = client.post(f"/goals/{goal_id}", data={**base_form, "linked_category_id": "999999"})

    assert create_bad_txn.status_code == 400
    assert create_bad_category.status_code == 400
    assert update_bad_txn.status_code == 400
    assert update_bad_category.status_code == 400


def test_historical_goal_progress_payoff_uses_contributions_capped_to_selected_month(empty_db):
    with engine.write_tx(empty_db) as conn:
        goal_id = _insert_goal(
            conn,
            name="Historical cap",
            target_cents=60_000,
            start_month="2026-07",
            target_month="2026-12",
            monthly_contribution_cents=10_000,
        )
        conn.execute(
            """
            INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status)
            VALUES (?, '2026-07', 10000, 10000, 'manual', 'applied')
            """,
            (goal_id,),
        )
        conn.execute(
            """
            INSERT INTO goal_ledger(goal_id, month, planned_cents, actual_cents, source, status)
            VALUES (?, '2026-10', 50000, 50000, 'manual', 'applied')
            """,
            (goal_id,),
        )
        rows = repo_goals.goal_progress_rows(conn, "2026-07")

    goal = next(row for row in rows if row["goal_id"] == goal_id)
    assert goal["contributed_cents"] == 60_000
    assert goal["payoff"]["state"] == "on_track"
    assert goal["payoff"]["ahead_behind_cents"] == 0


def test_acceptance_big_ticket_payoff_flow_scaled_laptop(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        electronics = _category(conn, "Acceptance Laptop")
        txn_id = _expense(
            conn,
            account_id=account_id,
            category_id=electronics,
            posted_on="2026-07-02",
            amount_cents=950_000,
            external_id="acceptance-laptop",
            merchant="Laptop Store",
        )
        pool_category = _category(conn, "Acceptance Utilities")
        _budget(conn, pool_category, 150_000)
        _expense(
            conn,
            account_id=account_id,
            category_id=pool_category,
            posted_on="2026-07-10",
            amount_cents=50_000,
            external_id="acceptance-util-jul",
            merchant="Utility",
        )
        _expense(
            conn,
            account_id=account_id,
            category_id=pool_category,
            posted_on="2026-08-10",
            amount_cents=50_000,
            external_id="acceptance-util-aug",
            merchant="Utility",
        )
        _expense(
            conn,
            account_id=account_id,
            category_id=pool_category,
            posted_on="2026-09-10",
            amount_cents=100_000,
            external_id="acceptance-util-sep",
            merchant="Utility",
        )
        goal_id = _insert_goal(
            conn,
            name="Laptop payoff",
            kind="payoff",
            target_cents=950_000,
            start_month="2026-07",
            target_month="2027-04",
            monthly_contribution_cents=95_000,
            linked_transaction_id=txn_id,
            auto_fund=1,
            priority=1,
        )
        for month in ("2026-07", "2026-08", "2026-09"):
            repo_goals.close_month(conn, month)

    with engine.read_conn(empty_db) as conn:
        progress = conn.execute(
            "SELECT * FROM v_goal_progress WHERE goal_id=?",
            (goal_id,),
        ).fetchone()
        rows_before = _ledger_rows(conn, goal_id)
        status = repo_goals.payoff_status(
            target_cents=progress["target_cents"],
            contributed_cents=progress["contributed_cents"],
            monthly_contribution_cents=progress["monthly_contribution_cents"],
            start_month=progress["start_month"],
            target_month=progress["target_month"],
            as_of_month="2026-09",
        )

    assert progress["contributed_cents"] == 240_000
    assert progress["remaining_cents"] == 710_000
    assert status["state"] == "behind"

    with engine.write_tx(empty_db) as conn:
        catchup = repo_goals.catch_up(conn, goal_id, "2026-09")
        rows_after = _ledger_rows(conn, goal_id)
        goal = conn.execute("SELECT monthly_contribution_cents FROM goals WHERE id=?", (goal_id,)).fetchone()

    assert catchup["required_monthly_cents"] == 101_429
    assert goal["monthly_contribution_cents"] == 101_429
    assert rows_after == rows_before
