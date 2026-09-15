"""FN-106 — per-account balance assertions: migration, capture, and reconciliation."""
from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import (
    engine,
    migrate,
    repo_assertions,
    repo_documents,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
)
from app.ingest.schemas import ExtractedStatement, StatementRow
from app.reconcile import assertions
from app.web.routes.review import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _make_account(conn, *, name="Test Acct", kind="chequing") -> int:
    cur = conn.execute(
        "INSERT INTO accounts(name, institution, kind, currency) VALUES (?,?,?,'CAD')",
        (name, "Bank", kind),
    )
    return int(cur.lastrowid)


def _make_category(conn, *, name="Groceries", kind="expense") -> int:
    cur = conn.execute(
        "INSERT INTO categories(name, kind) VALUES (?,?)", (name, kind)
    )
    return int(cur.lastrowid)


def _add_txn(conn, *, account_id, category_id, posted_on, amount_cents) -> int:
    """Insert a transaction plus its single correctly-signed split."""
    cur = conn.execute(
        """INSERT INTO transactions(account_id, posted_on, description, amount_cents, source, external_id)
           VALUES (?,?,?,?,'test',?)""",
        (account_id, posted_on, "seed", amount_cents, uuid.uuid4().hex),
    )
    txn_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO transaction_splits(transaction_id, category_id, amount_cents) VALUES (?,?,?)",
        (txn_id, category_id, amount_cents),
    )
    return txn_id


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_migration_creates_table_and_is_idempotent(tmp_path):
    path = tmp_path / "assert.sqlite"
    applied = migrate.init_db(str(path))
    assert "027_balance_assertions.sql" in applied
    assert migrate.init_db(str(path)) == []  # re-run applies nothing

    with engine.read_conn(str(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "account_balance_assertions" in tables


def test_record_assertion_upserts_on_account_and_date(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn)
        first = repo_assertions.record_assertion(
            conn, account_id=acct, asof_date="2026-06-30", asserted_cents=8750,
            statement_period="2026-06",
        )
        # Same (account, asof_date) refreshes in place rather than duplicating.
        second = repo_assertions.record_assertion(
            conn, account_id=acct, asof_date="2026-06-30", asserted_cents=9000,
            statement_period="2026-06",
        )
    assert first == second
    with engine.read_conn(empty_db) as conn:
        rows = repo_assertions.list_assertions(conn, account_id=acct)
    assert len(rows) == 1
    assert rows[0]["asserted_cents"] == 9000


# ---------------------------------------------------------------------------
# Ledger balance + tie / over / under (both sign conventions)
# ---------------------------------------------------------------------------

def test_ledger_balance_sums_splits_through_date_only(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn)
        cat = _make_category(conn)
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-06-05", amount_cents=-1000)
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-06-30", amount_cents=-500)
        # After the asof date; must NOT be counted.
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-07-02", amount_cents=-9999)
    with engine.read_conn(empty_db) as conn:
        assert assertions.ledger_balance_cents(conn, acct, "2026-06-30") == -1500


def test_tie_produces_no_exception(empty_db):
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn)
        cat = _make_category(conn)
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-06-10", amount_cents=-1500)
        repo_assertions.record_assertion(
            conn, account_id=acct, asof_date="2026-06-30", asserted_cents=-1500,
        )
    with engine.read_conn(empty_db) as conn:
        row = repo_assertions.latest_for_account(conn, acct)
        check = assertions.check_assertion(conn, row)
        exceptions = assertions.scan_assertion_exceptions(conn, month="2026-06")
    assert check.status == "tie"
    assert check.delta_cents == 0
    assert check.is_exception is False
    assert exceptions == []


def test_over_and_under_asset_account(empty_db):
    """Asset account (positive-leaning balances)."""
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn, kind="chequing")
        cat = _make_category(conn, name="Salary", kind="income")
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-06-10", amount_cents=10000)

        # Ledger 10000; assert 9000 -> ledger records MORE -> over, delta +1000.
        repo_assertions.record_assertion(
            conn, account_id=acct, asof_date="2026-06-30", asserted_cents=9000,
        )
    with engine.read_conn(empty_db) as conn:
        check = assertions.check_assertion(conn, repo_assertions.latest_for_account(conn, acct))
    assert check.status == "over"
    assert check.delta_cents == 1000
    assert check.severity_cents == 1000
    assert check.reason_code == "balance_assertion_over"
    assert check.asof_date == "2026-06-30"

    with engine.write_tx(empty_db) as conn:
        # Now assert 11000 -> ledger records LESS -> under, delta -1000.
        repo_assertions.record_assertion(
            conn, account_id=acct, asof_date="2026-06-30", asserted_cents=11000,
        )
    with engine.read_conn(empty_db) as conn:
        check = assertions.check_assertion(conn, repo_assertions.latest_for_account(conn, acct))
    assert check.status == "under"
    assert check.delta_cents == -1000
    assert check.severity_cents == 1000
    assert check.reason_code == "balance_assertion_under"


