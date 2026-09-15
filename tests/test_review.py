from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import (
    engine,
    repo_documents,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
)
from app.ingest.pipeline import process_document
from app.ingest.schemas import ExtractedReceipt, ExtractedStatement, StatementRow
from app.ingest.storage import capture
from app.web.routes.review import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _seed_needs_review(
    app_env,
    make_jpeg,
    fake_llm,
    *,
    name: str = "blurry.jpg",
    merchant: str = "???",
    category_guess: str = "",
):
    """Stage a receipt and run it through the real pipeline with a low-confidence
    extraction, landing it in the review queue exactly the way production traffic would."""
    cap = capture(raw=make_jpeg(), original_name=name, channel="web")
    receipt = ExtractedReceipt(
        merchant=merchant,
        currency="CAD",
        total_cents=999,
        category_guess=category_guess,
        confidence=0.2,
    )
    res = process_document(app_env, cap["source_document_id"], fake_llm(receipt))
    assert res["status"] == "needs_review"
    with engine.read_conn(app_env) as conn:
        extraction = conn.execute(
            "SELECT * FROM ingest_extractions WHERE source_document_id=?",
            (cap["source_document_id"],),
        ).fetchone()
    return cap["source_document_id"], extraction["id"]


def _seed_statement_review(
    app_env,
    *,
    rows: bool = True,
    zero_activity: bool = False,
    staged_account_id: int | None = None,
    closing_balance_cents: int = 8750,
):
    """Stage a statement doc in the review queue. `staged_account_id` mirrors an ingest that
    already auto-resolved an account (e.g. a checksum_mismatch doc); a mismatching
    `closing_balance_cents` makes the checksum fail so the reason renders as checksum_mismatch."""
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
        account_hint="Everyday Chequing",
        account_last4="9003",
        currency="CAD",
        statement_period="2026-06",
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=10000,
        closing_balance_cents=closing_balance_cents,
        declared_page_count=1,
        declared_row_count=1 if rows else 0,
        zero_activity=zero_activity,
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
                )
            ]
            if rows
            else []
        ),
        confidence=0.96,
        observed_page_count=1,
        extracted_page_count=1,
    )
    token = uuid.uuid4().hex[:12]
    raw = f"synthetic review statement {token}".encode()
    with engine.write_tx(app_env) as conn:
        cur = conn.execute(
            """INSERT INTO source_documents(
                 kind, original_name, storage_ref, sha256, mime_type, status)
               VALUES ('statement',?,?,?,'application/pdf','needs_review')""",
            (
                f"june-statement-{token}.pdf",
                f"inbox/june-statement-{token}.pdf",
                hashlib.sha256(raw).hexdigest(),
            ),
        )
        doc_id = int(cur.lastrowid)
        extraction_id = repo_documents.insert_extraction(
            conn,
            source_document_id=doc_id,
            doc_kind="statement",
            extracted_json=parsed.model_dump_json(),
            confidence=parsed.confidence,
            external_id="",
            proposed_account_id=staged_account_id,
            proposed_category_id=None,
            review_status="pending",
        )
        envelope = repo_statement_reviews.create_from_extraction(
            conn,
            source_document_id=doc_id,
            extraction_id=extraction_id,
            account_id=staged_account_id,
            parsed=parsed,
            raw=raw,
            actor="test:extract",
        )
        repo_statements.stage_lines(
            conn,
            source_document_id=doc_id,
            account_id=staged_account_id,
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
            account_id=staged_account_id,
            anchors=envelope.page_anchor_ids,
        )
    return doc_id, extraction_id


def test_review_queue_lists_item(app_env, make_jpeg, fake_llm):
    doc_id, _ = _seed_needs_review(app_env, make_jpeg, fake_llm, name="blurry.jpg")
    with engine.read_conn(app_env) as conn:
        capture_id = conn.execute(
            "SELECT capture_id FROM capture_provenance WHERE source_document_id=?",
            (doc_id,),
        ).fetchone()["capture_id"]

    r = _client().get("/review")

    assert r.status_code == 200
    assert "blurry.jpg" in r.text
    assert 'aria-label="Capture provenance"' in r.text
    assert "Web and camera · Local-only" in r.text
    assert capture_id not in r.text
    assert 'class="resolution-review-form"' in r.text
    assert 'data-resolution-kind="canonical_merchant"' in r.text
    assert 'data-resolution-kind="expense_category"' in r.text
    normalized_page = " ".join(r.text.split())
    assert "saved independently from the expense category" in normalized_page
    assert "merchant confirmation never" in normalized_page


