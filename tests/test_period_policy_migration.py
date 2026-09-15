from __future__ import annotations

from app.db import engine, migrate


def test_migration_036_creates_immutable_period_policy_tables(tmp_path):
    path = tmp_path / "period-policy.sqlite"
    applied = migrate.init_db(path)
    assert "036_period_policy.sql" in applied

    with engine.read_conn(path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert {
        "period_close_cycles",
        "period_close_snapshots",
        "period_close_events",
        "period_close_exceptions",
        "period_close_preacknowledgements",
        "period_close_acknowledgements",
        "period_close_resolutions",
        "period_close_reopens",
        "period_write_overrides",
    } <= tables


def test_migration_036_imports_legacy_close_as_unverified_exception(tmp_path, monkeypatch):
    path = tmp_path / "legacy-before-036.sqlite"
    original_migrations = migrate.MIGRATIONS_DIR
    before_036 = tmp_path / "migrations-before-036"
    before_036.mkdir()
    for source in sorted(original_migrations.glob("*.sql")):
        if source.name < "036_period_policy.sql":
            (before_036 / source.name).symlink_to(source)

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", before_036)
    migrate.init_db(path)
    with engine.write_tx(path) as conn:
        conn.execute(
            """INSERT INTO closed_periods(
                 month, status, summary_json
               ) VALUES ('2026-06', 'closed', '{"legacy":true}')"""
        )

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", original_migrations)
    assert migrate.init_db(path) == [
        "036_period_policy.sql",
        "037_import_runs.sql",
        "038_card_holders.sql",
        "039_month_spine_budgets.sql",
    ]
    assert migrate.init_db(path) == []

    with engine.read_conn(path) as conn:
        state = conn.execute(
            """SELECT state, integrity_status
               FROM v_current_period_close_snapshot
               WHERE month='2026-06'"""
        ).fetchone()
        exception = conn.execute(
            """SELECT exception_type, integrity_status
               FROM v_period_close_active_exceptions"""
        ).fetchone()
        event = conn.execute(
            """SELECT event_kind, integrity_status
               FROM period_close_events"""
        ).fetchone()

    assert tuple(state) == ("closed_with_exceptions", "unverified_legacy")
    assert tuple(exception) == ("evidence_gap", "unverified_legacy")
    assert tuple(event) == ("legacy_imported", "unverified_legacy")


def test_migration_036_upgrades_consistent_database_copy(tmp_path, monkeypatch):
    source_path = tmp_path / "source-before-036.sqlite"
    copy_path = tmp_path / "proof-copy.sqlite"
    original_migrations = migrate.MIGRATIONS_DIR
    before_036 = tmp_path / "migrations-through-035"
    before_036.mkdir()
    for source in sorted(original_migrations.glob("*.sql")):
        if source.name < "036_period_policy.sql":
            (before_036 / source.name).symlink_to(source)

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", before_036)
    migrate.init_db(source_path)
    with engine.write_tx(source_path) as conn:
        conn.execute(
            """INSERT INTO closed_periods(month, status, summary_json)
               VALUES ('2026-05', 'closed', '{"proof_copy":true}')"""
        )
    source_conn = engine.connect(source_path)
    copy_conn = engine.connect(copy_path)
    try:
        source_conn.backup(copy_conn)
    finally:
        copy_conn.close()
        source_conn.close()

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", original_migrations)
    assert migrate.init_db(copy_path) == [
        "036_period_policy.sql",
        "037_import_runs.sql",
        "038_card_holders.sql",
        "039_month_spine_budgets.sql",
    ]
    with engine.read_conn(copy_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        state = conn.execute(
            """SELECT state, integrity_status
               FROM v_current_period_close_snapshot
               WHERE month='2026-05'"""
        ).fetchone()
    assert tuple(state) == ("closed_with_exceptions", "unverified_legacy")
