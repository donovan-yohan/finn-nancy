from __future__ import annotations

import hashlib
import shutil
import sqlite3

import pytest

from app.db import engine, migrate


def _pre_031_database(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE schema_migrations(
                 filename TEXT PRIMARY KEY,
                 applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        for migration_path in sorted(migrate.MIGRATIONS_DIR.glob("*.sql")):
            if migration_path.name == "031_statement_review.sql":
                break
            conn.executescript(migration_path.read_text())
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(filename) VALUES (?)",
                (migration_path.name,),
            )
        conn.execute(
            """INSERT INTO accounts(
                 id, name, institution, kind, currency, external_ref
               )
               VALUES (1, 'Synthetic Card', 'Example', 'credit', 'CAD', 'card:4242')"""
        )
        digest = hashlib.sha256(b"synthetic statement").hexdigest()
        conn.execute(
            """INSERT INTO source_documents(
                 id, kind, original_name, storage_ref, sha256, mime_type, status
               )
               VALUES (
                 1, 'statement', 'synthetic.pdf', 'blobs/synthetic', ?,
                 'application/pdf', 'processed'
               )""",
            (digest,),
        )
        conn.execute(
            """INSERT INTO statement_lines(
                 id, source_document_id, account_id, posted_on, raw_description,
                 amount_cents, currency, statement_period, row_hash, flow_kind
               )
               VALUES (
                 1, 1, 1, '2026-06-03', 'Synthetic merchant', -1000,
                 'CAD', '2026-06', 'legacy-row', 'purchase'
               )"""
        )
        conn.commit()
    finally:
        conn.close()


def test_031_creates_review_schema_and_is_repeatable(empty_db):
    assert migrate.init_db(empty_db) == []
    with engine.read_conn(empty_db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "statement_reviews",
            "statement_review_pages",
            "statement_source_anchors",
            "statement_field_evidence",
            "statement_review_audit",
        } <= tables
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(statement_lines)")
        }
        assert {
            "review_disposition",
            "review_revision",
            "review_operation_key",
            "source_anchor_id",
            "row_confidence",
        } <= columns
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_031_conservative_legacy_and_copied_database_backfill(tmp_path):
    original = tmp_path / "pre031.sqlite"
    copy = tmp_path / "copy.sqlite"
    _pre_031_database(original)
    shutil.copy2(original, copy)

    assert migrate.init_db(copy) == [
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
    with engine.read_conn(copy) as conn:
        review = conn.execute("SELECT * FROM statement_reviews").fetchone()
        assert review["account_id"] == 1
        assert review["period_month"] == "2026-06"
        assert review["review_state"] == "legacy_unverified"
        assert review["observed_page_count"] == 0
        assert review["activity_kind"] == "transactions"
        evidence = conn.execute(
            "SELECT * FROM statement_field_evidence"
        ).fetchone()
        assert evidence["origin"] == "migration"
        assert evidence["confidence"] == 0
        assert evidence["source_anchor_id"] is None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    with sqlite3.connect(original) as conn:
        assert conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='statement_reviews'"""
        ).fetchone() is None


def test_031_evidence_audit_and_reviewed_rows_are_immutable(tmp_path):
    path = tmp_path / "pre031.sqlite"
    _pre_031_database(path)
    migrate.init_db(path)
    with engine.write_tx(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM statement_review_audit")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE statement_field_evidence SET confidence=1 WHERE id=1"
            )
        with pytest.raises(sqlite3.IntegrityError, match="excluded"):
            conn.execute("DELETE FROM statement_lines WHERE id=1")


def test_031_page_and_anchor_source_hashes_are_bound_to_review_source(
    tmp_path,
):
    path = tmp_path / "pre031.sqlite"
    _pre_031_database(path)
    migrate.init_db(path)
    with engine.write_tx(path) as conn:
        review = conn.execute("SELECT * FROM statement_reviews").fetchone()
        source_sha = conn.execute(
            "SELECT sha256 FROM source_documents WHERE id=?",
            (int(review["source_document_id"]),),
        ).fetchone()["sha256"]
        with pytest.raises(sqlite3.IntegrityError, match="source hash"):
            conn.execute(
                """INSERT INTO statement_review_pages(
                     statement_review_id, page_number, source_sha256,
                     page_sha256, included_in_extraction
                   )
                   VALUES (?,1,?,?,1)""",
                (int(review["id"]), "0" * 64, "1" * 64),
            )
        page = conn.execute(
            """INSERT INTO statement_review_pages(
                 statement_review_id, page_number, source_sha256,
                 page_sha256, included_in_extraction
               )
               VALUES (?,1,?,?,1)""",
            (int(review["id"]), source_sha, "1" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError, match="anchor hash"):
            conn.execute(
                """INSERT INTO statement_source_anchors(
                     statement_review_id, page_id, locator_kind, locator_json,
                     source_sha256, created_by
                   )
                   VALUES (?,?,'page','{}',?,'test:migration')""",
                (int(review["id"]), int(page.lastrowid), "0" * 64),
            )


def test_031_failure_rolls_back_schema_backfill_views_and_ledger(
    tmp_path, monkeypatch
):
    path = tmp_path / "pre031.sqlite"
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    _pre_031_database(path)
    source = migrate.MIGRATIONS_DIR / "031_statement_review.sql"
    failing = migration_dir / source.name
    failing.write_text(
        source.read_text()
        + "\nINSERT INTO table_that_does_not_exist(value) VALUES (1);\n"
    )
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", migration_dir)

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        migrate.init_db(path)

    with engine.read_conn(path) as conn:
        assert conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='statement_reviews'"""
        ).fetchone() is None
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(statement_lines)")
        }
        assert "review_disposition" not in columns
        assert conn.execute(
            """SELECT 1 FROM schema_migrations WHERE filename=?""",
            (source.name,),
        ).fetchone() is None
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_lines WHERE id=1"""
        ).fetchone()[0] == 1
        assert conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='view' AND name='v_statement_coverage_lines'"""
        ).fetchone() is not None