def test_over_and_under_credit_account_negative_balances(empty_db):
    """Credit-card liability: expenses negative, so the ledger balance is negative."""
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn, kind="credit")
        cat = _make_category(conn, name="Dining", kind="expense")
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-06-10", amount_cents=-5000)

        # Ledger -5000; assert -6000 -> ledger records MORE (less negative) -> over, +1000.
        repo_assertions.record_assertion(
            conn, account_id=acct, asof_date="2026-06-30", asserted_cents=-6000,
        )
    with engine.read_conn(empty_db) as conn:
        check = assertions.check_assertion(conn, repo_assertions.latest_for_account(conn, acct))
    assert check.status == "over"
    assert check.delta_cents == 1000

    with engine.write_tx(empty_db) as conn:
        # Assert -4000 -> ledger records LESS (more negative) -> under, -1000.
        repo_assertions.record_assertion(
            conn, account_id=acct, asof_date="2026-06-30", asserted_cents=-4000,
        )
    with engine.read_conn(empty_db) as conn:
        check = assertions.check_assertion(conn, repo_assertions.latest_for_account(conn, acct))
    assert check.status == "under"
    assert check.delta_cents == -1000


def test_scan_returns_exceptions_worst_first_and_filters(empty_db):
    with engine.write_tx(empty_db) as conn:
        a1 = _make_account(conn, name="Chequing")
        a2 = _make_account(conn, name="Credit")
        cat = _make_category(conn)
        _add_txn(conn, account_id=a1, category_id=cat, posted_on="2026-06-10", amount_cents=-1000)
        _add_txn(conn, account_id=a2, category_id=cat, posted_on="2026-06-10", amount_cents=-2000)
        # a1 off by 500, a2 off by 5000.
        repo_assertions.record_assertion(conn, account_id=a1, asof_date="2026-06-30", asserted_cents=-1500)
        repo_assertions.record_assertion(conn, account_id=a2, asof_date="2026-06-30", asserted_cents=3000)
        # A different month; must not appear under month='2026-06'.
        _add_txn(conn, account_id=a1, category_id=cat, posted_on="2026-05-10", amount_cents=-100)
        repo_assertions.record_assertion(conn, account_id=a1, asof_date="2026-05-31", asserted_cents=-999)
    with engine.read_conn(empty_db) as conn:
        june = assertions.scan_assertion_exceptions(conn, month="2026-06")
        just_a1 = assertions.scan_assertion_exceptions(conn, account_id=a1)
    assert [c.account_id for c in june] == [a2, a1]  # worst (5000) first
    assert all(c.asof_date.startswith("2026-06") for c in june)
    assert {c.account_id for c in just_a1} == {a1}  # both months for a1


# ---------------------------------------------------------------------------
# Capture on statement approval + auto-processing
# ---------------------------------------------------------------------------

