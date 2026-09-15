from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import engine
from app.ingest.schemas import ExtractedReceipt
from app.ingest.triage import TriageResult


def _receipt():
    return ExtractedReceipt(
        merchant="Shop", currency="CAD", total_cents=1000, confidence=0.95
    )


def test_triage_overrides_kind(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.ingest import pipeline
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="invoice.jpg", channel="web")
    monkeypatch.setattr(
        pipeline, "triage_document",
        lambda llm, raw, mime: TriageResult(kind="invoice", confidence=0.95),
    )

    result = pipeline.process_document(app_env, cap["source_document_id"], fake_llm(_receipt()))

    assert result == {"status": "unsupported_kind", "kind": "invoice"}
    with engine.read_conn(app_env) as conn:
        doc = conn.execute(
            "SELECT kind, status, metadata_json FROM source_documents WHERE id=?",
            (cap["source_document_id"],),
        ).fetchone()
    assert (doc["kind"], doc["status"]) == ("invoice", "needs_review")
    # Triage output is persisted (not only logged): the reclassification note survives
    # alongside the existing metadata keys.
    meta = json.loads(doc["metadata_json"])
    assert meta["channel"] == "web"
    assert meta["triage"] == {"from": "receipt", "to": "invoice", "confidence": 0.95}


def test_low_confidence_triage_keeps_sniffed_kind(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.ingest import pipeline
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="receipt.jpg", channel="web")
    monkeypatch.setattr(
        pipeline, "triage_document",
        lambda llm, raw, mime: TriageResult(kind="invoice", confidence=0.69),
    )

    result = pipeline.process_document(app_env, cap["source_document_id"], fake_llm(_receipt()))

    assert result["status"] == "inserted"
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            "SELECT kind, metadata_json FROM source_documents WHERE id=?",
            (cap["source_document_id"],),
        ).fetchone()
    assert row["kind"] == "receipt"
    # No override happened, so no triage note is persisted (keeps the review UI clutter-free).
    assert "triage" not in json.loads(row["metadata_json"])


def test_triage_failure_falls_back_cleanly(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.ingest import pipeline
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="receipt.jpg", channel="web")

    def fail(*args):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(pipeline, "triage_document", fail)
    result = pipeline.process_document(app_env, cap["source_document_id"], fake_llm(_receipt()))
    assert result["status"] == "inserted"


def test_triage_skips_non_image_documents(app_env, fake_llm, monkeypatch):
    from app.ingest import pipeline
    from app.ingest.storage import capture

    cap = capture(raw=b"just some notes, not an image", original_name="notes.txt", channel="web")

    def unexpected(*args):
        raise AssertionError("triage should only run for images")

    monkeypatch.setattr(pipeline, "triage_document", unexpected)
    result = pipeline.process_document(app_env, cap["source_document_id"], fake_llm(_receipt()))
    assert result == {"status": "unsupported_kind", "kind": "upload"}


def test_triage_disabled_skips_call(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.config import get_settings
    from app.ingest import pipeline
    from app.ingest.storage import capture

    monkeypatch.setenv("TRIAGE_ENABLED", "false")
    get_settings.cache_clear()
    cap = capture(raw=make_jpeg(), original_name="receipt.jpg", channel="web")

    def unexpected(*args):
        raise AssertionError("triage should be skipped")

    monkeypatch.setattr(pipeline, "triage_document", unexpected)
    result = pipeline.process_document(app_env, cap["source_document_id"], fake_llm(_receipt()))
    assert result["status"] == "inserted"


def _review_client() -> TestClient:
    from app.web.routes.review import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_reclassification_note_renders_in_review(app_env, make_jpeg, fake_llm, monkeypatch):
    """A reclassified doc shows the original sniffed kind, the triage kind, and confidence."""
    from app.ingest import pipeline
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="invoice.jpg", channel="web")
    monkeypatch.setattr(
        pipeline, "triage_document",
        lambda llm, raw, mime: TriageResult(kind="invoice", confidence=0.95),
    )
    pipeline.process_document(app_env, cap["source_document_id"], fake_llm(_receipt()))

    resp = _review_client().get("/review")
    assert resp.status_code == 200
    body = resp.text
    assert "reclassified from" in body
    assert "receipt" in body and "invoice" in body
    assert "0.95" in body


def test_no_note_for_non_reclassified_review_item(app_env, make_jpeg, fake_llm, monkeypatch):
    """Documents that were never reclassified show no note (no clutter)."""
    from app.ingest import pipeline
    from app.ingest.storage import capture

    cap = capture(raw=make_jpeg(), original_name="blurry.jpg", channel="web")
    monkeypatch.setattr(
        pipeline, "triage_document",
        lambda llm, raw, mime: TriageResult(kind="receipt", confidence=0.99),
    )
    # Low doc-level confidence routes the receipt to review without any reclassification.
    low = ExtractedReceipt(merchant="Shop", total_cents=1000, confidence=0.1)
    res = pipeline.process_document(app_env, cap["source_document_id"], fake_llm(low))
    assert res["status"] == "needs_review"

    resp = _review_client().get("/review")
    assert resp.status_code == 200
    assert "reclassified from" not in resp.text
