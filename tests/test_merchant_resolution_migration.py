from __future__ import annotations

import shutil
import sqlite3

import pytest

from app.db import engine, migrate


def _init_through_034(tmp_path, monkeypatch):
    migration_dir = tmp_path / "pre-035-migrations"
    migration_dir.mkdir()
    for source in sorted(migrate.MIGRATIONS_DIR.glob("*.sql")):
        if source.name >= "035_":
            continue
        shutil.copy2(source, migration_dir / source.name)
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", migration_dir)
    path = tmp_path / "pre-035.sqlite"
    migrate.init_db(path)
    return path


def test_035_adds_immutable_scoped_merchant_knowledge_schema(tmp_path):
    path = tmp_path / "merchant-resolution.sqlite"

    applied = migrate.init_db(path)

    assert applied[-2:] == [
        "038_card_holders.sql",
        "039_month_spine_budgets.sql",
    ]
    assert migrate.init_db(path) == []
    with engine.read_conn(path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        views = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            )
        }
        assert {
            "merchant_entities",
            "merchant_descriptor_patterns",
            "merchant_resolution_claims",
            "merchant_resolution_events",
        } <= tables
        assert {
            "v_current_merchant_resolution_claims",
            "v_active_merchant_resolution_claims",
            "v_transaction_split_category_resolution",
            "v_merchant_category_statistics",
            "v_expense_resolution_status",
            "v_expense_resolution_monthly_control",
            "v_resolved_expense_category_monthly",
            "v_report_budget_vs_actual",
            "v_report_category_monthly",
            "v_report_category_totals",
            "v_report_category_monthly_trend",
        } <= views
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_035_backfills_legacy_aliases_as_unverified_and_freezes_bypass(
    tmp_path, monkeypatch
):
    real_migrations = migrate.MIGRATIONS_DIR
    path = _init_through_034(tmp_path, monkeypatch)
    with engine.write_tx(path) as conn:
        category_id = int(
            conn.execute(
                """
                INSERT INTO categories(name, kind, brand_owner)
                VALUES ('Groceries', 'expense', 'shared')
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO merchant_aliases(
              raw_pattern, canonical, category_id, hits
            )
            VALUES ('MARKET 7', 'Market 7', ?, 3)
            """,
            (category_id,),
        )

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", real_migrations)
    assert migrate.init_db(path) == [
        "035_merchant_resolution_knowledge.sql",
        "036_period_policy.sql",
        "037_import_runs.sql",
        "038_card_holders.sql",
        "039_month_spine_budgets.sql",
    ]

    with engine.write_tx(path) as conn:
        claims = conn.execute(
            """
            SELECT claim_kind, event_kind, trust_state
            FROM v_current_merchant_resolution_claims
            ORDER BY claim_kind
            """
        ).fetchall()
        assert [tuple(row) for row in claims] == [
            ("canonical_merchant", "legacy_imported", "legacy_unverified"),
            ("expense_category", "legacy_imported", "legacy_unverified"),
        ]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM v_active_merchant_resolution_claims"
            ).fetchone()[0]
            == 0
        )
        for statement in (
            """
            INSERT INTO merchant_aliases(raw_pattern, canonical, hits)
            VALUES ('NEW', 'New', 1)
            """,
            "UPDATE merchant_aliases SET hits=4 WHERE raw_pattern='MARKET 7'",
            "DELETE FROM merchant_aliases WHERE raw_pattern='MARKET 7'",
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="merchant_aliases is frozen",
            ):
                conn.execute(statement)


def test_035_fails_closed_when_two_active_lines_consume_one_transaction(
    tmp_path, monkeypatch
):
    real_migrations = migrate.MIGRATIONS_DIR
    path = _init_through_034(tmp_path, monkeypatch)
    with engine.write_tx(path) as conn:
        account_id = int(
            conn.execute(
                """
                INSERT INTO accounts(name, kind)
                VALUES ('Card', 'credit')
                """
            ).lastrowid
        )
        document_id = int(
            conn.execute(
                """
                INSERT INTO source_documents(kind, storage_ref, status)
                VALUES ('statement', 'statement:dirty', 'processed')
                """
            ).lastrowid
        )
        transaction_id = int(
            conn.execute(
                """
                INSERT INTO transactions(
                  account_id, posted_on, description, amount_cents,
                  source, external_id, flow_kind
                )
                VALUES (?, '2026-01-01', 'Receipt', -1000,
                        'receipt', 'receipt:dirty', 'purchase')
                """,
                (account_id,),
            ).lastrowid
        )
        for ordinal in (1, 2):
            conn.execute(
                """
                INSERT INTO statement_lines(
                  source_document_id, account_id, posted_on, raw_description,
                  amount_cents, row_hash, match_status,
                  matched_transaction_id, match_method
                )
                VALUES (?, ?, '2026-01-02', ?, -1000, ?, 'matched', ?, 'manual')
                """,
                (
                    document_id,
                    account_id,
                    f"Statement row {ordinal}",
                    f"dirty-row-{ordinal}",
                    transaction_id,
                ),
            )

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", real_migrations)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        migrate.init_db(path)
    with engine.read_conn(path) as conn:
        assert (
            conn.execute(
                """
                SELECT 1 FROM schema_migrations
                WHERE filename='035_merchant_resolution_knowledge.sql'
                """
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='merchant_resolution_claims'
                """
            ).fetchone()
            is None
        )
