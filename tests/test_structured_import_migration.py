from __future__ import annotations

from app.db import engine, migrate


def test_033_adds_versioned_import_and_multiset_identity_schema(tmp_path):
    path = tmp_path / "structured.sqlite"
    applied = migrate.init_db(path)
    assert applied[-5:] == [
        "035_merchant_resolution_knowledge.sql",
        "036_period_policy.sql",
        "037_import_runs.sql",
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
        assert {
            "structured_statement_imports",
            "structured_statement_import_headers",
            "structured_statement_import_rows",
            "structured_statement_row_supersessions",
            "structured_provider_account_bindings",
            "structured_statement_import_audit",
        } <= tables
        review_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(statement_reviews)")
        }
        assert "source_kind" in review_columns
        import_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(structured_statement_imports)"
            )
        }
        assert {
            "source_sha256",
            "adapter_id",
            "adapter_version",
            "mapping_version",
            "mapping_json",
            "provider_identity_hash",
            "supersedes_import_id",
            "attempt_number",
            "config_fingerprint",
            "manual_fields_json",
            "overlap_kind",
            "revision",
        } <= import_columns
        row_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(structured_statement_import_rows)"
            )
        }
        assert {
            "fitid_hash",
            "weak_key_hash",
            "coarse_key_hash",
            "occurrence_ordinal",
            "is_pending",
            "overlap_state",
        } <= row_columns
        assert "acctid" not in {name.casefold() for name in import_columns | row_columns}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