def _seed_statement(app_env, *, closing_balance_cents=8750, rows=True):
    field_confidence = {
        field: 0.96
        for field in (
            "period_start_on",
            "period_end_on",
            "statement_issued_on",
            "opening_balance_cents",
            "closing_balance_cents",
            "currency",
            "account_fingerprint",
            "zero_activity",
        )
    }
    parsed = ExtractedStatement(
        institution="North Bank",
        account_hint="Everyday",
        account_last4="9003",
        currency="CAD",
        statement_period="2026-06",
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=10000,
        closing_balance_cents=closing_balance_cents,
        declared_page_count=1,
        declared_row_count=2 if rows else 0,
        field_confidence=field_confidence,
        field_pages={field: 1 for field in field_confidence},
        rows=(
            [
                StatementRow(
                    posted_on="2026-06-10",
                    description="Corner Store",
                    amount_cents=-1250,
                    page_number=1,
                    field_confidence={
                        "posted_on": 0.96,
                        "description": 0.96,
                        "amount_cents": 0.96,
                    },
                ),
                StatementRow(
                    posted_on="2026-06-20",
                    description="Cafe",
                    amount_cents=-500,
                    page_number=1,
                    field_confidence={
                        "posted_on": 0.96,
                        "description": 0.96,
                        "amount_cents": 0.96,
                    },
                ),
            ]
            if rows
            else []
        ),
        confidence=0.96,
        observed_page_count=1,
        extracted_page_count=1,
    )
    token = uuid.uuid4().hex[:12]
    raw = f"synthetic balance statement {token}".encode()
    with engine.write_tx(app_env) as conn:
        cur = conn.execute(
            """INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
               VALUES ('statement',?,?,?,'application/pdf','needs_review')""",
            (
                f"june-{token}.pdf",
                f"inbox/june-{token}.pdf",
                hashlib.sha256(raw).hexdigest(),
            ),
        )
        doc_id = int(cur.lastrowid)
        extraction_id = repo_documents.insert_extraction(
            conn, source_document_id=doc_id, doc_kind="statement",
            extracted_json=parsed.model_dump_json(), confidence=parsed.confidence,
            external_id="", proposed_account_id=None, proposed_category_id=None,
            review_status="pending",
        )
        envelope = repo_statement_reviews.create_from_extraction(
            conn,
            source_document_id=doc_id,
            extraction_id=extraction_id,
            account_id=None,
            parsed=parsed,
            raw=raw,
            actor="test:extract",
        )
        repo_statements.stage_lines(
            conn,
            source_document_id=doc_id,
            account_id=None,
            parsed=parsed,
            row_anchor_ids=repo_statement_reviews.row_anchor_ids(
                parsed, envelope.page_anchor_ids
            ),
        )
        repo_statement_reviews.record_row_evidence(
            conn,
            review_id=int(envelope.review["id"]),
            extraction_id=extraction_id,
            parsed=parsed,
            account_id=None,
            anchors=envelope.page_anchor_ids,
        )
    return doc_id, extraction_id


def _approve_seeded_statement(
    app_env, doc_id: int, extraction_id: int, *, override_reason: str = ""
):
    client = _client()
    assigned = client.post(
        f"/review/{extraction_id}/approve-statement",
        data={"account_id": "1"},
        follow_redirects=False,
    )
    assert assigned.status_code == 303
    with engine.read_conn(app_env) as conn:
        review = repo_statement_reviews.get_for_document(conn, doc_id)
        assert review is not None
        revision = int(review["revision"])
    return client.post(
        f"/review/statement/{doc_id}/approve",
        data={
            "expected_revision": str(revision),
            "reason": "source and rows verified",
            "override_reason": override_reason,
        },
        follow_redirects=False,
    )


def test_approve_statement_records_assertion(app_env):
    doc_id, extraction_id = _seed_statement(app_env, closing_balance_cents=8250)

    r = _approve_seeded_statement(app_env, doc_id, extraction_id)
    assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        rows = repo_assertions.list_assertions(conn, account_id=1)
    matches = [row for row in rows if row["source_document_id"] == doc_id]
    assert len(matches) == 1
    assertion = matches[0]
    assert assertion["asserted_cents"] == 8250
    assert assertion["asof_date"] == "2026-06-30"
    assert assertion["statement_period"] == "2026-06"


def test_approve_statement_without_closing_balance_captures_nothing(app_env):
    doc_id, extraction_id = _seed_statement(app_env, closing_balance_cents=None)

    r = _approve_seeded_statement(
        app_env,
        doc_id,
        extraction_id,
        override_reason="closing balance was not printed; accept source limitation",
    )
    assert r.status_code == 303

    with engine.read_conn(app_env) as conn:
        rows = [row for row in repo_assertions.list_assertions(conn) if row["source_document_id"] == doc_id]
    assert rows == []


def test_capture_helper_is_noop_without_actual_closing_date(empty_db):
    parsed = ExtractedStatement(
        statement_period="2026-06", closing_balance_cents=5000, rows=[]
    )
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn)
        result = assertions.capture_statement_assertion(
            conn, parsed=parsed, account_id=acct, source_document_id=None
        )
    assert result is None


def test_capture_normalizes_debt_style_closing_balance(empty_db):
    """A credit-card statement printing its closing balance debt-style (positive
    amount-owing) is normalized to the ledger's negative-for-liability sign, so a
    ledger that reconciles ties instead of firing a spurious ~2x exception."""
    parsed = ExtractedStatement(
        statement_period="2026-06",
        period_end_on="2026-06-30",
        opening_balance_cents=0,
        closing_balance_cents=5000,  # "Balance Owing: $50.00", debt-style
        rows=[StatementRow(posted_on="2026-06-10", description="Dining", amount_cents=-5000)],
    )
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn, kind="credit")
        cat = _make_category(conn, name="Dining", kind="expense")
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-06-10", amount_cents=-5000)
        assertions.capture_statement_assertion(
            conn, parsed=parsed, account_id=acct, source_document_id=None
        )
    with engine.read_conn(empty_db) as conn:
        row = repo_assertions.latest_for_account(conn, acct)
        check = assertions.check_assertion(conn, row)
    assert row["asserted_cents"] == -5000  # stored in canonical ledger sign
    assert check.status == "tie"
    assert check.delta_cents == 0


