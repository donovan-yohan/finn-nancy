from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.db import engine, migrate


MIGRATION = "030_statement_expectations.sql"
REVIEW_MIGRATION = "031_statement_review.sql"
CAPTURE_MIGRATION = "032_capture_telemetry.sql"
STRUCTURED_IMPORT_MIGRATION = "033_structured_statement_imports.sql"
POSITIVE_FLOW_MIGRATION = "034_positive_flow_reviews.sql"
MERCHANT_RESOLUTION_MIGRATION = "035_merchant_resolution_knowledge.sql"
PERIOD_POLICY_MIGRATION = "036_period_policy.sql"
IMPORT_RUNS_MIGRATION = "037_import_runs.sql"
CARD_HOLDERS_MIGRATION = "038_card_holders.sql"
MONTH_SPINE_MIGRATION = "039_month_spine_budgets.sql"


def _apply_pre_030(path: Path) -> None:
    """Build a synthetic legacy database whose ledger stops immediately before 030."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """CREATE TABLE schema_migrations(
                 filename TEXT PRIMARY KEY,
                 applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        for migration_path in sorted(migrate.MIGRATIONS_DIR.glob("*.sql")):
            if migration_path.name >= MIGRATION:
                continue
            conn.executescript(migration_path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations(filename) VALUES (?)",
                (migration_path.name,),
            )
        conn.commit()
        migrate._ensure_database_identity(conn)
    finally:
        conn.close()


def _insert_legacy_evidence(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            INSERT INTO accounts(id, name, institution, kind, currency)
            VALUES
              (1, 'Wallet', '', 'cash', 'CAD'),
              (2, 'Legacy Card', 'Synthetic Bank', 'credit', 'CAD'),
              (3, 'Legacy Savings', 'Synthetic Bank', 'savings', 'CAD');

            INSERT INTO source_documents(
              id, kind, original_name, storage_ref, sha256, mime_type, status
            )
            VALUES
              (101, 'statement', 'exact-a.pdf', 'legacy/exact-a.pdf', 'sha-101',
               'application/pdf', 'processed'),
              (102, 'statement', 'exact-b.pdf', 'legacy/exact-b.pdf', 'sha-102',
               'application/pdf', 'processed'),
              (103, 'statement', 'null-account.pdf', 'legacy/null-account.pdf', 'sha-103',
               'application/pdf', 'needs_review'),
              (104, 'statement', 'mixed-account.pdf', 'legacy/mixed-account.pdf', 'sha-104',
               'application/pdf', 'needs_review'),
              (105, 'statement', 'mixed-period.pdf', 'legacy/mixed-period.pdf', 'sha-105',
               'application/pdf', 'needs_review'),
              (106, 'statement', 'invalid-period.pdf', 'legacy/invalid-period.pdf', 'sha-106',
               'application/pdf', 'needs_review'),
              (107, 'receipt', 'not-a-statement.pdf', 'legacy/receipt.pdf', 'sha-107',
               'application/pdf', 'processed'),
              (108, 'statement', 'zero-lines.pdf', 'legacy/zero-lines.pdf', 'sha-108',
               'application/pdf', 'processed');
            """
        )

        rows = (
            # Two exact documents for one account-period prove multi-document support.
            (1001, 101, 2, "2026-06-01", "Exact A", -1000, "2026-06", "row-1001"),
            (1002, 101, 2, "2026-06-02", "Exact A2", -2000, "2026-06", "row-1002"),
            (1003, 102, 2, "2026-06-03", "Exact B", -3000, "2026-06", "row-1003"),
            # Every remaining document is deliberately ineligible.
            (1004, 103, None, "2026-06-04", "No account", -4000, "2026-06", "row-1004"),
            (1005, 104, 2, "2026-06-05", "Mixed account A", -5000, "2026-06", "row-1005"),
            (1006, 104, 3, "2026-06-06", "Mixed account B", -6000, "2026-06", "row-1006"),
            (1007, 105, 2, "2026-06-07", "Mixed period A", -7000, "2026-06", "row-1007"),
            (1008, 105, 2, "2026-07-08", "Mixed period B", -8000, "2026-07", "row-1008"),
            (1009, 106, 2, "2026-06-09", "Bad period", -9000, "2026-13", "row-1009"),
            (1010, 107, 2, "2026-06-10", "Receipt row", -10000, "2026-06", "row-1010"),
        )
        conn.executemany(
            """INSERT INTO statement_lines(
                 id, source_document_id, account_id, posted_on, raw_description,
                 amount_cents, statement_period, row_hash
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return {
            "cash_account": 1,
            "card_account": 2,
            "savings_account": 3,
            "exact_a": 101,
            "exact_b": 102,
            "null_account": 103,
            "mixed_account": 104,
            "mixed_period": 105,
            "invalid_period": 106,
            "receipt": 107,
            "zero_lines": 108,
        }
    finally:
        conn.close()


def _assert_integrity(path: Path) -> None:
    with engine.read_conn(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_policy(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    effective_from_month: str = "2026-01",
    configuration_state: str = "configured",
    requirement_mode: str | None = "required",
    cadence: str | None = "monthly",
    anchor_month: int | None = None,
    active_from: str | None = None,
    active_to: str | None = None,
) -> int:
    return int(
        conn.execute(
            """INSERT INTO account_statement_policies(
                 account_id, effective_from_month, configuration_state,
                 requirement_mode, cadence, anchor_month, active_from, active_to,
                 created_by, reason
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test:policy', 'synthetic policy')""",
            (
                account_id,
                effective_from_month,
                configuration_state,
                requirement_mode,
                cadence,
                anchor_month,
                active_from,
                active_to,
            ),
        ).lastrowid
    )


def _insert_expectation(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    policy_id: int,
    period_month: str,
    requirement_state: str = "required",
    lifecycle_state: str | None = "expected",
    origin: str = "policy",
) -> int:
    return int(
        conn.execute(
            """INSERT INTO account_statement_expectations(
                 account_id, period_month, policy_id, origin,
                 requirement_state, lifecycle_state, created_by, reason
               )
               VALUES (?, ?, ?, ?, ?, ?, 'test:prepare', 'synthetic expectation')""",
            (
                account_id,
                period_month,
                policy_id,
                origin,
                requirement_state,
                lifecycle_state,
            ),
        ).lastrowid
    )


def _insert_statement_document(
    conn: sqlite3.Connection,
    *,
    doc_id: int,
    account_id: int,
    period_month: str,
    match_status: str = "unmatched",
    is_pending: int = 0,
) -> None:
    conn.execute(
        """INSERT INTO source_documents(
             id, kind, original_name, storage_ref, sha256, mime_type, status
           )
           VALUES (?, 'statement', ?, ?, ?, 'application/pdf', 'processed')""",
        (
            doc_id,
            f"statement-{doc_id}.pdf",
            f"synthetic/statement-{doc_id}.pdf",
            f"synthetic-sha-{doc_id}",
        ),
    )
    conn.execute(
        """INSERT INTO statement_lines(
             source_document_id, account_id, posted_on, raw_description,
             amount_cents, statement_period, row_hash, match_status, is_pending
           )
           VALUES (?, ?, ?, ?, -1000, ?, ?, ?, ?)""",
        (
            doc_id,
            account_id,
            f"{period_month}-10",
            f"Synthetic {doc_id}",
            period_month,
            f"synthetic-row-{doc_id}",
            match_status,
            is_pending,
        ),
    )


def _insert_link(
    conn: sqlite3.Connection,
    *,
    expectation_id: int,
    source_document_id: int,
) -> int:
    return int(
        conn.execute(
            """INSERT INTO statement_expectation_documents(
                 expectation_id, source_document_id, attached_by, attach_reason
               )
               VALUES (?, ?, 'test:attach', 'synthetic statement evidence')""",
            (expectation_id, source_document_id),
        ).lastrowid
    )


def _audited_transition(
    conn: sqlite3.Connection,
    expectation_id: int,
    *,
    operation_key: str,
    new_requirement_state: str,
    new_lifecycle_state: str | None,
    event_kind: str = "lifecycle_transition",
    waived_at: str | None = None,
    waived_by: str | None = None,
    waiver_reason: str | None = None,
) -> None:
    row = conn.execute(
        "SELECT * FROM account_statement_expectations WHERE id=?",
        (expectation_id,),
    ).fetchone()
    assert row is not None
    conn.execute(
        """INSERT INTO statement_expectation_audit(
             operation_key, event_kind, policy_id, expectation_id, account_id,
             period_month, source_document_id,
             old_requirement_state, new_requirement_state,
             old_lifecycle_state, new_lifecycle_state, actor, reason
           )
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'test:transition',
                   'synthetic transition')""",
        (
            operation_key,
            event_kind,
            row["policy_id"],
            expectation_id,
            row["account_id"],
            row["period_month"],
            row["requirement_state"],
            new_requirement_state,
            row["lifecycle_state"],
            new_lifecycle_state,
        ),
    )
    conn.execute(
        """UPDATE account_statement_expectations
           SET requirement_state=?, lifecycle_state=?,
               waived_at=?, waived_by=?, waiver_reason=?,
               last_transition_key=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (
            new_requirement_state,
            new_lifecycle_state,
            waived_at,
            waived_by,
            waiver_reason,
            operation_key,
            expectation_id,
        ),
    )


def test_fresh_migration_has_exact_contract_and_second_run_is_idempotent(tmp_path):
    path = tmp_path / "fresh.sqlite"

    applied = migrate.init_db(path)
    assert MIGRATION in applied
    assert migrate.init_db(path) == []

    expected_columns = {
        "account_statement_policies": (
            "id",
            "account_id",
            "effective_from_month",
            "configuration_state",
            "requirement_mode",
            "cadence",
            "anchor_month",
            "active_from",
            "active_to",
            "created_by",
            "reason",
            "created_at",
        ),
        "account_statement_expectations": (
            "id",
            "account_id",
            "period_month",
            "policy_id",
            "origin",
            "requirement_state",
            "lifecycle_state",
            "waived_at",
            "waived_by",
            "waiver_reason",
            "created_by",
            "reason",
            "last_transition_key",
            "created_at",
            "updated_at",
        ),
        "statement_expectation_documents": (
            "id",
            "expectation_id",
            "source_document_id",
            "status",
            "attached_by",
            "attach_reason",
            "attached_at",
            "detached_at",
            "detached_by",
            "detach_reason",
        ),
        "statement_expectation_audit": (
            "id",
            "operation_key",
            "event_kind",
            "policy_id",
            "expectation_id",
            "account_id",
            "period_month",
            "source_document_id",
            "old_requirement_state",
            "new_requirement_state",
            "old_lifecycle_state",
            "new_lifecycle_state",
            "actor",
            "reason",
            "created_at",
        ),
    }
    with engine.read_conn(path) as conn:
        observed = {
            table: tuple(
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            )
            for table in expected_columns
        }
        migration_rows = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename=?",
            (MIGRATION,),
        ).fetchone()[0]
    assert observed == expected_columns
    assert migration_rows == 1
    _assert_integrity(path)


def test_account_insert_creates_conservative_audited_baseline_policy(tmp_path):
    path = tmp_path / "new-accounts.sqlite"
    migrate.init_db(path)

    with engine.write_tx(path) as conn:
        cash_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('Wallet', '', 'cash', 'CAD')"""
            ).lastrowid
        )
        credit_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('New Card', 'Synthetic', 'credit', 'CAD')"""
            ).lastrowid
        )

    with engine.read_conn(path) as conn:
        rows = {
            int(row["account_id"]): row
            for row in conn.execute(
                """SELECT * FROM account_statement_policies
                   WHERE account_id IN (?, ?)
                   ORDER BY account_id""",
                (cash_id, credit_id),
            )
        }
        audits = conn.execute(
            """SELECT audit.*, policy.created_by
               FROM statement_expectation_audit audit
               JOIN account_statement_policies policy
                 ON policy.id=audit.policy_id
               WHERE audit.event_kind='policy_recorded'
                 AND audit.account_id IN (?, ?)
               ORDER BY audit.account_id""",
            (cash_id, credit_id),
        ).fetchall()

    assert set(rows) == {cash_id, credit_id}
    assert rows[cash_id]["effective_from_month"] == "0001-01"
    assert rows[credit_id]["effective_from_month"] == "0001-01"
    assert (
        rows[cash_id]["configuration_state"],
        rows[cash_id]["requirement_mode"],
        rows[cash_id]["cadence"],
        rows[cash_id]["anchor_month"],
    ) == ("configured", "no_statement", "none", None)
    assert (
        rows[credit_id]["configuration_state"],
        rows[credit_id]["requirement_mode"],
        rows[credit_id]["cadence"],
        rows[credit_id]["anchor_month"],
    ) == ("unconfigured", None, None, None)
    assert all(row["created_by"] == "system:account-create" for row in audits)
    assert len(audits) == 2
    _assert_integrity(path)


def test_legacy_backfill_is_conservative_exact_and_audited(tmp_path):
    path = tmp_path / "legacy.sqlite"
    _apply_pre_030(path)
    ids = _insert_legacy_evidence(path)

    assert migrate.init_db(path) == [
        MIGRATION,
        REVIEW_MIGRATION,
        CAPTURE_MIGRATION,
        STRUCTURED_IMPORT_MIGRATION,
        POSITIVE_FLOW_MIGRATION,
        MERCHANT_RESOLUTION_MIGRATION,
        PERIOD_POLICY_MIGRATION,
        IMPORT_RUNS_MIGRATION,
        CARD_HOLDERS_MIGRATION,
        MONTH_SPINE_MIGRATION,
    ]
    assert migrate.init_db(path) == []

    with engine.read_conn(path) as conn:
        policies = {
            int(row["account_id"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM account_statement_policies ORDER BY account_id"
            )
        }
        expectations = conn.execute(
            "SELECT * FROM account_statement_expectations ORDER BY id"
        ).fetchall()
        links = conn.execute(
            """SELECT expectation_id, source_document_id, status
               FROM statement_expectation_documents ORDER BY source_document_id"""
        ).fetchall()
        audit_rows = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM statement_expectation_audit
                   WHERE expectation_id IS NOT NULL
                   ORDER BY id"""
            )
        ]
        invalid_review = conn.execute(
            """SELECT period_month, review_state
               FROM statement_reviews WHERE source_document_id=?""",
            (ids["invalid_period"],),
        ).fetchone()
        invalid_line_period = conn.execute(
            """SELECT statement_period FROM statement_lines
               WHERE source_document_id=?""",
            (ids["invalid_period"],),
        ).fetchone()["statement_period"]

    assert set(policies) == {
        ids["cash_account"],
        ids["card_account"],
        ids["savings_account"],
    }
    cash = policies[ids["cash_account"]]
    assert (
        cash["effective_from_month"],
        cash["configuration_state"],
        cash["requirement_mode"],
        cash["cadence"],
        cash["anchor_month"],
        cash["active_from"],
        cash["active_to"],
    ) == ("0001-01", "configured", "no_statement", "none", None, None, None)
    for account_id in (ids["card_account"], ids["savings_account"]):
        policy = policies[account_id]
        assert (
            policy["configuration_state"],
            policy["requirement_mode"],
            policy["cadence"],
            policy["anchor_month"],
            policy["active_from"],
            policy["active_to"],
        ) == ("unconfigured", None, None, None, None, None)

    assert len(expectations) == 1
    expectation = expectations[0]
    assert (
        expectation["account_id"],
        expectation["period_month"],
        expectation["origin"],
        expectation["requirement_state"],
        expectation["lifecycle_state"],
    ) == (
        ids["card_account"],
        "2026-06",
        "legacy_document",
        "required",
        "received",
    )
    assert [
        (row["source_document_id"], row["status"]) for row in links
    ] == [
        (ids["exact_a"], "active"),
        (ids["exact_b"], "active"),
    ]
    assert [
        (
            row["event_kind"],
            row["source_document_id"],
            row["old_lifecycle_state"],
            row["new_lifecycle_state"],
        )
        for row in audit_rows
    ] == [
        ("expectation_prepared", None, None, "expected"),
        ("document_attached", ids["exact_a"], "expected", "expected"),
        ("document_attached", ids["exact_b"], "expected", "expected"),
        ("lifecycle_transition", None, "expected", "received"),
    ]
    assert tuple(invalid_review) == (None, "legacy_unverified")
    assert invalid_line_period == "2026-13"
    with engine.read_conn(path) as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_expectation_audit
               WHERE event_kind='policy_recorded'"""
        ).fetchone()[0] == 3
    _assert_integrity(path)


def test_pre_migration_copy_upgrades_to_same_truth_and_preserves_identity(tmp_path):
    source = tmp_path / "source.sqlite"
    copied = tmp_path / "copied.sqlite"
    _apply_pre_030(source)
    _insert_legacy_evidence(source)
    shutil.copy2(source, copied)

    with sqlite3.connect(source) as conn:
        source_identity_before = migrate.read_database_identity(conn)
    with sqlite3.connect(copied) as conn:
        copied_identity_before = migrate.read_database_identity(conn)
    assert source_identity_before == copied_identity_before

    assert migrate.init_db(source) == [
        MIGRATION,
        REVIEW_MIGRATION,
        CAPTURE_MIGRATION,
        STRUCTURED_IMPORT_MIGRATION,
        POSITIVE_FLOW_MIGRATION,
        MERCHANT_RESOLUTION_MIGRATION,
        PERIOD_POLICY_MIGRATION,
        IMPORT_RUNS_MIGRATION,
        CARD_HOLDERS_MIGRATION,
        MONTH_SPINE_MIGRATION,
    ]
    assert migrate.init_db(copied) == [
        MIGRATION,
        REVIEW_MIGRATION,
        CAPTURE_MIGRATION,
        STRUCTURED_IMPORT_MIGRATION,
        POSITIVE_FLOW_MIGRATION,
        MERCHANT_RESOLUTION_MIGRATION,
        PERIOD_POLICY_MIGRATION,
        IMPORT_RUNS_MIGRATION,
        CARD_HOLDERS_MIGRATION,
        MONTH_SPINE_MIGRATION,
    ]

    def semantic_snapshot(path: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
        with engine.read_conn(path) as conn:
            policies = [
                tuple(row)
                for row in conn.execute(
                    """SELECT account_id, effective_from_month, configuration_state,
                              requirement_mode, cadence, anchor_month,
                              active_from, active_to, created_by, reason
                       FROM account_statement_policies ORDER BY id"""
                )
            ]
            expectations = [
                tuple(row)
                for row in conn.execute(
                    """SELECT account_id, period_month, origin,
                              requirement_state, lifecycle_state
                       FROM account_statement_expectations ORDER BY id"""
                )
            ]
            links = [
                tuple(row)
                for row in conn.execute(
                    """SELECT expectation_id, source_document_id, status
                       FROM statement_expectation_documents ORDER BY id"""
                )
            ]
        return policies, expectations, links

    assert semantic_snapshot(source) == semantic_snapshot(copied)
    with engine.read_conn(source) as conn:
        source_identity_after = migrate.read_database_identity(conn)
    with engine.read_conn(copied) as conn:
        copied_identity_after = migrate.read_database_identity(conn)
    assert source_identity_after == copied_identity_after == source_identity_before
    _assert_integrity(source)
    _assert_integrity(copied)


@pytest.mark.parametrize(
    (
        "effective_from_month",
        "configuration_state",
        "requirement_mode",
        "cadence",
        "anchor_month",
        "active_from",
        "active_to",
    ),
    (
        ("2026-13", "configured", "required", "monthly", None, None, None),
        ("0000-01", "configured", "required", "monthly", None, None, None),
        ("2026-01", "configured", None, "monthly", None, None, None),
        ("2026-01", "configured", "required", None, None, None, None),
        ("2026-01", "configured", "required", "none", None, None, None),
        ("2026-01", "configured", "required", "monthly", 1, None, None),
        ("2026-01", "configured", "no_statement", "monthly", None, None, None),
        ("2026-01", "unconfigured", "required", "monthly", None, None, None),
        ("2026-01", "configured", "required", "quarterly", None, None, None),
        ("2026-01", "configured", "required", "annual", 13, None, None),
        ("2026-01", "configured", "required", "monthly", None, "2026-13-01", None),
        ("2026-01", "configured", "required", "monthly", None, "2026-02-30", None),
        ("2026-01", "configured", "required", "monthly", None, "2026-03-01", "2026-02-01"),
    ),
)
def test_policy_constraints_reject_impossible_or_malformed_versions(
    tmp_path,
    effective_from_month,
    configuration_state,
    requirement_mode,
    cadence,
    anchor_month,
    active_from,
    active_to,
):
    path = tmp_path / "policy-guards.sqlite"
    migrate.init_db(path)
    with engine.write_tx(path) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('Synthetic Card', 'Synthetic', 'credit', 'CAD')"""
            ).lastrowid
        )
    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(path) as conn:
            _insert_policy(
                conn,
                account_id=account_id,
                effective_from_month=effective_from_month,
                configuration_state=configuration_state,
                requirement_mode=requirement_mode,
                cadence=cadence,
                anchor_month=anchor_month,
                active_from=active_from,
                active_to=active_to,
            )