def test_review_reject_and_delete_forms_require_confirm(app_env, make_jpeg, fake_llm):
    # FN-116: reject and doc-delete are destructive; each must guard its POST with a
    # confirm step whose copy names the specific document.
    _, extraction_id = _seed_needs_review(app_env, make_jpeg, fake_llm, name="blurry.jpg")
    r = _client().get("/review")
    assert r.status_code == 200

    assert f'action="/review/{extraction_id}/reject"' in r.text
    assert 'confirm("Reject blurry.jpg? It leaves the review queue.")' in r.text
    assert 'confirm("Delete blurry.jpg? This permanently removes the document.")' in r.text
    # every destructive form on the page carries an explicit confirm guard.
    assert r.text.count("onsubmit='return confirm(") >= 2


def test_review_queue_renders_statement_card(app_env):
    doc_id, _ = _seed_statement_review(app_env)

    r = _client().get("/review")

    assert r.status_code == 200
    assert "North Bank" in r.text
    assert "Everyday Chequing" in r.text
    assert "last4 9003" in r.text
    assert "period 2026-06" in r.text
    assert "1 row" in r.text
    assert "$100.00" in r.text and "$87.50" in r.text
    assert "account unresolved" in r.text
    assert "Review source, metadata, and rows" in r.text
    assert "set account, then review" in r.text
    assert "extracted: <strong>?" not in r.text
    assert f'action="/review/doc/{doc_id}/delete"' not in r.text


def test_review_queue_names_zero_activity_proof_instead_of_missing_rows(
    app_env,
):
    _seed_statement_review(
        app_env,
        rows=False,
        zero_activity=True,
        closing_balance_cents=10000,
    )

    response = _client().get("/review")

    assert response.status_code == 200
    assert "reason: zero activity proof review" in response.text


def test_review_queue_renders_similar_transaction_anchors(app_env, make_jpeg, fake_llm, monkeypatch):
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "false")
    get_settings.cache_clear()
    _seed_needs_review(
        app_env,
        make_jpeg,
        fake_llm,
        name="market.jpg",
        merchant="Synthetic Market",
        category_guess="Groceries",
    )

    r = _client().get("/review")

    assert r.status_code == 200
    assert "similar past transactions" in r.text
    assert "Synthetic Market" in r.text
    assert "#17" in r.text or "#11" in r.text or "#3" in r.text


def test_review_queue_renders_when_similarity_empty(app_env, make_jpeg, fake_llm, monkeypatch):
    from app.web.routes import review as review_routes

    monkeypatch.setattr(review_routes.repo_embeddings, "similar_transactions", lambda *args, **kwargs: [])
    _seed_needs_review(app_env, make_jpeg, fake_llm, name="no-neighbors.jpg")

    r = _client().get("/review")

    assert r.status_code == 200
    assert "no-neighbors.jpg" in r.text


def test_review_doc_file(app_env, make_jpeg, fake_llm):
    doc_id, _ = _seed_needs_review(app_env, make_jpeg, fake_llm)
    r = _client().get(f"/review/doc/{doc_id}/file")
    assert r.status_code == 200
    assert len(r.content) > 0


def test_review_doc_file_missing_404(app_env):
    r = _client().get("/review/doc/999999/file")
    assert r.status_code == 404


def _insert_doc(storage_ref: str) -> int:
    with engine.write_tx(get_settings().db_path) as conn:
        cur = conn.execute(
            "INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status) "
            "VALUES ('upload','x',?,'deadbeef','application/pdf','staged')",
            (storage_ref,),
        )
        return int(cur.lastrowid)


