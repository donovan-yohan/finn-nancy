from __future__ import annotations

import fitz  # PyMuPDF

from app.db import engine
from app.ingest.schemas import ExtractedReceipt


def _make_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln, fontsize=12)
        y += 22
    return doc.tobytes()


def test_single_page_pdf_is_treated_as_receipt(app_env, fake_llm):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    raw = _make_pdf(["ExampleRide Eats", "Order 2026-06-20", "Burrito     15.00", "Total       15.00"])
    cap = capture(raw=raw, original_name="ubereats.pdf", channel="web")
    assert cap["kind"] == "receipt"  # 1 page, no statement markers

    receipt = ExtractedReceipt(
        merchant="ExampleRide Eats", currency="CAD", subtotal_cents=1500, total_cents=1500,
        category_guess="Restaurants", confidence=0.9,
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "inserted"

    with engine.read_conn(app_env) as conn:
        txn = conn.execute(
            "SELECT amount_cents FROM transactions WHERE id=?", (res["transaction_id"],)
        ).fetchone()
    assert txn["amount_cents"] == -1500  # PDF page rasterized -> vision -> signed expense


def test_one_page_statement_with_header_word_is_statement(app_env):
    """EQ-style single-page statement: no marker phrases, just 'Statement' in the header."""
    from app.ingest.extract.pdf import pdf_doc_kind

    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), "March 2026 Statement", fontsize=14)
    p.insert_text((72, 100), "# 000-000-001   March 01, 2026 to March 31, 2026", fontsize=10)
    p.insert_text((72, 130), "Mar 05  Interest earned   1.23", fontsize=10)
    assert pdf_doc_kind(doc.tobytes()) == "statement"


def test_multipage_delivery_receipt_is_receipt(app_env):
    """ExampleRide-Eats-style 2-page PDF with receipt language must NOT be triaged as a statement."""
    from app.ingest.extract.pdf import pdf_doc_kind

    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Here's your receipt for McDonald's.", fontsize=12)
    p1.insert_text((72, 100), "Total  $25.14", fontsize=12)
    doc.new_page().insert_text((72, 72), "Thanks for tipping, Sample Member A", fontsize=12)
    assert pdf_doc_kind(doc.tobytes()) == "receipt"


def test_statement_pdf_stages_lines_and_enqueues_reconcile(app_env):
    """Statement PDFs parse into statement_lines and hand off to the reconcile job."""
    from app.db import engine as eng
    from app.ingest.pipeline import process_document
    from app.ingest.schemas import ExtractedStatement, StatementRow
    from app.ingest.storage import capture

    raw = _make_pdf(["MONTHLY STATEMENT", "Opening balance   100.00",
                     "Closing balance    50.00", "Payment due        50.00"])
    cap = capture(raw=raw, original_name="statement.pdf", channel="web")
    assert cap["kind"] == "statement"  # marker-driven

    parsed = ExtractedStatement(
        institution="Fake CU", account_last4="9003", currency="CAD",
        statement_period="2026-01",
        period_start_on="2026-01-01",
        period_end_on="2026-01-31",
        statement_issued_on="2026-02-01",
        opening_balance_cents=10000, closing_balance_cents=5000,
        declared_page_count=1,
        declared_row_count=1,
        field_confidence={
            field: 0.9
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
                posted_on="2026-01-10",
                description="COFFEE",
                amount_cents=-5000,
                page_number=1,
                field_confidence={
                    "posted_on": 0.9,
                    "description": 0.9,
                    "amount_cents": 0.9,
                },
            )
        ],
        confidence=0.9,
    )

    class _StatementLLM:
        def with_structured_output(self, schema, **kw):
            class _S:
                def invoke(self, messages):
                    return parsed
            return _S()

    with eng.write_tx(app_env) as conn:
        conn.execute("UPDATE accounts SET external_ref='FCU:chequing:9003' WHERE id=1")

    res = process_document(app_env, cap["source_document_id"], _StatementLLM())
    assert res["status"] == "staged" and res["lines"] == 1

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT * FROM statement_lines").fetchone()
        assert line["amount_cents"] == -5000 and line["account_id"] == 1
        job = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document'"
        ).fetchone()
        assert job is not None
        # statements never insert transactions at ingest (sole-promoter invariant)
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='statement'").fetchone()[0]
        assert n == 0