def test_capture_asof_uses_printed_closing_date_even_with_pending_rows(empty_db):
    """The assertion uses the printed close, never any transaction date."""
    parsed = ExtractedStatement(
        statement_period="2026-06",
        period_end_on="2026-06-30",
        closing_balance_cents=-5000,
        rows=[
            StatementRow(posted_on="2026-06-10", description="Dining", amount_cents=-5000),
            StatementRow(posted_on="2026-06-29", description="Pending Auth", amount_cents=-2000, is_pending=True),
        ],
    )
    with engine.write_tx(empty_db) as conn:
        acct = _make_account(conn, kind="credit")
        cat = _make_category(conn, name="Dining", kind="expense")
        _add_txn(conn, account_id=acct, category_id=cat, posted_on="2026-06-10", amount_cents=-5000)
        assertions.capture_statement_assertion(
            conn, parsed=parsed, account_id=acct, source_document_id=None
        )
    with engine.read_conn(empty_db) as conn:
        row = repo_assertions.latest_for_account(conn, acct)
        check = assertions.check_assertion(conn, row)
    assert row["asof_date"] == "2026-06-30"
    assert check.status == "tie"


def test_pipeline_autoprocess_captures_assertion(app_env, monkeypatch):
    """A cleanly auto-processed statement (last4 matches an account) captures its
    assertion in the pipeline, never touching /review."""
    from pathlib import Path

    from app.config import get_settings
    from app.ingest import pipeline

    field_confidence = {
        field: 0.97
        for field in (
            "period_start_on",
            "period_end_on",
            "statement_issued_on",
            "opening_balance_cents",
            "closing_balance_cents",
            "currency",
            "account_fingerprint",
            "zero_activity",
        )
    }
    parsed = ExtractedStatement(
        institution="North Bank",
        account_hint="Auto",
        account_last4="1111",
        currency="CAD",
        statement_period="2026-06",
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=0,
        closing_balance_cents=-1750,
        declared_page_count=1,
        declared_row_count=2,
        field_confidence=field_confidence,
        field_pages={field: 1 for field in field_confidence},
        rows=[
            StatementRow(
                posted_on="2026-06-10",
                description="Corner Store",
                amount_cents=-1250,
                page_number=1,
                field_confidence={
                    "posted_on": 0.97,
                    "description": 0.97,
                    "amount_cents": 0.97,
                },
            ),
            StatementRow(
                posted_on="2026-06-18",
                description="Cafe",
                amount_cents=-500,
                page_number=1,
                field_confidence={
                    "posted_on": 0.97,
                    "description": 0.97,
                    "amount_cents": 0.97,
                },
            ),
        ],
        confidence=0.97,
        observed_page_count=1,
        extracted_page_count=1,
    )
    monkeypatch.setattr(pipeline, "extract_statement", lambda llm, raw: parsed)
    monkeypatch.setattr(pipeline, "checksum_ok", lambda p: True)

    token = uuid.uuid4().hex[:12]
    raw = b"%PDF-1.4 fake"
    with engine.write_tx(app_env) as conn:
        # Give an account an external_ref ending in the statement's last4 so it auto-resolves.
        cur = conn.execute(
            "INSERT INTO accounts(name, institution, kind, currency, external_ref) VALUES ('Auto','Bank','credit','CAD','xxxx1111')"
        )
        acct = int(cur.lastrowid)
        repo_statement_expectations.record_policy(
            conn,
            account_id=acct,
            effective_from_month="2026-01",
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            actor="test:assertion",
            reason="auto-process fixture requires monthly statements",
        )
        cur = conn.execute(
            """INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status)
               VALUES ('statement',?,?,?,'application/pdf','staged')""",
            (
                f"auto-{token}.pdf",
                f"inbox/auto-{token}.pdf",
                hashlib.sha256(raw).hexdigest(),
            ),
        )
        doc_id = int(cur.lastrowid)
        storage_ref = f"inbox/auto-{token}.pdf"

    # The pipeline reads the blob before the (patched) extract; give it a real file.
    blob = Path(get_settings().data_dir) / storage_ref
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(raw)

    result = pipeline.process_document(app_env, doc_id, llm=object())
    assert result["status"] == "staged"

    with engine.read_conn(app_env) as conn:
        rows = [row for row in repo_assertions.list_assertions(conn, account_id=acct)]
    assert len(rows) == 1
    assert rows[0]["asserted_cents"] == -1750
    assert rows[0]["asof_date"] == "2026-06-30"
