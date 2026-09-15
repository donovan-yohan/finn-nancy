from __future__ import annotations

import os
import shutil
import sqlite3
import uuid

import pytest

from app.db import engine, migrate


def _database_identity(path) -> str | None:
    with engine.read_conn(path) as conn:
        return migrate.read_database_identity(conn)


def test_migrations_apply_and_are_idempotent(tmp_path):
    path = tmp_path / "m.sqlite"
    applied = migrate.init_db(str(path))
    assert "001_tables.sql" in applied
    assert "002_views.sql" in applied
    assert "003_jobs.sql" in applied

    # Re-running applies nothing.
    assert migrate.init_db(str(path)) == []

    with engine.read_conn(str(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}

    assert {
        "accounts",
        "transactions",
        "source_documents",
        "jobs",
        "proposed_actions",
        "proposed_action_audit",
        "classification_labels",
        "goals",
        "goal_ledger",
        "rag_chunks",
        "rag_fts",
        "embeddings",
        "closed_periods",
        "close_audit",
        "capture_submissions",
        "capture_provenance",
        "capture_events",
        "capture_transport_consents",
        "account_statement_policies",
        "account_statement_expectations",
        "statement_expectation_documents",
        "statement_expectation_audit",
        "statement_reviews",
        "statement_review_pages",
        "statement_source_anchors",
        "statement_field_evidence",
        "statement_review_audit",
        "structured_statement_imports",
        "structured_statement_import_rows",
        "structured_statement_import_audit",
        "positive_flow_decision_events",
    } <= tables
    assert {"subscription_watchlist_decisions"} <= tables
    assert {
        "v_cashflow_monthly",
        "v_category_totals",
        "v_transactions_recent",
        "v_month_spine",
        "v_budget_vs_actual",
        "v_category_underspend_monthly",
        "v_expense_classified",
        "v_leisure_vs_bigticket",
        "v_category_monthly_trend",
        "v_top_merchants",
        "v_recurring_candidates",
        "v_cashflow_runway",
        "v_statement_coverage_lines",
        "v_statement_coverage_by_doc",
        "v_recurring_payment_series_monthly",
        "v_recurring_payment_deltas",
        "v_subscription_watchlist_candidates",
        "v_planning_category_monthly_net",
        "v_planning_category_net_trend",
        "v_goal_progress",
        "v_rag_transaction_chunks",
    } <= views

    with engine.read_conn(str(path)) as conn:
        conn.execute("SELECT * FROM rag_fts LIMIT 1").fetchall()
        for view in (
            "v_month_spine",
            "v_budget_vs_actual",
            "v_category_underspend_monthly",
            "v_expense_classified",
            "v_leisure_vs_bigticket",
            "v_category_monthly_trend",
            "v_top_merchants",
            "v_recurring_candidates",
            "v_cashflow_runway",
            "v_statement_coverage_lines",
            "v_statement_coverage_by_doc",
            "v_recurring_payment_series_monthly",
            "v_recurring_payment_deltas",
            "v_subscription_watchlist_candidates",
            "v_planning_category_monthly_net",
            "v_planning_category_net_trend",
            "v_goal_progress",
            "v_rag_transaction_chunks",
        ):
            conn.execute(f"SELECT * FROM {view} LIMIT 1").fetchall()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_migration_rolls_back_schema_and_ledger(tmp_path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    migration_path = migration_dir / "999_atomic_probe.sql"
    migration_path.write_text(
        """
        CREATE TABLE atomic_probe(id INTEGER PRIMARY KEY);
        INSERT INTO table_that_does_not_exist(value) VALUES (1);
        """
    )
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", migration_dir)
    db_path = tmp_path / "atomic-script-failure.sqlite"

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        migrate.init_db(db_path)

    with engine.read_conn(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='atomic_probe'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE filename=?",
            (migration_path.name,),
        ).fetchone() is None

    migration_path.write_text("CREATE TABLE atomic_probe(id INTEGER PRIMARY KEY);")
    assert migrate.init_db(db_path) == [migration_path.name]
    with engine.read_conn(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='atomic_probe'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename=?",
            (migration_path.name,),
        ).fetchone()[0] == 1


def test_ledger_insert_failure_rolls_back_migration(tmp_path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    migration_path = migration_dir / "999_ledger_failure.sql"
    migration_path.write_text(
        f"""
        CREATE TABLE atomic_ledger_probe(id INTEGER PRIMARY KEY);
        CREATE TRIGGER force_ledger_failure
        BEFORE INSERT ON schema_migrations
        WHEN NEW.filename = '{migration_path.name}'
        BEGIN
          SELECT RAISE(ABORT, 'forced ledger failure');
        END;
        """
    )
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", migration_dir)
    db_path = tmp_path / "atomic-ledger-failure.sqlite"

    with pytest.raises(sqlite3.IntegrityError, match="forced ledger failure"):
        migrate.init_db(db_path)

    with engine.read_conn(db_path) as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='atomic_ledger_probe'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='force_ledger_failure'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM schema_migrations WHERE filename=?",
            (migration_path.name,),
        ).fetchone() is None


def test_existing_migrations_are_atomic_wrapper_compatible():
    for path in sorted(migrate.MIGRATIONS_DIR.glob("*.sql")):
        migrate._assert_atomic_migration_compatible(path, path.read_text())


@pytest.mark.parametrize(
    "statement",
    [
        "BEGIN IMMEDIATE;",
        "COMMIT;",
        "END;",
        "ROLLBACK;",
        "SAVEPOINT migration;",
        "RELEASE migration;",
        "VACUUM;",
        "ATTACH DATABASE 'other.sqlite' AS other;",
        "DETACH DATABASE other;",
        "PRAGMA foreign_keys=OFF;",
    ],
)
def test_atomic_migration_wrapper_rejects_incompatible_control(tmp_path, statement):
    path = tmp_path / "999_incompatible.sql"
    with pytest.raises(RuntimeError, match="incompatible with atomic migrations"):
        migrate._assert_atomic_migration_compatible(path, statement)


def test_atomic_migration_wrapper_allows_trigger_body(tmp_path):
    path = tmp_path / "999_trigger.sql"
    migrate._assert_atomic_migration_compatible(
        path,
        """
        CREATE TRIGGER allowed_trigger
        AFTER INSERT ON example
        BEGIN
          SELECT 1;
        END;
        """,
    )


def test_database_uuid_is_initialized_on_fresh_db_and_idempotent(tmp_path):
    path = tmp_path / "fresh.sqlite"
    migrate.init_db(path)
    first = _database_identity(path)
    assert first is not None
    assert str(uuid.UUID(first)) == first

    assert migrate.init_db(path) == []
    assert _database_identity(path) == first
    with engine.read_conn(path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM app_settings WHERE key = ?",
            (migrate.DATABASE_IDENTITY_SETTING_KEY,),
        ).fetchone()[0]
    assert count == 1


def test_adopts_legacy_db_without_ledger(tmp_path):
    """A pre-existing canonical DB with no schema_migrations gets baselined, then only 003+ run."""
    import sqlite3

    from app.db.migrate import MIGRATIONS_DIR

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    for f in ("001_tables.sql", "002_views.sql"):
        conn.executescript((MIGRATIONS_DIR / f).read_text())
    conn.commit()
    conn.close()

    applied = migrate.init_db(str(path))
    assert "001_tables.sql" not in applied and "002_views.sql" not in applied
    assert "003_jobs.sql" in applied and "004_ingestion.sql" in applied

    with engine.read_conn(str(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        acct_cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    assert {
        "jobs",
        "ingest_extractions",
        "merchant_aliases",
        "budgets",
        "app_settings",
        "household_members",
    } <= tables
    assert "external_ref" in acct_cols
    with engine.read_conn(str(path)) as conn:
        cat_cols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
        budget_cols = {r[1] for r in conn.execute("PRAGMA table_info(budgets)")}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert "is_leisure" in cat_cols
    assert {"owner", "owner_member_id"} <= budget_cols
    assert _database_identity(path) is not None


def test_flow_backfill_migrates_legacy_rows_without_guessing_and_is_repeatable(tmp_path):
    path = tmp_path / "pre-flow.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE schema_migrations(
             filename TEXT PRIMARY KEY,
             applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    try:
        for migration_path in sorted(migrate.MIGRATIONS_DIR.glob("*.sql")):
            if migration_path.name == "028_flow_semantics.sql":
                break
            conn.executescript(migration_path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations(filename) VALUES (?)",
                (migration_path.name,),
            )
        conn.execute(
            """INSERT INTO accounts(id, name, institution, kind, currency)
               VALUES (1, 'Legacy Account', 'Synthetic Bank', 'chequing', 'CAD')"""
        )
        for external_id, source, amount_cents in (
            ("legacy-receipt", "receipt", -1000),
            ("legacy-opening", "opening", 5000),
            ("legacy-adjustment", "adjustment", -50),
            ("legacy-manual", "manual", -700),
            ("legacy-statement", "statement", 800),
        ):
            conn.execute(
                """INSERT INTO transactions(
                     account_id, posted_on, description, amount_cents,
                     source, external_id)
                   VALUES (1, '2026-06-10', ?, ?, ?, ?)""",
                (external_id, amount_cents, source, external_id),
            )
        conn.commit()
    finally:
        conn.close()

    assert migrate.init_db(path) == [
        "028_flow_semantics.sql",
        "029_capture_submissions.sql",
        "030_statement_expectations.sql",
        "031_statement_review.sql",
        "032_capture_telemetry.sql",
        "033_structured_statement_imports.sql",
        "034_positive_flow_reviews.sql",
        "035_merchant_resolution_knowledge.sql",
        "036_period_policy.sql",
        "037_import_runs.sql",
        "038_card_holders.sql",
        "039_month_spine_budgets.sql",
    ]
    with engine.read_conn(path) as conn:
        observed = {
            row["external_id"]: row["flow_kind"]
            for row in conn.execute(
                "SELECT external_id, flow_kind FROM transactions ORDER BY id"
            )
        }
        pending = {
            row["transaction_id"]
            for row in conn.execute(
                "SELECT transaction_id FROM transaction_flow_reviews WHERE status='pending'"
            )
        }
        pending_external_ids = {
            row["external_id"]
            for row in conn.execute(
                f"""SELECT external_id FROM transactions
                    WHERE id IN ({','.join('?' for _ in pending)})""",
                sorted(pending),
            )
        }
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM transaction_flow_audit"
        ).fetchone()[0]

    assert observed == {
        "legacy-receipt": "purchase",
        "legacy-opening": "opening",
        "legacy-adjustment": "adjustment",
        "legacy-manual": "unknown",
        "legacy-statement": "unknown",
    }
    assert pending_external_ids == {"legacy-manual", "legacy-statement"}
    assert audit_count == 3

    assert migrate.init_db(path) == []
    with engine.read_conn(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transaction_flow_audit"
        ).fetchone()[0] == audit_count
        assert conn.execute(
            "SELECT COUNT(*) FROM transaction_flow_reviews WHERE status='pending'"
        ).fetchone()[0] == len(pending)


def test_database_uuid_survives_copy_and_reinit(tmp_path):
    source = tmp_path / "source.sqlite"
    copied = tmp_path / "copied.sqlite"
    migrate.init_db(source)
    source_identity = _database_identity(source)

    shutil.copy2(source, copied)
    assert migrate.init_db(copied) == []
    assert _database_identity(copied) == source_identity


def test_database_uuid_changes_when_same_path_is_replaced(tmp_path):
    live = tmp_path / "live.sqlite"
    replacement = tmp_path / "replacement.sqlite"
    migrate.init_db(live)
    migrate.init_db(replacement)
    original_identity = _database_identity(live)
    replacement_identity = _database_identity(replacement)
    assert replacement_identity != original_identity

    for suffix in ("-wal", "-shm"):
        (tmp_path / f"live.sqlite{suffix}").unlink(missing_ok=True)
        (tmp_path / f"replacement.sqlite{suffix}").unlink(missing_ok=True)
    os.replace(replacement, live)

    assert _database_identity(live) == replacement_identity


def test_sample_db_has_rows(sample_db):
    with engine.read_conn(sample_db) as conn:
        (tx_count,) = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
        (cf_count,) = conn.execute("SELECT COUNT(*) FROM v_cashflow_monthly").fetchone()
        (configured_policy_count,) = conn.execute(
            """SELECT COUNT(*)
               FROM account_statement_policies
               WHERE created_by='fixture:sample'
                 AND configuration_state='configured'
                 AND requirement_mode='required'
                 AND cadence='monthly'"""
        ).fetchone()
    assert tx_count > 0
    assert cf_count > 0
    assert configured_policy_count == 3
