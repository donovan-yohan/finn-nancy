from __future__ import annotations

from app.db import engine
from app.ingest.schemas import ExtractedReceipt


def test_capture_dedup_and_receipt_pipeline(app_env, make_jpeg, fake_llm):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    jpeg = make_jpeg()
    cap = capture(raw=jpeg, original_name="loblaws.jpg", channel="web")
    assert cap["status"] == "staged" and cap["kind"] == "receipt"
    # identical bytes -> content-addressed dedup
    assert capture(raw=jpeg, original_name="loblaws.jpg", channel="web")["status"] == "duplicate"

    receipt = ExtractedReceipt(
        merchant="Loblaws #123", purchased_on="2026-06-03",
        currency="CAD",
        subtotal_cents=4000, tax_cents=210, total_cents=4210,
        category_guess="Groceries", confidence=0.95,
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "inserted"

    with engine.read_conn(app_env) as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id=?", (res["transaction_id"],)).fetchone()
        assert txn["amount_cents"] == -4210  # expense -> negative
        assert txn["source"] == "receipt"
        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn["id"],)
        ).fetchone()
        assert split["amount_cents"] == -4210
        cat = conn.execute("SELECT name FROM categories WHERE id=?", (split["category_id"],)).fetchone()
        assert cat["name"] == "Uncategorized"
        proposal = conn.execute(
            """
            SELECT kind, status
            FROM proposed_actions
            WHERE json_extract(payload_json, '$.transaction_id')=?
            """,
            (txn["id"],),
        ).fetchone()
        assert tuple(proposal) == ("recategorization", "proposed")
        knowledge = conn.execute(
            """
            SELECT claim_kind, event_kind, trust_state
            FROM v_current_merchant_resolution_claims
            WHERE transaction_id=?
            """,
            (txn["id"],),
        ).fetchone()
        assert tuple(knowledge) == (
            "expense_category",
            "proposed",
            "untrusted_proposal",
        )
        doc = conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (cap["source_document_id"],)
        ).fetchone()
        assert doc["status"] == "processed"

    # reprocessing the same document is idempotent (UNIQUE(source, external_id))
    again = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert again["status"] == "duplicate"


def test_low_confidence_routes_to_review(app_env, make_jpeg, fake_llm):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="blurry.jpg", channel="web")
    receipt = ExtractedReceipt(
        merchant="???", currency="CAD", total_cents=999, confidence=0.2
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "needs_review" and res["reason"] == "low_confidence"

    with engine.read_conn(app_env) as conn:
        doc = conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (cap["source_document_id"],)
        ).fetchone()
        assert doc["status"] == "needs_review"
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='receipt'").fetchone()[0]
        assert n == 0  # nothing promoted


def test_arithmetic_mismatch_routes_to_review(app_env, make_jpeg, fake_llm):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="bad-math.jpg", channel="web")
    # subtotal + tax = 3000, but total claims 9999 -> mismatch beyond tolerance
    receipt = ExtractedReceipt(
        merchant="Confused Cafe", currency="CAD", subtotal_cents=3000,
        tax_cents=0, total_cents=9999, confidence=0.9
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "needs_review" and res["reason"] == "arithmetic_mismatch"


def test_receipt_guess_creates_review_proposal_without_legacy_alias(
    app_env, make_jpeg, fake_llm
):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="a.jpg", channel="web")
    receipt = ExtractedReceipt(
        merchant="Loblaws #55", currency="CAD", subtotal_cents=1000, total_cents=1000,
        category_guess="Groceries", confidence=0.9,
    )
    process_document(app_env, cap["source_document_id"], fake_llm(receipt))

    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM merchant_aliases"
        ).fetchone()[0] == 0
        current = conn.execute(
            """
            SELECT event_kind, trust_state
            FROM v_current_merchant_resolution_claims
            WHERE claim_kind='expense_category'
            """
        ).fetchone()
        assert tuple(current) == ("proposed", "untrusted_proposal")
        assert conn.execute(
            "SELECT COUNT(*) FROM v_active_merchant_resolution_claims"
        ).fetchone()[0] == 0