def test_review_doc_file_path_traversal_blocked(app_env):
    # A row whose storage_ref escapes DATA_DIR (absolute path outside it, or via '..')
    # must never be served, regardless of whether the target file exists.
    doc_id = _insert_doc("/etc/hostname")
    r = _client().get(f"/review/doc/{doc_id}/file")
    assert r.status_code == 404


def test_review_doc_file_legacy_absolute_ref_inside_data_dir_serves(app_env):
    # Legacy live rows store an absolute path that still lives under DATA_DIR
    # (e.g. <data_dir>/inbox/x.pdf); those must keep serving.
    data_dir = Path(get_settings().data_dir)
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    real_file = inbox / "legacy.pdf"
    real_file.write_bytes(b"%PDF-1.4 fake")

    doc_id = _insert_doc(str(real_file))
    r = _client().get(f"/review/doc/{doc_id}/file")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 fake"


def test_approve_with_overrides_then_duplicate(app_env, make_jpeg, fake_llm):
    doc_id, extraction_id = _seed_needs_review(app_env, make_jpeg, fake_llm)
    client = _client()
    form = {
        "merchant": "Corner Store",
        "purchased_on": "2026-06-10",
        "total": "9.99",
        "category_name": "Groceries",
        "account_id": "",
    }

    r = client.post(f"/review/{extraction_id}/approve", data=form)
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        txns = conn.execute(
            "SELECT * FROM transactions WHERE source_document_id=?", (doc_id,)
        ).fetchall()
        assert len(txns) == 1
        txn = txns[0]
        assert txn["amount_cents"] == -999

        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn["id"],)
        ).fetchone()
        cat = conn.execute(
            "SELECT name FROM categories WHERE id=?", (split["category_id"],)
        ).fetchone()
        assert cat["name"] == "Groceries"

        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "processed"

        extraction = conn.execute(
            "SELECT review_status FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        assert extraction["review_status"] == "approved"

        claims = conn.execute(
            """
            SELECT claim_kind, event_kind, trust_state
            FROM v_current_merchant_resolution_claims
            WHERE transaction_id=?
            ORDER BY claim_kind
            """,
            (txn["id"],),
        ).fetchall()
        assert [tuple(row) for row in claims] == [
            ("canonical_merchant", "accepted", "human_confirmed"),
            ("expense_category", "accepted", "human_confirmed"),
        ]
        resolution = conn.execute(
            """
            SELECT resolution_status
            FROM v_expense_resolution_status
            WHERE transaction_id=?
            """,
            (txn["id"],),
        ).fetchone()
        assert resolution["resolution_status"] == "resolved"

    # approving the same extraction again hits the duplicate path (same doc sha ->
    # same external_id) — must not create a second transaction.
    r2 = client.post(f"/review/{extraction_id}/approve", data=form)
    assert r2.status_code in (200, 303)
    with engine.read_conn(app_env) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source_document_id=?", (doc_id,)
        ).fetchone()[0]
        assert n == 1
        extraction = conn.execute(
            "SELECT review_status FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        assert extraction["review_status"] == "approved"


def test_approve_invalid_amount_rejected(app_env, make_jpeg, fake_llm):
    doc_id, extraction_id = _seed_needs_review(app_env, make_jpeg, fake_llm, name="bad-amount.jpg")
    client = _client()
    form = {
        "merchant": "Corner Store",
        "purchased_on": "2026-06-10",
        "total": "not-a-number",
        "category_name": "Groceries",
        "account_id": "",
    }
    r = client.post(f"/review/{extraction_id}/approve", data=form)
    assert r.status_code == 400

    with engine.read_conn(app_env) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source_document_id=?", (doc_id,)
        ).fetchone()[0]
        assert n == 0
        extraction = conn.execute(
            "SELECT review_status FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        assert extraction["review_status"] == "pending"
        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "needs_review"


def test_receipt_approve_rejects_statement_extraction(app_env):
    doc_id, extraction_id = _seed_statement_review(app_env)
    r = _client().post(
        f"/review/{extraction_id}/approve",
        data={
            "merchant": "Wrong",
            "purchased_on": "2026-06-10",
            "total": "0.00",
            "category_name": "Groceries",
            "account_id": "",
        },
    )
    assert r.status_code == 400

    with engine.read_conn(app_env) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source_document_id=?", (doc_id,)
        ).fetchone()[0] == 0


