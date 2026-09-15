from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.accounting.contract import (
    AccountingContractViolation,
    FlowKind,
    assert_contract_pack,
    assert_golden_month,
    load_contract_pack,
)
from app.accounting import flows
from app.api.service import record_transaction
from app.config import get_settings
from app.close import checklist
from app.db import (
    engine,
    repo_close,
    repo_documents,
    repo_ledger,
    repo_statement_expectations,
    repo_statements,
)
from app.ingest.pipeline import process_document
from app.ingest.schemas import ExtractedReceipt, ExtractedStatement, StatementRow
from app.ingest.storage import capture
from app.reconcile import apply as reconcile_apply
from app.reconcile import positive_flows
from app.reconcile.engine import reconcile_document
from app.web.routes.review import router as review_router


PACK_PATH = Path(__file__).parent / "fixtures" / "accounting" / "golden_month.json"


class _StaticStructured:
    def __init__(self, result):
        self.result = result

    def invoke(self, messages):
        return self.result


class _StaticLLM:
    def __init__(self, result):
        self.result = result

    def with_structured_output(self, schema, **kwargs):
        assert isinstance(self.result, schema)
        return _StaticStructured(self.result)


def _statement_pdf(tag: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    lines = [
        "MONTHLY STATEMENT",
        f"Account ending {tag}",
        "Statement period June 2026",
        "Opening balance 0.00",
        "Closing balance 0.00",
        "This is synthetic contract evidence only.",
    ]
    for index, line in enumerate(lines):
        page.insert_text((72, 72 + 18 * index), line, fontsize=11)
    return doc.tobytes()


def _review_client() -> TestClient:
    app = FastAPI()
    app.include_router(review_router)
    return TestClient(app)


def _insert_txn(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    amount_cents: int,
    source: str,
    external_id: str,
    description: str,
    flow_kind: FlowKind | str,
) -> int:
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=description,
        counterparty=description,
        amount_cents=amount_cents,
        source=source,
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind=flow_kind,
    )
    assert txn_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=amount_cents,
        memo=description,
    )
    return txn_id


def _insert_promoted_statement_txn(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    amount_cents: int,
    description: str,
    flow_kind: FlowKind | str,
) -> int:
    token = uuid.uuid4().hex
    doc_id = repo_documents.insert_source_document(
        conn,
        kind="statement",
        original_name=f"synthetic-{token}.pdf",
        storage_ref=f"contract/{token}.pdf",
        sha256=f"sha-{token}",
        mime_type="application/pdf",
        status="processed",
    )
    parsed = ExtractedStatement(
        institution="Contract Bank",
        account_last4="0000",
        currency="CAD",
        statement_period="2026-06",
        rows=[
            StatementRow(
                posted_on=posted_on,
                description=description,
                amount_cents=amount_cents,
            )
        ],
        confidence=1.0,
    )
    result = repo_statements.stage_lines(
        conn, source_document_id=doc_id, account_id=account_id, parsed=parsed
    )
    assert result == {"staged": 1, "duplicates": 0}
    line = repo_statements.lines_for_document(conn, doc_id)[0]
    effective_flow_kind = (
        FlowKind.UNKNOWN if amount_cents > 0 else flow_kind
    )
    repo_statements.set_flow_kind(
        conn, int(line["id"]), str(effective_flow_kind)
    )
    txn_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=description,
        counterparty="",
        amount_cents=amount_cents,
        source="statement",
        external_id=line["row_hash"],
        source_document_id=doc_id,
        source_confidence=1.0,
        flow_kind=effective_flow_kind,
    )
    assert txn_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=txn_id,
        category_id=category_id,
        amount_cents=amount_cents,
    )
    conn.execute(
        "UPDATE transactions SET recon_status='cleared', cleared_on=? WHERE id=?",
        (posted_on, txn_id),
    )
    repo_statements.set_match(
        conn,
        line["id"],
        status="promoted",
        transaction_id=txn_id,
        rationale="synthetic golden-month promotion",
    )
    repo_documents.set_status(conn, doc_id, "matched")
    return txn_id


