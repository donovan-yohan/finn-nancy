from __future__ import annotations

import pytest

from app.close import anomaly
from app.db import engine, repo_ledger, repo_statements


def _account(conn, name: str = "Everyday Card") -> int:
    return conn.execute(
        "INSERT INTO accounts(name, institution, kind, currency) "
        "VALUES (?, 'Test', 'credit', 'CAD')",
        (name,),
    ).lastrowid


def _category(conn, name: str) -> int:
    return conn.execute(
        "INSERT INTO categories(name, kind, brand_owner, color) "
        "VALUES (?, 'expense', 'nancy', '#ff9f43')",
        (name,),
    ).lastrowid


def _expense(
    conn,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    merchant: str,
    amount_cents: int,
    external_id: str,
) -> None:
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=f"{merchant} charge",
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


def test_category_increase_beyond_threshold_is_flagged(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        dining = _category(conn, "Dining")
        for i, month in enumerate(("2026-03", "2026-04", "2026-05")):
            _expense(
                conn,
                account_id=acct,
                category_id=dining,
                posted_on=f"{month}-10",
                merchant=f"Bistro {i}",
                amount_cents=10000,
                external_id=f"dining-{month}",
            )
        _expense(
            conn,
            account_id=acct,
            category_id=dining,
            posted_on="2026-06-10",
            merchant="Bistro X",
            amount_cents=15000,
            external_id="dining-2026-06",
        )

    with engine.read_conn(empty_db) as conn:
        found = anomaly.scan_anomalies(conn, "2026-06", min_recurring_months=99)

    assert len(found) == 1
    item = found[0]
    assert item.kind == "category_deviation"
    assert item.direction == "increase"
    assert item.category_name == "Dining"
    assert item.current_cents == 15000
    assert item.baseline_cents == 10000
    assert item.deviation_cents == 5000
    assert item.pct_change == 50.0
    assert item.severity_cents == 5000
    assert "up 50.0%" in item.detail


def test_category_drop_to_zero_is_flagged(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        rent = _category(conn, "Rent")
        misc = _category(conn, "Misc")
        for month in ("2026-03", "2026-04", "2026-05"):
            _expense(
                conn,
                account_id=acct,
                category_id=rent,
                posted_on=f"{month}-01",
                merchant="Landlord",
                amount_cents=210000,
                external_id=f"rent-{month}",
            )
        # Some unrelated activity so the target month exists on the spine.
        _expense(
            conn,
            account_id=acct,
            category_id=misc,
            posted_on="2026-06-05",
            merchant="Corner Store",
            amount_cents=4200,
            external_id="misc-2026-06",
        )

    with engine.read_conn(empty_db) as conn:
        found = anomaly.scan_anomalies(conn, "2026-06", min_recurring_months=99)

    assert len(found) == 1
    item = found[0]
    assert item.kind == "category_deviation"
    assert item.direction == "decrease"
    assert item.category_name == "Rent"
    assert item.current_cents == 0
    assert item.baseline_cents == 210000
    assert item.deviation_cents == -210000
    assert item.pct_change == -100.0
    assert "down 100.0%" in item.detail


def test_missing_recurring_payment_is_flagged(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn, "Recurring Card")
        subs = _category(conn, "Subscriptions")
        for month in ("2026-03", "2026-04", "2026-05"):
            _expense(
                conn,
                account_id=acct,
                category_id=subs,
                posted_on=f"{month}-04",
                merchant="Koodo",
                amount_cents=5000,
                external_id=f"koodo-{month}",
            )
        # Spotify recurs and IS present this month -> must not be flagged.
        for month in ("2026-03", "2026-04", "2026-05", "2026-06"):
            _expense(
                conn,
                account_id=acct,
                category_id=subs,
                posted_on=f"{month}-06",
                merchant="Spotify",
                amount_cents=1200,
                external_id=f"spotify-{month}",
            )

    with engine.read_conn(empty_db) as conn:
        # Huge dollar floor suppresses the category-deviation check so we isolate
        # the missing-recurring signal.
        found = anomaly.scan_anomalies(
            conn, "2026-06", min_deviation_cents=100_000_000, min_recurring_months=3
        )

    assert len(found) == 1
    item = found[0]
    assert item.kind == "missing_recurring"
    assert item.direction == "missing"
    assert item.merchant == "Koodo"
    assert item.account_name == "Recurring Card"
    assert item.current_cents == 0
    assert item.baseline_cents == 5000
    assert item.severity_cents == 5000
    assert "no charge this month" in item.detail


def test_no_anomaly_when_spend_tracks_trend(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        groceries = _category(conn, "Groceries")
        transit = _category(conn, "Transit")
        # Groceries perfectly steady across four months.
        for month in ("2026-03", "2026-04", "2026-05", "2026-06"):
            _expense(
                conn,
                account_id=acct,
                category_id=groceries,
                posted_on=f"{month}-12",
                merchant="Grocer",
                amount_cents=40000,
                external_id=f"groceries-{month}",
            )
        # Transit wiggles but stays inside the percentage threshold.
        for month, cents in (
            ("2026-03", 10000),
            ("2026-04", 10000),
            ("2026-05", 10000),
            ("2026-06", 11000),
        ):
            _expense(
                conn,
                account_id=acct,
                category_id=transit,
                posted_on=f"{month}-15",
                merchant="Metro",
                amount_cents=cents,
                external_id=f"transit-{month}",
            )

    with engine.read_conn(empty_db) as conn:
        found = anomaly.scan_anomalies(conn, "2026-06")

    assert found == []


def test_anomalies_sorted_worst_first(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        travel = _category(conn, "Travel")
        gym_cat = _category(conn, "Gym Membership")
        for month in ("2026-03", "2026-04", "2026-05"):
            _expense(
                conn,
                account_id=acct,
                category_id=travel,
                posted_on=f"{month}-08",
                merchant="Airline",
                amount_cents=50000,
                external_id=f"travel-{month}",
            )
        # Large travel spike this month -> big-dollar category anomaly.
        _expense(
            conn,
            account_id=acct,
            category_id=travel,
            posted_on="2026-06-08",
            merchant="Airline",
            amount_cents=200000,
            external_id="travel-2026-06",
        )
        # A small recurring gym payment that disappears this month.
        for month in ("2026-03", "2026-04", "2026-05"):
            _expense(
                conn,
                account_id=acct,
                category_id=gym_cat,
                posted_on=f"{month}-20",
                merchant="Gym",
                amount_cents=8000,
                external_id=f"gym-{month}",
            )

    with engine.read_conn(empty_db) as conn:
        found = anomaly.scan_anomalies(conn, "2026-06")

    assert found, "expected anomalies"
    severities = [item.severity_cents for item in found]
    assert severities == sorted(severities, reverse=True)
    assert found[0].category_name == "Travel"
    assert found[0].direction == "increase"
    assert any(
        item.kind == "missing_recurring" and item.merchant == "Gym" for item in found
    )


def test_threshold_is_configurable(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _account(conn)
        dining = _category(conn, "Dining")
        for month in ("2026-03", "2026-04", "2026-05"):
            _expense(
                conn,
                account_id=acct,
                category_id=dining,
                posted_on=f"{month}-10",
                merchant="Bistro",
                amount_cents=30000,
                external_id=f"dining-{month}",
            )
        # A 15% increase ($45.00 over trend): below the default 30% band, above
        # a 10% band, and comfortably past the dollar floor either way.
        _expense(
            conn,
            account_id=acct,
            category_id=dining,
            posted_on="2026-06-10",
            merchant="Bistro",
            amount_cents=34500,
            external_id="dining-2026-06",
        )

    with engine.read_conn(empty_db) as conn:
        strict = anomaly.scan_anomalies(
            conn, "2026-06", deviation_pct=30.0, min_recurring_months=99
        )
        lenient = anomaly.scan_anomalies(
            conn, "2026-06", deviation_pct=10.0, min_recurring_months=99
        )

    assert strict == []
    assert len(lenient) == 1
    assert lenient[0].pct_change == 15.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"trailing_months": 0}, "trailing_months must be >= 1"),
        ({"min_recurring_months": 0}, "min_recurring_months must be >= 1"),
        ({"deviation_pct": -1.0}, "deviation_pct must be >= 0"),
        ({"min_deviation_cents": -1}, "min_deviation_cents must be >= 0"),
    ],
)
def test_out_of_range_thresholds_rejected(empty_db, kwargs, message):
    with engine.read_conn(empty_db) as conn:
        with pytest.raises(ValueError, match=message):
            anomaly.scan_anomalies(conn, "2026-06", **kwargs)