def test_statement_account_resolution_stops_at_received_until_approval(app_env):
    doc_id, extraction_id = _seed_statement_review(app_env)

    r = _client().post(
        f"/review/{extraction_id}/approve-statement", data={"account_id": "1"}
    )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        lines = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (doc_id,)
        ).fetchall()
        assert len(lines) == 1
        assert lines[0]["account_id"] == 1
        extraction = conn.execute(
            "SELECT review_status, proposed_account_id FROM ingest_extractions WHERE id=?",
            (extraction_id,),
        ).fetchone()
        assert extraction["review_status"] == "pending"
        assert conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (doc_id,)
        ).fetchone()["status"] == "needs_review"
        link = repo_statement_expectations.active_link_for_document(conn, doc_id)
        assert link is not None and link["lifecycle_state"] == "received"
        job = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document' AND source_document_id=?",
            (doc_id,),
        ).fetchone()
        assert job is None


def test_statement_account_can_be_corrected_before_approval(app_env):
    doc_id, extraction_id = _seed_statement_review(app_env)
    client = _client()

    first = client.post(
        f"/review/{extraction_id}/approve-statement", data={"account_id": "1"}
    )
    assert first.status_code in (200, 303)

    second = client.post(
        f"/review/{extraction_id}/approve-statement", data={"account_id": "2"}
    )
    assert second.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        lines = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (doc_id,)
        ).fetchall()
        assert len(lines) == 1
        assert {line["account_id"] for line in lines} == {2}
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document' AND source_document_id=?",
            (doc_id,),
        ).fetchall()
        assert jobs == []


def test_approve_statement_replaces_prior_account_lines(app_env):
    # A checksum_mismatch doc is staged under its auto-resolved account (1). Approving with a
    # DIFFERENT account (2) must MOVE the lines, not leave the old set and stack a second set
    # under 2 — otherwise the doc ends up double-booked and both sets promotable from /recon.
    doc_id, extraction_id = _seed_statement_review(
        app_env, staged_account_id=1, closing_balance_cents=9999
    )

    r = _client().post(
        f"/review/{extraction_id}/approve-statement", data={"account_id": "2"}
    )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        lines = conn.execute(
            "SELECT account_id FROM statement_lines WHERE source_document_id=?", (doc_id,)
        ).fetchall()
        assert len(lines) == 1
        assert lines[0]["account_id"] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM statement_lines WHERE source_document_id=? AND account_id=1",
            (doc_id,),
        ).fetchone()[0] == 0


def test_review_and_recon_resolve_account_identically(app_env):
    # /review approve-statement and /recon assign-account share one mutation helper, so both
    # must leave the same statement_lines state (modulo the chosen account and its hash).
    from app.ingest.normalize import row_hash
    from app.web.routes.recon import router as recon_router

    review_doc, review_extraction = _seed_statement_review(app_env)
    recon_doc, _ = _seed_statement_review(app_env)

    app = FastAPI()
    app.include_router(router)
    app.include_router(recon_router)
    client = TestClient(app)

    assert client.post(
        f"/review/{review_extraction}/approve-statement", data={"account_id": "1"}
    ).status_code in (200, 303)
    assert client.post(
        f"/recon/doc/{recon_doc}/assign-account",
        data={"account_id": "2", "month": "2026-06"},
    ).status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        review_line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (review_doc,)
        ).fetchone()
        recon_line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (recon_doc,)
        ).fetchone()
        shared = ("posted_on", "raw_description", "norm_merchant", "amount_cents",
                  "currency", "match_status")
        assert {c: review_line[c] for c in shared} == {c: recon_line[c] for c in shared}
        assert review_line["account_id"] == 1
        assert recon_line["account_id"] == 2
        assert review_line["row_hash"] == row_hash(
            1, review_line["posted_on"], review_line["amount_cents"],
            review_line["raw_description"], 0,
        )
        assert recon_line["row_hash"] == row_hash(
            2, recon_line["posted_on"], recon_line["amount_cents"],
            recon_line["raw_description"], 0,
        )
        for doc_id in (review_doc, recon_doc):
            assert conn.execute(
                "SELECT status FROM source_documents WHERE id=?", (doc_id,)
            ).fetchone()["status"] == "needs_review"
            assert conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE type='reconcile_document' AND source_document_id=?",
                (doc_id,),
            ).fetchone()[0] == 0