@pytest.fixture
def golden_ledger(empty_db, tmp_path, monkeypatch, make_jpeg):
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "contract-data"))
    monkeypatch.setenv("TRIAGE_ENABLED", "false")
    get_settings.cache_clear()

    pack = load_contract_pack(PACK_PATH)
    with engine.write_tx(empty_db) as conn:
        accounts = {}
        for name, kind, external_ref in (
            ("Everyday Chequing", "chequing", "BANK:CHEQ:1111"),
            ("Everyday Card", "credit", "BANK:CARD:4242"),
            ("Cash Wallet", "cash", ""),
            ("Rainy Day Savings", "savings", "BANK:SAVE:2222"),
        ):
            accounts[name] = int(
                conn.execute(
                    """INSERT INTO accounts(name,institution,kind,currency,external_ref)
                       VALUES (?,'Contract Bank',?,'CAD',?)""",
                    (name, kind, external_ref),
                ).lastrowid
            )
        for name in (
            "Everyday Chequing",
            "Everyday Card",
            "Rainy Day Savings",
        ):
            repo_statement_expectations.record_policy(
                conn,
                account_id=accounts[name],
                effective_from_month="2026-01",
                configuration_state="configured",
                requirement_mode="required",
                cadence="monthly",
                actor="test:contract",
                reason="golden-month account requires monthly statements",
            )
        categories = {}
        for name, kind in (
            ("Groceries", "expense"),
            ("Salary", "income"),
            ("Transfers", "transfer"),
        ):
            categories[name] = int(
                conn.execute(
                    "INSERT INTO categories(name,kind,brand_owner) VALUES (?,?,'shared')",
                    (name, kind),
                ).lastrowid
            )

    mapping: dict[str, list[int]] = {}

    # Manual-entry seam: API service -> repository transaction + matching split.
    mapping["cash_purchase"] = [
        record_transaction(
            amount_cents=-1250,
            posted_on="2026-06-01",
            description="Synthetic market purchase",
            category="Groceries",
            account_id=accounts["Cash Wallet"],
            external_id="golden:cash-purchase",
            source="manual",
            flow_kind=FlowKind.PURCHASE,
        )["transaction_id"]
    ]
    mapping["salary_income"] = [
        record_transaction(
            amount_cents=300000,
            posted_on="2026-06-05",
            description="Synthetic payroll",
            category="Salary",
            account_id=accounts["Everyday Chequing"],
            external_id="golden:salary",
            source="manual",
            flow_kind=FlowKind.INCOME,
        )["transaction_id"]
    ]

    # Receipt-ingest seam: immutable image -> extraction -> provisional expense.
    receipt_cap = capture(
        raw=make_jpeg(color=(34, 125, 81)),
        original_name="synthetic-corner-cafe.jpg",
        channel="inbox",
    )
    receipt = ExtractedReceipt(
        merchant="Corner Cafe",
        purchased_on="2026-06-03",
        currency="CAD",
        subtotal_cents=4000,
        tax_cents=210,
        total_cents=4210,
        card_last4="4242",
        category_guess="Groceries",
        confidence=0.99,
    )
    receipt_result = process_document(
        empty_db, receipt_cap["source_document_id"], _StaticLLM(receipt)
    )
    assert receipt_result["status"] == "inserted"
    receipt_txn_id = int(receipt_result["transaction_id"])

    # Statement-import seam corroborates the receipt and must not book it twice.
    statement_cap = capture(
        raw=_statement_pdf("4242"),
        original_name="synthetic-card-statement.pdf",
        channel="inbox",
    )
    card_statement = ExtractedStatement(
        institution="Contract Bank",
        account_last4="4242",
        currency="CAD",
        statement_period="2026-06",
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=0,
        closing_balance_cents=-4210,
        declared_page_count=1,
        declared_row_count=1,
        field_confidence={
            field: 0.99
            for field in (
                "period_start_on",
                "period_end_on",
                "statement_issued_on",
                "opening_balance_cents",
                "closing_balance_cents",
                "currency",
                "account_fingerprint",
            )
        },
        field_pages={
            field: 1
            for field in (
                "period_start_on",
                "period_end_on",
                "statement_issued_on",
                "opening_balance_cents",
                "closing_balance_cents",
                "currency",
                "account_fingerprint",
            )
        },
        rows=[
            StatementRow(
                posted_on="2026-06-04",
                description="CORNER CAFE T042",
                amount_cents=-4210,
                page_number=1,
                field_confidence={
                    "posted_on": 0.99,
                    "description": 0.99,
                    "amount_cents": 0.99,
                },
            )
        ],
        confidence=0.99,
    )
    staged = process_document(
        empty_db, statement_cap["source_document_id"], _StaticLLM(card_statement)
    )
    assert staged["status"] == "staged"
    reconciled = reconcile_document(empty_db, statement_cap["source_document_id"], llm=None)
    assert reconciled["matched"] == 0
    assert reconciled["promoted"] == 0
    assert reconciled["needs_review"] == 1
    with engine.write_tx(empty_db) as conn:
        statement_line = repo_statements.lines_for_document(
            conn,
            statement_cap["source_document_id"],
        )[0]
        reconcile_apply.confirm_match(
            conn,
            int(statement_line["id"]),
            receipt_txn_id,
        )
    mapping["card_receipt"] = [receipt_txn_id]

    with engine.write_tx(empty_db) as conn:
        for case_id, account_name, amount, description, category_name in (
            ("partial_refund", "Everyday Card", 1000, "CORNER CAFE REFUND", "Groceries"),
            ("bank_fee", "Everyday Chequing", -500, "MONTHLY ACCOUNT FEE", "Groceries"),
            ("reversed_charge", "Everyday Card", -1200, "TEMPORARY CHARGE", "Groceries"),
            ("charge_reversal", "Everyday Card", 1200, "TEMPORARY CHARGE REVERSAL", "Groceries"),
        ):
            mapping[case_id] = [
                _insert_promoted_statement_txn(
                    conn,
                    account_id=accounts[account_name],
                    category_id=categories[category_name],
                    posted_on=next(
                        case.posted_on for case in pack.cases if case.id == case_id
                    ),
                    amount_cents=amount,
                    description=description,
                    flow_kind=next(
                        case.flow_kind for case in pack.cases if case.id == case_id
                    ),
                )
            ]

        mapping["savings_transfer"] = [
            _insert_txn(
                conn,
                account_id=accounts["Everyday Chequing"],
                category_id=categories["Transfers"],
                posted_on="2026-06-10",
                amount_cents=-50000,
                source="manual",
                external_id="golden:transfer:out",
                description="Transfer to savings",
                flow_kind=FlowKind.INTERNAL_TRANSFER,
            ),
            _insert_txn(
                conn,
                account_id=accounts["Rainy Day Savings"],
                category_id=categories["Transfers"],
                posted_on="2026-06-10",
                amount_cents=50000,
                source="manual",
                external_id="golden:transfer:in",
                description="Transfer from chequing",
                flow_kind=FlowKind.INTERNAL_TRANSFER,
            ),
        ]
        mapping["card_payment"] = [
            _insert_promoted_statement_txn(
                conn,
                account_id=accounts["Everyday Chequing"],
                category_id=categories["Transfers"],
                posted_on="2026-06-12",
                amount_cents=-75000,
                description="CARD PAYMENT OUT",
                flow_kind=FlowKind.CARD_PAYMENT,
            ),
            _insert_promoted_statement_txn(
                conn,
                account_id=accounts["Everyday Card"],
                category_id=categories["Transfers"],
                posted_on="2026-06-12",
                amount_cents=75000,
                description="PAYMENT RECEIVED",
                flow_kind=FlowKind.CARD_PAYMENT,
            ),
        ]
        mapping["reimbursed_expense"] = [
            _insert_txn(
                conn,
                account_id=accounts["Everyday Chequing"],
                category_id=categories["Groceries"],
                posted_on="2026-06-14",
                amount_cents=-2500,
                source="manual",
                external_id="golden:reimbursed-expense",
                description="Shared supplies",
                flow_kind=FlowKind.PURCHASE,
            )
        ]
        mapping["expense_reimbursement"] = [
            _insert_txn(
                conn,
                account_id=accounts["Everyday Chequing"],
                category_id=categories["Groceries"],
                posted_on="2026-06-15",
                amount_cents=2500,
                source="manual",
                external_id="golden:reimbursement",
                description="Shared supplies reimbursement",
                flow_kind=FlowKind.REIMBURSEMENT,
            )
        ]
        mapping["opening_balance"] = [
            _insert_txn(
                conn,
                account_id=accounts["Everyday Chequing"],
                category_id=categories["Transfers"],
                posted_on="2026-06-01",
                amount_cents=100000,
                source="opening",
                external_id="open:golden-chequing",
                description="Opening balance",
                flow_kind=FlowKind.OPENING,
            )
        ]
        mapping["reconciliation_adjustment"] = [
            _insert_txn(
                conn,
                account_id=accounts["Everyday Chequing"],
                category_id=categories["Transfers"],
                posted_on="2026-06-30",
                amount_cents=700,
                source="adjustment",
                external_id="golden:adjustment",
                description="Audited reconciliation adjustment",
                flow_kind=FlowKind.ADJUSTMENT,
            )
        ]

        def accept_positive_pair(
            *,
            subject_id: int,
            candidate_id: int,
            flow_kind: FlowKind,
            relationship_kind: flows.RelationshipKind,
            selected_category_id: int | None = None,
        ) -> None:
            review = next(
                item
                for item in positive_flows.list_positive_flow_reviews(
                    conn, "2026-06"
                )
                if int(item["subject"]["transaction_id"]) == subject_id
            )
            proposal = next(
                item
                for item in review["proposals"]
                if item["proposed_flow_kind"] == flow_kind.value
                and item["relationship_kind"] == relationship_kind.value
                and item["candidate_transaction_id"] == candidate_id
            )
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=subject_id,
                month="2026-06",
                proposal_key=proposal["proposal_key"],
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key=f"fixture:golden:{subject_id}:{flow_kind.value}",
                actor="fixture:golden",
                reason=f"synthetic {relationship_kind.value} provenance",
                selected_category_id=selected_category_id,
            )

        def expense_category_for(transaction_id: int) -> int:
            row = conn.execute(
                """
                SELECT split.category_id
                FROM transaction_splits split
                JOIN categories category ON category.id=split.category_id
                WHERE split.transaction_id=? AND category.kind='expense'
                ORDER BY split.id
                LIMIT 1
                """,
                (transaction_id,),
            ).fetchone()
            assert row is not None
            return int(row["category_id"])

        accept_positive_pair(
            subject_id=mapping["partial_refund"][0],
            candidate_id=mapping["card_receipt"][0],
            flow_kind=FlowKind.REFUND,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            selected_category_id=expense_category_for(
                mapping["card_receipt"][0]
            ),
        )
        accept_positive_pair(
            subject_id=mapping["charge_reversal"][0],
            candidate_id=mapping["reversed_charge"][0],
            flow_kind=FlowKind.REVERSAL,
            relationship_kind=flows.RelationshipKind.REVERSAL_OF,
            selected_category_id=expense_category_for(
                mapping["reversed_charge"][0]
            ),
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
            source_transaction_id=mapping["savings_transfer"][0],
            target_transaction_id=mapping["savings_transfer"][1],
            actor="fixture:golden",
            reason="synthetic savings transfer pair",
        )
        accept_positive_pair(
            subject_id=mapping["card_payment"][1],
            candidate_id=mapping["card_payment"][0],
            flow_kind=FlowKind.CARD_PAYMENT,
            relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REIMBURSEMENT_FOR,
            source_transaction_id=mapping["expense_reimbursement"][0],
            target_transaction_id=mapping["reimbursed_expense"][0],
            actor="fixture:golden",
            reason="synthetic reimbursement provenance",
        )

    # Edited-extraction seam: retain $20.00 source JSON, approve corrected $23.00.
    edited_cap = capture(
        raw=make_jpeg(color=(67, 89, 171)),
        original_name="synthetic-edited-receipt.jpg",
        channel="inbox",
    )
    original = ExtractedReceipt(
        merchant="Synthetic Books",
        purchased_on="2026-06-18",
        currency="CAD",
        total_cents=2000,
        category_guess="Groceries",
        confidence=0.2,
    )
    needs_review = process_document(
        empty_db, edited_cap["source_document_id"], _StaticLLM(original)
    )
    assert needs_review["status"] == "needs_review"
    extraction_id = int(needs_review["extraction_id"])
    response = _review_client().post(
        f"/review/{extraction_id}/approve",
        data={
            "merchant": "Synthetic Books",
            "purchased_on": "2026-06-18",
            "total": "23.00",
            "category_name": "Groceries",
            "account_id": str(accounts["Cash Wallet"]),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with engine.read_conn(empty_db) as conn:
        edited_extraction = conn.execute(
            "SELECT * FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        assert json.loads(edited_extraction["extracted_json"])["total_cents"] == 2000
        assert edited_extraction["review_status"] == "approved"
        mapping["edited_extraction"] = [int(edited_extraction["transaction_id"])]

    # Duplicate statement staging is one row and one promoted transaction.
    with engine.write_tx(empty_db) as conn:
        token = uuid.uuid4().hex
        duplicate_doc_id = repo_documents.insert_source_document(
            conn,
            kind="statement",
            original_name="synthetic-duplicate-statement.pdf",
            storage_ref=f"contract/{token}.pdf",
            sha256=f"sha-{token}",
            mime_type="application/pdf",
            status="processed",
        )
        duplicate_statement = ExtractedStatement(
            institution="Contract Bank",
            account_last4="1111",
            currency="CAD",
            statement_period="2026-06",
            rows=[
                StatementRow(
                    posted_on="2026-06-20",
                    description="SYNTHETIC CORNER SHOP",
                    amount_cents=-575,
                )
            ],
            confidence=1.0,
        )
        first = repo_statements.stage_lines(
            conn,
            source_document_id=duplicate_doc_id,
            account_id=accounts["Everyday Chequing"],
            parsed=duplicate_statement,
        )
        second = repo_statements.stage_lines(
            conn,
            source_document_id=duplicate_doc_id,
            account_id=accounts["Everyday Chequing"],
            parsed=duplicate_statement,
        )
        assert first == {"staged": 1, "duplicates": 0}
        assert second == {"staged": 0, "duplicates": 1}
        duplicate_line = repo_statements.lines_for_document(conn, duplicate_doc_id)[0]
        repo_statements.set_flow_kind(
            conn,
            int(duplicate_line["id"]),
            FlowKind.PURCHASE.value,
        )
    duplicate_result = reconcile_document(empty_db, duplicate_doc_id, llm=None)
    assert duplicate_result["promoted"] == 0
    assert duplicate_result["needs_review"] == 1
    with engine.write_tx(empty_db) as conn:
        duplicate_line = repo_statements.lines_for_document(conn, duplicate_doc_id)[0]
        transaction_id = reconcile_apply.promote_line(
            conn,
            int(duplicate_line["id"]),
        )
        assert transaction_id is not None
        mapping["duplicate_statement_import"] = [
            transaction_id
        ]

    yield {
        "db_path": empty_db,
        "pack": pack,
        "mapping": mapping,
        "accounts": accounts,
        "categories": categories,
    }
    get_settings.cache_clear()


def test_versioned_pack_covers_normalized_accounting_cases():
    pack = load_contract_pack(PACK_PATH)
    assert_contract_pack(pack)
    assert {case.id for case in pack.cases} >= {
        "cash_purchase",
        "card_receipt",
        "salary_income",
        "partial_refund",
        "bank_fee",
        "charge_reversal",
        "savings_transfer",
        "card_payment",
        "expense_reimbursement",
        "opening_balance",
        "reconciliation_adjustment",
        "edited_extraction",
        "duplicate_statement_import",
        "missing_expected_statement",
        "closed_period_write",
        "foreign_currency_receipt",
        "ambiguous_currency_statement",
    }


def test_missing_statement_and_closed_period_use_production_close_seams(empty_db):
    pack = load_contract_pack(PACK_PATH)
    missing = next(case for case in pack.cases if case.id == "missing_expected_statement")
    closed = next(case for case in pack.cases if case.id == "closed_period_write")
    assert missing.expected.close_impact == "blocking"
    assert closed.expected.close_impact == "blocking"

    with engine.write_tx(empty_db) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name,institution,kind,currency)
                   VALUES ('Contract Card','','credit','CAD')"""
            ).lastrowid
        )
        repo_statement_expectations.record_policy(
            conn,
            account_id=account_id,
            effective_from_month="2026-01",
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            actor="test:contract",
            reason="missing-statement contract requires statement evidence",
        )
        category_id = int(
            conn.execute(
                """INSERT INTO categories(name,kind,brand_owner)
                   VALUES ('Contract Expense','expense','shared')"""
            ).lastrowid
        )
        txn_id = _insert_txn(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-06-22",
            amount_cents=-100,
            source="manual",
            external_id="contract:closed-period",
            description="Closed-period mutation target",
            flow_kind=FlowKind.PURCHASE,
        )
        repo_close.mark_closed(conn, "2026-06")

    with engine.read_conn(empty_db) as conn:
        coverage = next(
            row
            for row in checklist.build_checklist(conn, "2026-05")["rows"]
            if row["key"] == "coverage"
        )
        assert coverage["complete"] is False
        assert coverage["detail"] == "no statement evidence uploaded"
        with pytest.raises(repo_close.MonthLockedError):
            repo_close.guard_transaction_write(conn, txn_id)


def test_golden_month_exercises_all_ingest_seams_without_double_count(golden_ledger):
    with engine.read_conn(golden_ledger["db_path"]) as conn:
        totals = assert_golden_month(
            conn, golden_ledger["pack"], golden_ledger["mapping"]
        )
    assert totals == {
        "income_cents": 300000,
        "spending_cents": 7835,
        "external_cash_cents": 292165,
        "transaction_count": 17,
        "close_blocker_count": 4,
    }


def test_sign_inversion_mutation_fails_contract(golden_ledger):
    txn_id = golden_ledger["mapping"]["cash_purchase"][0]
    with engine.write_tx(golden_ledger["db_path"]) as conn:
        conn.execute("UPDATE transactions SET amount_cents=1250 WHERE id=?", (txn_id,))
        conn.execute(
            "UPDATE transaction_splits SET amount_cents=1250 WHERE transaction_id=?",
            (txn_id,),
        )
    with engine.read_conn(golden_ledger["db_path"]) as conn:
        with pytest.raises(AccountingContractViolation, match="observed ledger legs"):
            assert_golden_month(conn, golden_ledger["pack"], golden_ledger["mapping"])


def test_double_booking_mutation_fails_contract(golden_ledger):
    with engine.write_tx(golden_ledger["db_path"]) as conn:
        _insert_txn(
            conn,
            account_id=golden_ledger["accounts"]["Everyday Card"],
            category_id=golden_ledger["categories"]["Groceries"],
            posted_on="2026-06-04",
            amount_cents=-4210,
            source="statement",
            external_id="mutation:double-booked-receipt",
            description="Duplicate card expense mutation",
            flow_kind=FlowKind.PURCHASE,
        )
    with engine.read_conn(golden_ledger["db_path"]) as conn:
        with pytest.raises(AccountingContractViolation, match="unassigned transaction"):
            assert_golden_month(conn, golden_ledger["pack"], golden_ledger["mapping"])


def test_currency_mutation_fails_contract(golden_ledger):
    with engine.write_tx(golden_ledger["db_path"]) as conn:
        conn.execute(
            "UPDATE accounts SET currency='USD' WHERE id=?",
            (golden_ledger["accounts"]["Everyday Card"],),
        )
    with engine.read_conn(golden_ledger["db_path"]) as conn:
        with pytest.raises(AccountingContractViolation, match="unsupported_currency"):
            assert_golden_month(conn, golden_ledger["pack"], golden_ledger["mapping"])


def test_foreign_receipt_and_ambiguous_statement_fail_closed(
    empty_db, tmp_path, monkeypatch, make_jpeg
):
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "currency-data"))
    monkeypatch.setenv("TRIAGE_ENABLED", "false")
    get_settings.cache_clear()
    with engine.write_tx(empty_db) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name,institution,kind,currency,external_ref)
                   VALUES ('Home Account','Contract Bank','chequing','CAD','BANK:1111')"""
            ).lastrowid
        )
        repo_statement_expectations.record_policy(
            conn,
            account_id=account_id,
            effective_from_month="2026-01",
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            actor="test:contract",
            reason="currency contract requires monthly statements",
        )
        conn.execute(
            "INSERT INTO categories(name,kind,brand_owner) VALUES ('Groceries','expense','shared')"
        )

    receipt_cap = capture(
        raw=make_jpeg(color=(155, 43, 64)),
        original_name="synthetic-usd-receipt.jpg",
        channel="inbox",
    )
    usd_receipt = ExtractedReceipt(
        merchant="Foreign Test Merchant",
        purchased_on="2026-06-24",
        currency="USD",
        total_cents=1500,
        category_guess="Groceries",
        confidence=0.99,
    )
    receipt_result = process_document(
        empty_db, receipt_cap["source_document_id"], _StaticLLM(usd_receipt)
    )
    assert receipt_result["status"] == "needs_review"
    assert receipt_result["reason"] == "unsupported_currency"

    ambiguous_receipt_cap = capture(
        raw=make_jpeg(color=(42, 73, 105)),
        original_name="synthetic-ambiguous-currency-receipt.jpg",
        channel="inbox",
    )
    ambiguous_receipt = ExtractedReceipt(
        merchant="Ambiguous Test Merchant",
        purchased_on="2026-06-24",
        currency="",
        total_cents=1600,
        category_guess="Groceries",
        confidence=0.99,
    )
    ambiguous_receipt_result = process_document(
        empty_db,
        ambiguous_receipt_cap["source_document_id"],
        _StaticLLM(ambiguous_receipt),
    )
    assert ambiguous_receipt_result["status"] == "needs_review"
    assert ambiguous_receipt_result["reason"] == "ambiguous_currency"

    statement_cap = capture(
        raw=_statement_pdf("1111"),
        original_name="synthetic-unknown-currency-statement.pdf",
        channel="inbox",
    )
    ambiguous_statement = ExtractedStatement(
        institution="Contract Bank",
        account_last4="1111",
        currency="",
        statement_period="2026-06",
        rows=[
            StatementRow(
                posted_on="2026-06-25",
                description="UNKNOWN CURRENCY PURCHASE",
                amount_cents=-2500,
            )
        ],
        confidence=0.99,
    )
    statement_result = process_document(
        empty_db,
        statement_cap["source_document_id"],
        _StaticLLM(ambiguous_statement),
    )
    assert statement_result["status"] == "needs_review"
    assert statement_result["reason"] == "ambiguous_currency"

    with engine.read_conn(empty_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE type='reconcile_document' AND source_document_id=?""",
            (statement_cap["source_document_id"],),
        ).fetchone()[0] == 0
        line = repo_statements.lines_for_document(
            conn, statement_cap["source_document_id"]
        )[0]
        assert line["currency"] == ""
        assert line["match_status"] == "unmatched"
        assert account_id == line["account_id"]
    get_settings.cache_clear()


def test_reconcile_guard_blocks_foreign_staged_line(empty_db, monkeypatch):
    monkeypatch.setenv("DB_PATH", empty_db)
    get_settings.cache_clear()
    with engine.write_tx(empty_db) as conn:
        account_id = int(
            conn.execute(
                """INSERT INTO accounts(name,institution,kind,currency)
                   VALUES ('Home Account','Contract Bank','chequing','CAD')"""
            ).lastrowid
        )
        doc_id = repo_documents.insert_source_document(
            conn,
            kind="statement",
            original_name="synthetic-foreign.pdf",
            storage_ref="contract/foreign.pdf",
            sha256="contract-foreign",
            mime_type="application/pdf",
            status="processed",
        )
        repo_statements.stage_lines(
            conn,
            source_document_id=doc_id,
            account_id=account_id,
            parsed=ExtractedStatement(
                currency="USD",
                statement_period="2026-06",
                rows=[
                    StatementRow(
                        posted_on="2026-06-26",
                        description="FOREIGN PURCHASE",
                        amount_cents=-9900,
                    ),
                    StatementRow(
                        posted_on="2026-06-27",
                        description="SECOND FOREIGN PURCHASE",
                        amount_cents=-8800,
                    )
                ],
            ),
        )
        first_line = repo_statements.lines_for_document(conn, doc_id)[0]
        assert reconcile_apply.promote_line(conn, first_line["id"]) is None

    result = reconcile_document(empty_db, doc_id, llm=None)
    assert result["needs_review"] == 1
    assert result["promoted"] == 0
    with engine.read_conn(empty_db) as conn:
        lines = repo_statements.lines_for_document(conn, doc_id)
        assert len(lines) == 2
        assert all(line["match_status"] == "needs_review" for line in lines)
        assert all("unsupported_currency" in line["match_rationale"] for line in lines)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    get_settings.cache_clear()
