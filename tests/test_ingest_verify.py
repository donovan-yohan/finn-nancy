from __future__ import annotations

from app.db import engine
from app.ingest.schemas import ExtractedReceipt, ReceiptLineItem
from app.ingest.verify import VerifierVerdict, arithmetic_ok, verify_receipt


def _items(*amounts: int) -> list[ReceiptLineItem]:
    return [ReceiptLineItem(description=f"item{i}", amount_cents=a) for i, a in enumerate(amounts)]


# --- arithmetic check (deterministic, no LLM) -----------------------------------

def test_arithmetic_ok_line_items_sum_to_subtotal():
    r = ExtractedReceipt(
        subtotal_cents=3000, tax_cents=390, total_cents=3390, line_items=_items(1000, 2000)
    )
    assert arithmetic_ok(r) == (True, "ok")


def test_arithmetic_ok_line_items_sum_to_total():
    # Tax-inclusive line pricing: items sum to the grand total, no subtotal shown.
    r = ExtractedReceipt(total_cents=3000, line_items=_items(1000, 2000))
    assert arithmetic_ok(r) == (True, "ok")


def test_arithmetic_flags_line_item_mismatch():
    # Items sum to 3000 but the receipt claims a 9999 total (and no matching subtotal).
    r = ExtractedReceipt(total_cents=9999, line_items=_items(1000, 2000))
    ok, reason = arithmetic_ok(r)
    assert (ok, reason) == (False, "line_items_mismatch")


def test_arithmetic_no_line_items_passes():
    # Nothing itemized to reconcile — upstream _validate owns subtotal/tax/total.
    r = ExtractedReceipt(subtotal_cents=4000, tax_cents=210, total_cents=4210)
    assert arithmetic_ok(r) == (True, "ok")


# --- verify_receipt end-to-end (arithmetic + optional judge) ---------------------

def test_verify_receipt_passes_without_judge(make_jpeg, fake_llm):
    r = ExtractedReceipt(subtotal_cents=3000, total_cents=3000, line_items=_items(1000, 2000))
    assert verify_receipt(fake_llm(r), make_jpeg(), r) == (True, "ok")


def test_verify_receipt_flags_bad_math_without_judge(make_jpeg, fake_llm):
    r = ExtractedReceipt(total_cents=9999, line_items=_items(1000, 2000))
    assert verify_receipt(fake_llm(r), make_jpeg(), r) == (False, "line_items_mismatch")


def test_verify_receipt_judge_rejection(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("VERIFIER_LLM_JUDGE", "true")
    get_settings.cache_clear()
    r = ExtractedReceipt(total_cents=3000, line_items=_items(1000, 2000))  # arithmetic clean
    verdict = VerifierVerdict(fields_match=False, confidence=0.9, notes="total looks wrong")
    assert verify_receipt(fake_llm(verdict), make_jpeg(), r) == (False, "verifier_rejected")


def test_verify_receipt_judge_agrees(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("VERIFIER_LLM_JUDGE", "true")
    get_settings.cache_clear()
    r = ExtractedReceipt(total_cents=3000, line_items=_items(1000, 2000))
    verdict = VerifierVerdict(fields_match=True, confidence=0.95)
    assert verify_receipt(fake_llm(verdict), make_jpeg(), r) == (True, "ok")


def test_verify_receipt_judge_exception_fails_open(app_env, make_jpeg, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("VERIFIER_LLM_JUDGE", "true")
    get_settings.cache_clear()

    class _Boom:
        def with_structured_output(self, schema, **kwargs):
            raise RuntimeError("judge unavailable")

        def invoke(self, messages):
            raise RuntimeError("judge unavailable")

    r = ExtractedReceipt(total_cents=3000, line_items=_items(1000, 2000))
    # Judge blows up on a clean receipt -> fail-open to a pass, never raises.
    assert verify_receipt(_Boom(), make_jpeg(), r) == (True, "ok")


# --- pipeline integration (routing) ---------------------------------------------

def test_pipeline_line_item_mismatch_routes_to_review(app_env, make_jpeg, fake_llm):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="bad-items.jpg", channel="web")
    # subtotal+tax==total passes _validate, but line items sum to 3000, not 9000.
    receipt = ExtractedReceipt(
        merchant="Itemized Inc", currency="CAD", subtotal_cents=9000,
        tax_cents=0, total_cents=9000,
        line_items=_items(1000, 2000), confidence=0.95,
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "needs_review" and res["reason"] == "line_items_mismatch"

    with engine.read_conn(app_env) as conn:
        doc = conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (cap["source_document_id"],)
        ).fetchone()
        assert doc["status"] == "needs_review"
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='receipt'").fetchone()[0]
        assert n == 0  # nothing promoted


def test_pipeline_consistent_receipt_auto_files(app_env, make_jpeg, fake_llm):
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="good-items.jpg", channel="web")
    receipt = ExtractedReceipt(
        merchant="Tidy Grocer", currency="CAD", subtotal_cents=3000,
        tax_cents=0, total_cents=3000,
        line_items=_items(1000, 2000), category_guess="Groceries", confidence=0.95,
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "inserted"


def test_pipeline_verifier_disabled_skips_check(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.config import get_settings
    from app.ingest.pipeline import process_document
    from app.ingest.storage import capture

    monkeypatch.setenv("VERIFIER_ENABLED", "false")
    get_settings.cache_clear()
    cap = capture(raw=make_jpeg(), original_name="unchecked.jpg", channel="web")
    # Bad line-item math would route to review if the verifier ran.
    receipt = ExtractedReceipt(
        merchant="Skip Verify", currency="CAD", subtotal_cents=9000,
        tax_cents=0, total_cents=9000,
        line_items=_items(1000, 2000), confidence=0.95,
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "inserted"


def test_pipeline_judge_exception_still_auto_files(app_env, make_jpeg, monkeypatch):
    from app.config import get_settings
    from app.ingest import pipeline
    from app.ingest.schemas import ExtractedReceipt as _R
    from app.ingest.storage import capture

    monkeypatch.setenv("VERIFIER_LLM_JUDGE", "true")
    get_settings.cache_clear()

    receipt = _R(
        merchant="Flaky Judge", currency="CAD", subtotal_cents=3000,
        tax_cents=0, total_cents=3000,
        line_items=_items(1000, 2000), confidence=0.95,
    )

    class _ExtractOkJudgeBoom:
        """Returns the receipt for extraction; the judge call always raises."""

        def with_structured_output(self, schema, **kwargs):
            from app.ingest.verify import VerifierVerdict as _V

            if schema is _V:
                raise RuntimeError("judge unavailable")

            class _S:
                def invoke(self_inner, messages):
                    return receipt

            return _S()

        def invoke(self, messages):
            raise RuntimeError("judge unavailable")

    cap = capture(raw=make_jpeg(), original_name="flaky.jpg", channel="web")
    res = pipeline.process_document(app_env, cap["source_document_id"], _ExtractOkJudgeBoom())
    assert res["status"] == "inserted"  # verifier exception never blocks ingestion