def test_expectation_checks_reject_null_lifecycle_or_waiver_actor(tmp_path):
    path = tmp_path / "expectation-null-guards.sqlite"
    migrate.init_db(path)

    with engine.write_tx(path) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('Synthetic Card', 'Synthetic', 'credit', 'CAD')"""
            ).lastrowid
        )
        policy_id = _insert_policy(conn, account_id=account_id)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO account_statement_expectations(
                     account_id, period_month, policy_id, origin,
                     requirement_state, lifecycle_state, created_by, reason
                   )
                   VALUES (?, '2026-06', ?, 'policy',
                           'required', NULL, 'test:prepare', 'missing lifecycle')""",
                (account_id, policy_id),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO account_statement_expectations(
                     account_id, period_month, policy_id, origin,
                     requirement_state, lifecycle_state,
                     waived_at, waived_by, waiver_reason, created_by, reason
                   )
                   VALUES (?, '2026-07', ?, 'policy',
                           'waived', NULL,
                           CURRENT_TIMESTAMP, NULL, 'synthetic waiver',
                           'test:prepare', 'missing waiver actor')""",
                (account_id, policy_id),
            )


def test_append_only_uniqueness_and_evidence_identity_guards(tmp_path):
    path = tmp_path / "guards.sqlite"
    migrate.init_db(path)

    with engine.write_tx(path) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('Synthetic Card', 'Synthetic', 'credit', 'CAD')"""
            ).lastrowid
        )
        policy_id = _insert_policy(conn, account_id=account_id)
        expectation_id = _insert_expectation(
            conn,
            account_id=account_id,
            policy_id=policy_id,
            period_month="2026-06",
        )
        second_expectation_id = _insert_expectation(
            conn,
            account_id=account_id,
            policy_id=policy_id,
            period_month="2026-07",
        )
        _insert_statement_document(
            conn, doc_id=201, account_id=account_id, period_month="2026-06"
        )
        _insert_statement_document(
            conn, doc_id=202, account_id=account_id, period_month="2026-06"
        )
        first_link_id = _insert_link(
            conn, expectation_id=expectation_id, source_document_id=201
        )
        second_link_id = _insert_link(
            conn, expectation_id=expectation_id, source_document_id=202
        )

    # One expectation accepts multiple source documents.
    with engine.read_conn(path) as conn:
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_expectation_documents
               WHERE expectation_id=? AND status='active'""",
            (expectation_id,),
        ).fetchone()[0] == 2

    # One source document cannot be active on two expectations.
    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(path) as conn:
            _insert_link(
                conn,
                expectation_id=second_expectation_id,
                source_document_id=201,
            )

    guarded_mutations = (
        ("UPDATE account_statement_policies SET reason='changed' WHERE id=?", (policy_id,)),
        ("DELETE FROM account_statement_policies WHERE id=?", (policy_id,)),
        (
            "UPDATE statement_expectation_audit SET reason='changed' WHERE policy_id=?",
            (policy_id,),
        ),
        (
            "DELETE FROM statement_expectation_audit WHERE policy_id=?",
            (policy_id,),
        ),
        (
            "DELETE FROM account_statement_expectations WHERE id=?",
            (expectation_id,),
        ),
        (
            "DELETE FROM statement_expectation_documents WHERE id=?",
            (first_link_id,),
        ),
        (
            "UPDATE statement_expectation_documents SET attach_reason='changed' WHERE id=?",
            (first_link_id,),
        ),
        (
            "UPDATE account_statement_expectations SET lifecycle_state='received' WHERE id=?",
            (expectation_id,),
        ),
    )
    for sql, args in guarded_mutations:
        with pytest.raises(sqlite3.IntegrityError):
            with engine.write_tx(path) as conn:
                conn.execute(sql, args)

    # Active evidence cannot disappear or stop being a statement.
    for sql in (
        "DELETE FROM source_documents WHERE id=201",
        "UPDATE source_documents SET kind='receipt' WHERE id=201",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with engine.write_tx(path) as conn:
                conn.execute(sql)

    # A malformed audit cannot authorize an illegal lifecycle skip.
    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(path) as conn:
            _audited_transition(
                conn,
                expectation_id,
                operation_key="test:illegal-skip",
                new_requirement_state="required",
                new_lifecycle_state="reviewed",
            )

    # The legal forward path is audited.  Nonterminal rows still block clean reconciliation.
    with engine.write_tx(path) as conn:
        _audited_transition(
            conn,
            expectation_id,
            operation_key="test:received",
            new_requirement_state="required",
            new_lifecycle_state="received",
        )
        _audited_transition(
            conn,
            expectation_id,
            operation_key="test:reviewed",
            new_requirement_state="required",
            new_lifecycle_state="reviewed",
        )

    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(path) as conn:
            _audited_transition(
                conn,
                expectation_id,
                operation_key="test:premature-reconciled",
                new_requirement_state="required",
                new_lifecycle_state="reconciled",
            )

    with engine.write_tx(path) as conn:
        conn.execute(
            """UPDATE statement_lines
               SET match_status='matched'
               WHERE source_document_id IN (201, 202)"""
        )
        _audited_transition(
            conn,
            expectation_id,
            operation_key="test:reconciled",
            new_requirement_state="required",
            new_lifecycle_state="reconciled",
        )

    # Reviewed/reconciled evidence identity is frozen; non-identity metadata remains mutable.
    for assignment in (
        "storage_ref='mutated/path.pdf'",
        "sha256='mutated-sha'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with engine.write_tx(path) as conn:
                conn.execute(
                    f"UPDATE source_documents SET {assignment} WHERE id=201"
                )
    with engine.write_tx(path) as conn:
        conn.execute(
            """UPDATE source_documents
               SET original_name='renamed.pdf', status='matched'
               WHERE id=201"""
        )

    # Detach is the sole link transition, is audited once, and releases deletion.
    with engine.write_tx(path) as conn:
        conn.execute(
            """UPDATE statement_expectation_documents
               SET status='detached', detached_at=CURRENT_TIMESTAMP,
                   detached_by='test:detach', detach_reason='synthetic removal'
               WHERE id=?""",
            (second_link_id,),
        )
    with engine.read_conn(path) as conn:
        detach_audit = conn.execute(
            """SELECT * FROM statement_expectation_audit
               WHERE event_kind='document_detached'
                 AND source_document_id=202"""
        ).fetchall()
    assert len(detach_audit) == 1
    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(path) as conn:
            conn.execute(
                """UPDATE statement_expectation_documents
                   SET detach_reason='rewritten'
                   WHERE id=?""",
                (second_link_id,),
            )
    with engine.write_tx(path) as conn:
        conn.execute("DELETE FROM statement_lines WHERE source_document_id=202")
        conn.execute("DELETE FROM source_documents WHERE id=202")
    with engine.read_conn(path) as conn:
        detached = conn.execute(
            "SELECT * FROM statement_expectation_documents WHERE id=?",
            (second_link_id,),
        ).fetchone()
        assert detached["status"] == "detached"
        assert detached["source_document_id"] is None

    _assert_integrity(path)


def test_receipt_or_nonrequired_expectation_cannot_receive_statement_link(tmp_path):
    path = tmp_path / "link-guards.sqlite"
    migrate.init_db(path)
    with engine.write_tx(path) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name, institution, kind, currency)
                   VALUES ('Synthetic Card', 'Synthetic', 'credit', 'CAD')"""
            ).lastrowid
        )
        policy_id = _insert_policy(conn, account_id=account_id)
        exempt_id = _insert_expectation(
            conn,
            account_id=account_id,
            policy_id=policy_id,
            period_month="2026-06",
            requirement_state="exempt",
            lifecycle_state=None,
        )
        conn.execute(
            """INSERT INTO source_documents(
                 id, kind, original_name, storage_ref, sha256, mime_type, status
               )
               VALUES (
                 301, 'receipt', 'receipt.jpg', 'synthetic/receipt.jpg',
                 'synthetic-receipt-sha', 'image/jpeg', 'processed'
               )"""
        )

    with pytest.raises(sqlite3.IntegrityError):
        with engine.write_tx(path) as conn:
            _insert_link(
                conn, expectation_id=exempt_id, source_document_id=301
            )
