from __future__ import annotations

import sqlite3

from app.close import checklist
from app.db import engine, migrate


def test_flow_migration_does_not_guess_foreign_currency_receipts(tmp_path):
    path = tmp_path / "pre-flow-currency.sqlite"
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
            """INSERT INTO accounts(
                 id, name, institution, kind, currency
               ) VALUES
                 (1, 'Home Account', 'Synthetic Bank', 'chequing', 'CAD'),
                 (2, 'Foreign Account', 'Synthetic Bank', 'credit', 'USD')"""
        )
        category_id = int(
            conn.execute(
                """INSERT INTO categories(name, kind, brand_owner)
                   VALUES ('Legacy Purchases', 'expense', 'shared')"""
            ).lastrowid
        )
        for transaction_id in (1, 2):
            conn.execute(
                """INSERT INTO transaction_splits(
                     transaction_id, category_id, amount_cents
                   ) VALUES (?, ?, -2500)""",
                (transaction_id, category_id),
            )
        conn.execute(
            """INSERT INTO transactions(
                 account_id, posted_on, description, amount_cents,
                 source, external_id
               ) VALUES
                 (1, '2026-06-10', 'home receipt', -2500, 'receipt', 'home'),
                 (2, '2026-06-10', 'foreign receipt', -2500, 'receipt', 'foreign')"""
        )
        conn.commit()
    finally:
        conn.close()

    applied = migrate.init_db(path)
    assert applied[:6] == [
        "028_flow_semantics.sql",
        "029_capture_submissions.sql",
        "030_statement_expectations.sql",
        "031_statement_review.sql",
        "032_capture_telemetry.sql",
        "033_structured_statement_imports.sql",
    ]
    with engine.read_conn(path) as conn:
        rows = {
            row["external_id"]: row["flow_kind"]
            for row in conn.execute(
                "SELECT external_id, flow_kind FROM transactions ORDER BY id"
            )
        }
        pending = {
            row["external_id"]
            for row in conn.execute(
                """SELECT txn.external_id
                   FROM transaction_flow_reviews review
                   JOIN transactions txn ON txn.id=review.transaction_id
                   WHERE review.status='pending'"""
            )
        }
        audit = {
            row["external_id"]
            for row in conn.execute(
                """SELECT txn.external_id
                   FROM transaction_flow_audit audit
                   JOIN transactions txn ON txn.id=audit.transaction_id"""
            )
        }
        cashflow = conn.execute(
            "SELECT * FROM v_cashflow_monthly WHERE month='2026-06'"
        ).fetchone()
        status = {
            row["flow_kind"]: row["semantic_status"]
            for row in conn.execute(
                """SELECT flow_kind, semantic_status
                   FROM v_transaction_flow_status
                   ORDER BY transaction_id"""
            )
        }
        close_state = checklist.build_checklist(conn, "2026-06")

    assert rows == {"home": "purchase", "foreign": "unknown"}
    assert pending == {"foreign"}
    assert audit == {"home"}
    assert cashflow["expense_cents"] == 2500
    assert status == {"purchase": "complete", "unknown": "unknown"}
    assert close_state["flow_review_count"] == 1
    assert close_state["has_blocking_flow_reviews"] is True
