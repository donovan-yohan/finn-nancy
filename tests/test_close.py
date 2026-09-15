from __future__ import annotations

import sqlite3

import pytest

from app.db import engine, migrate, repo_close, repo_period_policy


def test_migration_creates_close_tables_and_is_idempotent(tmp_path):
    path = tmp_path / "close.sqlite"
    applied = migrate.init_db(str(path))
    assert "026_close.sql" in applied
    # Re-running applies nothing (idempotent under the ledger guard).
    assert migrate.init_db(str(path)) == []

    with engine.read_conn(str(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"closed_periods", "close_audit"} <= tables


def test_get_or_create_period_is_open_and_stable(empty_db):
    with engine.write_tx(empty_db) as conn:
        first = repo_close.get_or_create_period(conn, "2026-06")
        assert first["status"] == "open"
        again = repo_close.get_or_create_period(conn, "2026-06")
    assert again["id"] == first["id"]


def test_lock_state_transitions(empty_db):
    # Before close: not locked.
    with engine.read_conn(empty_db) as conn:
        assert repo_close.is_month_locked(conn, "2026-06") is False

    # After close: locked.
    with engine.write_tx(empty_db) as conn:
        repo_close.mark_closed(conn, "2026-06", coverage_pct=97.5,
                               uncategorized_count=0, variance_ack=True,
                               net_delta_cents=-1234, summary={"note": "clean"})
    with engine.read_conn(empty_db) as conn:
        assert repo_close.is_month_locked(conn, "2026-06") is True
        row = repo_close.get_period(conn, "2026-06")
    assert row["status"] == "closed"
    assert row["closed_at"] is not None
    assert row["coverage_pct"] == 97.5
    assert row["variance_ack"] == 1
    assert row["net_delta_cents"] == -1234

    # After reopen: unlocked again, distinct 'reopened' state, closed_at cleared.
    with engine.write_tx(empty_db) as conn:
        repo_close.reopen(conn, "2026-06", reason="correction needed")
    with engine.read_conn(empty_db) as conn:
        assert repo_close.is_month_locked(conn, "2026-06") is False
        row = repo_close.get_period(conn, "2026-06")
    assert row["status"] == "reopened"
    assert row["closed_at"] is None


def test_close_and_reopen_write_audit_rows(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo_close.mark_closed(conn, "2026-05", reason="signed off")
        repo_close.reopen(conn, "2026-05", reason="oops")

    with engine.read_conn(empty_db) as conn:
        rows = repo_close.list_audit(conn, "2026-05")
    assert len(rows) == 2
    close_row, reopen_row = rows
    assert (close_row["field"], close_row["old_value"], close_row["new_value"]) == (
        "status", "open", "closed")
    assert close_row["reason"] == "signed off"
    assert (reopen_row["old_value"], reopen_row["new_value"]) == ("closed", "reopened")


def test_record_audit_persists_and_is_retrievable_by_month(empty_db):
    with engine.write_tx(empty_db) as conn:
        audit_id = repo_close.record_audit(
            conn, month="2026-04", entity="transaction", entity_id=42,
            field="category_id", old_value=7, new_value=9, reason="override edit")

    with engine.read_conn(empty_db) as conn:
        rows = repo_close.list_audit(conn, "2026-04")
        other = repo_close.list_audit(conn, "2026-03")
    assert other == []
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == audit_id
    assert row["entity"] == "transaction"
    assert row["entity_id"] == 42
    assert (row["old_value"], row["new_value"]) == ("7", "9")


def test_close_audit_has_no_update_or_delete_path():
    # The repo deliberately exposes only insert + read for the immutable trail.
    assert not hasattr(repo_close, "update_audit")
    assert not hasattr(repo_close, "delete_audit")


def test_month_check_constraint_rejects_bad_format(empty_db):
    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(empty_db) as conn:
            conn.execute("INSERT INTO closed_periods(month) VALUES ('2026-6')")


def test_mark_closed_upserts_metrics_on_existing_open_period(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo_close.get_or_create_period(conn, "2026-07")
        repo_close.mark_closed(conn, "2026-07", coverage_pct=100.0)
    with engine.read_conn(empty_db) as conn:
        row = repo_close.get_period(conn, "2026-07")
    assert row["status"] == "closed"
    assert row["coverage_pct"] == 100.0


def test_legacy_mark_closed_cannot_bypass_exception_acknowledgements(empty_db):
    with pytest.raises(repo_period_policy.PeriodAcknowledgementRequired):
        with engine.write_tx(empty_db) as conn:
            repo_close.mark_closed(
                conn,
                "2026-07",
                coverage_pct=100.0,
                uncategorized_count=3,
            )
    with engine.read_conn(empty_db) as conn:
        assert repo_period_policy.current_state(conn, "2026-07") == "open"


def test_post_migration_legacy_close_materializes_exception_before_reopen(empty_db):
    with engine.write_tx(empty_db) as conn:
        conn.execute(
            """INSERT INTO closed_periods(month, status, summary_json)
               VALUES ('2026-01', 'closed', '{"legacy":true}')"""
        )
        reopened = repo_close.reopen(
            conn,
            "2026-01",
            reason="correct post-migration legacy close",
        )
        state = conn.execute(
            """SELECT state
               FROM v_current_period_close_state
               WHERE month='2026-01'"""
        ).fetchone()
        history = conn.execute(
            """SELECT close_state, integrity_status, is_current
               FROM v_period_close_snapshot_history
               WHERE month='2026-01'"""
        ).fetchone()

    assert reopened["status"] == "reopened"
    assert state["state"] == "reopened"
    assert tuple(history) == ("closed_with_exceptions", "verified_sha256", 0)