def test_approve_statement_requires_new_account_name(app_env):
    _, extraction_id = _seed_statement_review(app_env)

    r = _client().post(
        f"/review/{extraction_id}/approve-statement",
        data={"account_id": "", "name": "", "kind": "chequing"},
    )

    assert r.status_code == 400


def test_approve_statement_rejects_invalid_account_kind(app_env):
    _, extraction_id = _seed_statement_review(app_env)

    r = _client().post(
        f"/review/{extraction_id}/approve-statement",
        data={"account_id": "", "name": "New account", "kind": "invalid"},
    )

    assert r.status_code == 400


def test_approve_statement_rejects_nonexistent_account(app_env):
    _, extraction_id = _seed_statement_review(app_env)

    r = _client().post(
        f"/review/{extraction_id}/approve-statement", data={"account_id": "999999"}
    )

    assert r.status_code == 400


def test_approve_statement_creates_account(app_env):
    doc_id, extraction_id = _seed_statement_review(app_env)

    r = _client().post(
        f"/review/{extraction_id}/approve-statement",
        data={
            "account_id": "",
            "name": "North Bank Everyday Chequing",
                "institution": "North Bank",
                "kind": "chequing",
                "external_ref": "9003",
                "statement_cadence": "monthly",
            },
        )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        account = conn.execute(
            "SELECT * FROM accounts WHERE name='North Bank Everyday Chequing'"
        ).fetchone()
        assert account is not None
        assert account["institution"] == "North Bank"
        assert account["kind"] == "chequing"
        assert account["external_ref"] == "9003"
        line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (doc_id,)
        ).fetchone()
        assert line["account_id"] == account["id"]


def test_reject(app_env, make_jpeg, fake_llm):
    doc_id, extraction_id = _seed_needs_review(app_env, make_jpeg, fake_llm, name="reject-me.jpg")
    client = _client()
    r = client.post(f"/review/{extraction_id}/reject")
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        extraction = conn.execute(
            "SELECT review_status FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()
        assert extraction["review_status"] == "rejected"
        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "archived"
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source_document_id=?", (doc_id,)
        ).fetchone()[0]
        assert n == 0


def test_reject_statement_excludes_null_account_lines_but_preserves_evidence(app_env):
    doc_id, extraction_id = _seed_statement_review(app_env)

    r = _client().post(f"/review/{extraction_id}/reject")
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (doc_id,)
        ).fetchone()
        assert line is not None and line["review_disposition"] == "excluded"
        assert conn.execute(
            "SELECT review_status FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()["review_status"] == "rejected"


def test_reject_statement_excludes_resolved_account_lines(app_env):
    # A checksum_mismatch doc's lines were already staged under a non-null account; rejecting
    # must remove ALL of them, not just account-less rows, so nothing stays promotable /recon.
    doc_id, extraction_id = _seed_statement_review(
        app_env, staged_account_id=1, closing_balance_cents=9999
    )

    r = _client().post(f"/review/{extraction_id}/reject")
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (doc_id,)
        ).fetchone()
        assert line is not None and line["review_disposition"] == "excluded"
        assert conn.execute(
            "SELECT review_status FROM ingest_extractions WHERE id=?", (extraction_id,)
        ).fetchone()["review_status"] == "rejected"


def test_delete_document(app_env, make_jpeg, fake_llm):
    doc_id, _ = _seed_needs_review(app_env, make_jpeg, fake_llm, name="delete-me.jpg")
    client = _client()
    r = client.post(f"/review/doc/{doc_id}/delete")
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        doc = conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc is None
