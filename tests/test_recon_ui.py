from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import (
    engine,
    repo_documents,
    repo_ledger,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
)
from app.ingest.schemas import ExtractedStatement
from app.reconcile import positive_flows
from app.web.routes import close as close_routes
from app.web.routes import recon


def test_positive_flow_mobile_css_keeps_table_as_the_only_scroller():
    css = (
        Path(__file__).parents[1] / "app" / "web" / "static" / "app.css"
    ).read_text()

    assert "#positive-flow-review .coverage-grid {" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert "#positive-flow-review .coverage-grid > *" in css
    assert "#positive-flow-review .card {" in css
    assert "overflow-x: visible;" in css
    assert "#positive-flow-review .table-wrap {" in css
    assert "overscroll-behavior-inline: contain;" in css
    assert "#positive-flow-review .action-row form {" in css
    assert "flex: 1 1 100%;" in css
    assert "#positive-flow-review .action-row select {" in css
    assert "width: 100%;" in css
    assert "min-width: 0;" in css


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(recon.router)
    return TestClient(app)


def _close_recon_client() -> TestClient:
    app = FastAPI()
    app.include_router(close_routes.router)
    app.include_router(recon.router)
    return TestClient(app)


class FakeApply:
    """Stand-in for the parallel builder's app.reconcile.apply module.

    Mimics the documented contract closely enough to exercise recon.py's own logic
    (write_tx usage, doc-status finalization, 404 handling) without depending on the
    real implementation, which is owned by another builder.
    """

    def confirm_match(self, conn, line_id, transaction_id):
        line = conn.execute("SELECT posted_on FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        repo_statements.set_match(
            conn, line_id, status="matched", method="manual", transaction_id=transaction_id,
            score=1.0, rationale="manual confirm",
        )
        repo_statements.mark_cleared(conn, transaction_id, line["posted_on"])

    def promote_line(self, conn, line_id):
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        cat_id = repo_ledger.ensure_uncategorized(conn)
        txn_id = repo_ledger.insert_transaction(
            conn, account_id=line["account_id"], posted_on=line["posted_on"],
            description=line["raw_description"], counterparty="", amount_cents=line["amount_cents"],
            source="statement", external_id=line["row_hash"], source_document_id=line["source_document_id"],
            source_confidence=1.0,
            flow_kind=line["flow_kind"],
        )
        repo_ledger.insert_split(conn, transaction_id=txn_id, category_id=cat_id,
                                 amount_cents=line["amount_cents"])
        repo_statements.set_match(conn, line_id, status="promoted", method="manual",
                                  transaction_id=txn_id, score=1.0)
        repo_statements.mark_cleared(conn, txn_id, line["posted_on"])

    def ignore_line(self, conn, line_id):
        repo_statements.set_match(conn, line_id, status="ignored", method="manual")

    def unreconcile_document(self, conn, doc_id):
        for line in repo_statements.lines_for_document(conn, doc_id):
            if line["matched_transaction_id"]:
                conn.execute(
                    "UPDATE transactions SET recon_status='uncleared', cleared_on='' WHERE id=?",
                    (line["matched_transaction_id"],),
                )
            repo_statements.set_match(conn, line["id"], status="unmatched")
        conn.execute("UPDATE source_documents SET status='needs_review' WHERE id=?", (doc_id,))


def _insert_doc(*, name: str = "statement.pdf", status: str = "needs_review") -> int:
    with engine.write_tx(get_settings().db_path) as conn:
        cur = conn.execute(
            "INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status) "
            "VALUES ('statement', ?, ?, 'deadbeef', 'application/pdf', ?)",
            (name, f"blobs/{name}-{status}", status),
        )
        return int(cur.lastrowid)


def _insert_line(*, doc_id: int, account_id: int | None, posted_on: str, amount_cents: int,
                 description: str = "SYNTHETIC MARKET", match_status: str = "needs_review",
                 row_hash: str = "hash1", rationale: str = "",
                 statement_period: str | None = None) -> int:
    declared_period = statement_period or posted_on[:7]
    with engine.write_tx(get_settings().db_path) as conn:
        cur = conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description, norm_merchant,
                 amount_cents, currency, is_pending, statement_period, row_hash,
                 match_status, match_rationale)
               VALUES (?,?,?,?,?,?,?,0,?,?,?,?)""",
            (doc_id, account_id, posted_on, description, description.upper(), amount_cents,
             "CAD", declared_period, row_hash, match_status, rationale),
        )
        return int(cur.lastrowid)


def _enroll_statement_review(
    doc_id: int, *, account_id: int | None, period: str
) -> None:
    confidence = {
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
        institution="Synthetic Bank",
        account_hint="Review fixture",
        account_last4="4242",
        currency="CAD",
        statement_period=period,
        period_start_on=f"{period}-01",
        period_end_on=f"{period}-31",
        statement_issued_on=f"{period}-31",
        opening_balance_cents=0,
        closing_balance_cents=500,
        declared_page_count=1,
        declared_row_count=1,
        field_confidence=confidence,
        field_pages={field: 1 for field in confidence},
        confidence=0.96,
        observed_page_count=1,
        extracted_page_count=1,
    )
    raw = f"statement review {doc_id}".encode()
    with engine.write_tx(get_settings().db_path) as conn:
        conn.execute(
            "UPDATE source_documents SET sha256=? WHERE id=?",
            (hashlib.sha256(raw).hexdigest(), int(doc_id)),
        )
        extraction_id = repo_documents.insert_extraction(
            conn,
            source_document_id=doc_id,
            doc_kind="statement",
            extracted_json=parsed.model_dump_json(),
            confidence=parsed.confidence,
            external_id="",
            proposed_account_id=account_id,
            proposed_category_id=None,
            review_status="pending",
        )
        repo_statement_reviews.create_from_extraction(
            conn,
            source_document_id=doc_id,
            extraction_id=extraction_id,
            account_id=account_id,
            parsed=parsed,
            raw=raw,
            actor="test:extract",
        )


def test_recon_page_lists_needs_review_line_and_candidate(app_env):
    doc_id = _insert_doc(name="jan-statement.pdf")
    # Sample txn id=3: account 3, 2026-01-05, -16243, 'weekly groceries', uncleared.
    line_id = _insert_line(doc_id=doc_id, account_id=3, posted_on="2026-01-05",
                           amount_cents=-16243, description="SYNTHETIC MARKET", row_hash="h-candidate")

    r = _client().get("/recon")
    assert r.status_code == 200
    assert "jan-statement.pdf" in r.text
    assert "SYNTHETIC MARKET" in r.text
    assert "weekly groceries" in r.text  # candidate txn description
    assert "SYNTHETIC_CARD_9001" in r.text  # candidate account name

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "needs_review"


def test_recon_ignore_and_unreconcile_forms_require_confirm(app_env):
    # FN-116: ignoring a line and unreconciling a document are destructive; each must
    # guard its POST with a confirm step whose copy names the specific item.
    doc_id = _insert_doc(name="jan-statement.pdf")
    line_id = _insert_line(doc_id=doc_id, account_id=3, posted_on="2026-01-05",
                           amount_cents=-16243, description="SYNTHETIC MARKET", row_hash="h-confirm")

    r = _client().get("/recon")
    assert r.status_code == 200
    assert f'action="/recon/line/{line_id}/ignore"' in r.text
    assert 'confirm("Ignore this statement line? SYNTHETIC MARKET")' in r.text
    assert f'action="/recon/doc/{doc_id}/unreconcile"' in r.text
    assert 'confirm("Unreconcile jan-statement.pdf? This clears its matches.")' in r.text


def test_recon_page_renders_statement_coverage_dashboard(app_env):
    doc_id = _insert_doc(name="coverage-june.pdf", status="matched")
    _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-01",
        amount_cents=-2500,
        description="MATCHED COFFEE",
        match_status="matched",
        row_hash="h-ui-covered",
    )
    _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-02",
        amount_cents=-4000,
        description="BRAND NEW SERVICE",
        match_status="unmatched",
        row_hash="h-ui-unmatched",
    )
    _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-03",
        amount_cents=-500,
        description="CARD PAYMENT",
        match_status="ignored",
        row_hash="h-ui-ignored",
    )
    _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-04",
        amount_cents=10000,
        description="PAYROLL",
        match_status="ignored",
        row_hash="h-ui-income",
    )
    with engine.write_tx(app_env) as conn:
        row, attached = repo_statement_expectations.attach_exact_document(
            conn,
            doc_id,
            actor="test:ingest",
            reason="coverage fixture has exact account and closing period",
        )
        assert attached and row is not None
        repo_statement_expectations.mark_document_reviewed(
            conn,
            doc_id,
            actor="test:review",
            reason="coverage fixture reviewed",
        )

    r = _client().get("/recon")
    assert r.status_code == 200
    body = r.text
    assert "Matched vs unmatched spend" in body
    assert "coverage-june.pdf" in body
    assert "$70.00" in body  # statement spend includes ignored expense lines
    assert "$25.00" in body
    assert "$40.00" in body
    assert "$5.00" in body
    assert "$100.00" in body
    assert "38.5% covered" in body
    assert "new merchant" in body
    assert "BRAND NEW SERVICE" in body


def test_confirm_matches_line_clears_txn_and_finalizes_doc(app_env, monkeypatch):
    monkeypatch.setattr(recon, "apply", FakeApply())
    doc_id = _insert_doc()
    line_id = _insert_line(doc_id=doc_id, account_id=3, posted_on="2026-01-05",
                           amount_cents=-16243, row_hash="h-confirm")

    r = _client().post(
        f"/recon/line/{line_id}/confirm",
        data={"transaction_id": "3", "month": "2026-01"},
    )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "matched"
        assert line["matched_transaction_id"] == 3

        txn = conn.execute("SELECT * FROM transactions WHERE id=3").fetchone()
        assert txn["recon_status"] == "cleared"
        assert txn["cleared_on"] == "2026-01-05"

        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "matched"  # no needs_review lines remain for this doc


def test_confirm_second_line_on_already_matched_txn_returns_400(app_env):
    """Fix 2 regression: uses the REAL apply module (no monkeypatch) so the confirm route's
    ValueError -> HTTPException(400) wiring is actually exercised."""
    doc_id = _insert_doc()
    line1_id = _insert_line(doc_id=doc_id, account_id=3, posted_on="2026-01-05",
                            amount_cents=-16243, row_hash="h-confirm-1")
    line2_id = _insert_line(doc_id=doc_id, account_id=3, posted_on="2026-01-05",
                            amount_cents=-16243, row_hash="h-confirm-2")
    client = _client()

    r1 = client.post(
        f"/recon/line/{line1_id}/confirm",
        data={"transaction_id": "3", "month": "2026-01"},
    )
    assert r1.status_code in (200, 303)

    r2 = client.post(
        f"/recon/line/{line2_id}/confirm",
        data={"transaction_id": "3", "month": "2026-01"},
    )
    assert r2.status_code == 400

    with engine.read_conn(app_env) as conn:
        line2 = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line2_id,)).fetchone()
        assert line2["match_status"] == "needs_review"  # unchanged from _insert_line's default
        assert line2["matched_transaction_id"] is None


def test_promote_creates_new_signed_transaction(app_env, monkeypatch):
    monkeypatch.setattr(recon, "apply", FakeApply())
    doc_id = _insert_doc()
    line_id = _insert_line(doc_id=doc_id, account_id=1, posted_on="2026-04-01",
                           amount_cents=-2500, description="NEW MERCHANT", row_hash="h-promote")

    r = _client().post(
        f"/recon/line/{line_id}/promote",
        data={"flow_kind": "purchase", "month": "2026-04"},
    )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT * FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "promoted"
        assert line["matched_transaction_id"] is not None

        txn = conn.execute(
            "SELECT * FROM transactions WHERE id=?", (line["matched_transaction_id"],)
        ).fetchone()
        assert txn["amount_cents"] == -2500
        assert txn["source"] == "statement"
        assert txn["recon_status"] == "cleared"
        assert txn["flow_kind"] == "purchase"

        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?", (txn["id"],)
        ).fetchone()
        assert split is not None
        assert split["amount_cents"] == -2500

        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "matched"


def test_ignore_marks_line_ignored(app_env, monkeypatch):
    monkeypatch.setattr(recon, "apply", FakeApply())
    doc_id = _insert_doc()
    line_id = _insert_line(doc_id=doc_id, account_id=1, posted_on="2026-04-02",
                           amount_cents=-999, row_hash="h-ignore")

    r = _client().post(
        f"/recon/line/{line_id}/ignore", data={"month": "2026-04"}
    )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT match_status FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "ignored"
        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "matched"


def test_assign_account_updates_lines_then_returns_to_statement_review(app_env):
    doc_id = _insert_doc(name="unresolved.pdf")
    line_id = _insert_line(doc_id=doc_id, account_id=None, posted_on="2026-05-01",
                           amount_cents=-500, row_hash="h-unresolved", match_status="unmatched")
    _enroll_statement_review(doc_id, account_id=None, period="2026-05")

    r = _client().get("/recon")
    assert r.status_code == 200
    assert "unresolved.pdf" in r.text

    r2 = _client().post(
        f"/recon/doc/{doc_id}/assign-account",
        data={"account_id": "2", "month": "2026-05"},
        follow_redirects=False,
    )
    assert r2.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT account_id FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["account_id"] == 2

        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "needs_review"

        job = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document' AND source_document_id=?", (doc_id,)
        ).fetchone()
        assert job is None


def test_assign_account_recomputes_row_hash_and_drops_duplicate(app_env):
    """Fix 3 regression: a line staged with account_id=None keeps a hash computed against
    account 0. Once assign-account resolves it, that hash must be recomputed against the
    real account — otherwise a row that's ALREADY staged under that account (from another
    doc) collides only at promotion time. Here it collides immediately, so the newly
    resolved (re-exported) line is dropped as a duplicate."""
    from app.ingest.normalize import row_hash as _row_hash

    with engine.write_tx(app_env) as conn:
        cur = conn.execute(
            "INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status) "
            "VALUES ('statement','a.pdf','blobs/a-dedupe','sha-a-dedupe','application/pdf','processed')"
        )
        doc_a = int(cur.lastrowid)
        hash_a = _row_hash(1, "2026-05-01", -500, "SOME MERCHANT", 0)
        conn.execute(
                """INSERT INTO statement_lines(
                     source_document_id, account_id, posted_on, raw_description, norm_merchant,
                     amount_cents, currency, is_pending, statement_period, row_hash, match_status)
                   VALUES (?,1,'2026-05-01','SOME MERCHANT','SOME MERCHANT',-500,
                           'CAD',0,'2026-05',?,'unmatched')""",
            (doc_a, hash_a),
        )

    doc_b = _insert_doc(name="unresolved-dup.pdf")
    _insert_line(doc_id=doc_b, account_id=None, posted_on="2026-05-01", amount_cents=-500,
                description="SOME MERCHANT", row_hash="placeholder-not-yet-resolved",
                match_status="unmatched")
    _enroll_statement_review(doc_b, account_id=None, period="2026-05")

    r = _client().post(
        f"/recon/doc/{doc_b}/assign-account",
        data={"account_id": "1", "month": "2026-05"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        remaining_b = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (doc_b,)
        ).fetchall()
        assert len(remaining_b) == 1
        assert remaining_b[0]["review_disposition"] == "excluded"
        assert repo_statements.lines_for_document(conn, doc_b) == []

        a_line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?", (doc_a,)
        ).fetchone()
        assert a_line["row_hash"] == hash_a  # doc A's own line is untouched


def test_assign_account_refuses_already_resolved_doc(app_env):
    """Idempotency/scope guard: once a doc's lines have moved past 'unmatched' (reconcile
    ran and matched/promoted them into real transactions), a repeat assign-account POST
    (double submit, browser resubmit, direct POST) must be refused, not silently re-move the
    lines onto a different account — which would leave statement_lines and transactions
    disagreeing on the account."""
    doc_id = _insert_doc(name="resolved.pdf", status="matched")
    line_id = _insert_line(doc_id=doc_id, account_id=3, posted_on="2026-01-05",
                           amount_cents=-16243, row_hash="h-resolved", match_status="matched")

    r = _client().post(
        f"/recon/doc/{doc_id}/assign-account",
        data={"account_id": "2", "month": "2026-01"},
    )
    assert r.status_code == 409

    with engine.read_conn(app_env) as conn:
        line = conn.execute(
            "SELECT account_id, match_status, row_hash FROM statement_lines WHERE id=?", (line_id,)
        ).fetchone()
        # Line untouched: account not moved, status/hash preserved.
        assert line["account_id"] == 3
        assert line["match_status"] == "matched"
        assert line["row_hash"] == "h-resolved"

        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "matched"  # unchanged (guard fired before set_status)

        job = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='reconcile_document' AND source_document_id=?",
            (doc_id,),
        ).fetchone()[0]
        assert job == 0  # no stray reconcile re-enqueued


def test_unreconcile_document_via_fake_apply(app_env, monkeypatch):
    monkeypatch.setattr(recon, "apply", FakeApply())
    doc_id = _insert_doc(status="matched")
    line_id = _insert_line(doc_id=doc_id, account_id=3, posted_on="2026-01-05",
                           amount_cents=-16243, row_hash="h-unreconcile", match_status="matched")
    with engine.write_tx(get_settings().db_path) as conn:
        repo_statements.set_match(conn, line_id, status="matched", transaction_id=3, method="manual")
        repo_statements.mark_cleared(conn, 3, "2026-01-05")

    r = _client().post(
        f"/recon/doc/{doc_id}/unreconcile", data={"month": "2026-01"}
    )
    assert r.status_code in (200, 303)

    with engine.read_conn(app_env) as conn:
        line = conn.execute("SELECT match_status FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        assert line["match_status"] == "unmatched"
        txn = conn.execute("SELECT recon_status FROM transactions WHERE id=3").fetchone()
        assert txn["recon_status"] == "uncleared"
        doc = conn.execute("SELECT status FROM source_documents WHERE id=?", (doc_id,)).fetchone()
        assert doc["status"] == "needs_review"


def test_rerun_enqueues_job(app_env):
    doc_id = _insert_doc(status="matched")
    _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-05-01",
        amount_cents=-500,
        row_hash="h-rerun",
        match_status="unmatched",
    )
    r = _client().post(
        f"/recon/doc/{doc_id}/rerun", data={"month": "2026-05"}
    )
    assert r.status_code in (200, 303)
    with engine.read_conn(app_env) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE type='reconcile_document' AND source_document_id=?", (doc_id,)
        ).fetchone()
        assert job is not None


def test_404_on_unknown_line_and_doc_ids(app_env, monkeypatch):
    monkeypatch.setattr(recon, "apply", FakeApply())
    client = _client()
    month = {"month": "2026-06"}
    assert client.post(
        "/recon/line/999999/confirm",
        data={"transaction_id": "1", **month},
    ).status_code == 404
    assert client.post(
        "/recon/line/999999/promote", data=month
    ).status_code == 404
    assert client.post(
        "/recon/line/999999/ignore", data=month
    ).status_code == 404
    assert client.post(
        "/recon/doc/999999/assign-account",
        data={"account_id": "1", **month},
    ).status_code == 404
    assert client.post(
        "/recon/doc/999999/unreconcile", data=month
    ).status_code == 404
    assert client.post(
        "/recon/doc/999999/rerun", data=month
    ).status_code == 404


def test_recon_page_empty_state(app_env):
    r = _client().get("/recon")
    assert r.status_code == 200
    assert "nothing to reconcile" in r.text


def test_positive_statement_line_cannot_bypass_review_with_income_or_ignore(
    app_env,
):
    doc_id = _insert_doc(name="positive-bypass.csv")
    line_id = _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-20",
        amount_cents=10000,
        description="AMBIGUOUS CREDIT",
        row_hash="h-positive-bypass",
        statement_period="2026-06",
    )
    client = _client()
    promote = client.post(
        f"/recon/line/{line_id}/promote",
        data={"flow_kind": "income", "month": "2026-06"},
    )
    assert promote.status_code == 400
    assert "must be created as unknown" in promote.text
    ignored = client.post(
        f"/recon/line/{line_id}/ignore",
        data={"month": "2026-06"},
    )
    assert ignored.status_code == 400
    assert "cannot be ignored" in ignored.text
    with engine.read_conn(app_env) as conn:
        line = conn.execute(
            "SELECT match_status, flow_kind FROM statement_lines WHERE id=?",
            (line_id,),
        ).fetchone()
        assert tuple(line) == ("needs_review", "unknown")


def test_close_ignored_positive_deep_link_recovers_one_row_into_review(
    app_env,
):
    doc_id = _insert_doc(name="legacy-ignored-positive.csv", status="matched")
    line_id = _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-20",
        amount_cents=10000,
        description="LEGACY IGNORED CREDIT",
        row_hash="h-legacy-ignored-positive",
        statement_period="2026-06",
    )
    with engine.write_tx(app_env) as conn:
        repo_statements.set_match(
            conn,
            line_id,
            status="ignored",
            rationale="legacy operator ignore",
        )

    client = _close_recon_client()
    close_page = client.get("/close?month=2026-06")
    assert close_page.status_code == 200
    expected_href = (
        f"/recon?month=2026-06#positive-intake-{line_id}"
    )
    assert expected_href in close_page.text
    assert "LEGACY IGNORED CREDIT" in close_page.text

    recovery_page = client.get(expected_href)
    assert recovery_page.status_code == 200
    assert f'id="positive-intake-{line_id}"' in recovery_page.text
    assert "Create unknown transaction for this row" in recovery_page.text
    assert "Every matched statement credit" not in recovery_page.text
    with engine.read_conn(app_env) as conn:
        intake = next(
            item
            for item in positive_flows.list_positive_flow_intake(
                conn, "2026-06"
            )
            if int(item["line"]["statement_line_id"]) == line_id
        )

    recovered = client.post(
        f"/recon/positive/line/{line_id}/recover",
        data={
            "month": "2026-06",
            "evidence_fingerprint": intake["evidence_fingerprint"],
            "operation_key": "ui:recover-ignored-positive",
            "reason": "legacy ignored credit needs accounting review",
        },
        follow_redirects=False,
    )
    assert recovered.status_code == 303
    assert recovered.headers["location"] == "/recon?month=2026-06"

    queue = client.get("/recon?month=2026-06")
    assert f'id="positive-intake-{line_id}"' not in queue.text
    assert "Mark as earned income" in queue.text
    assert "Mark as interest income" in queue.text
    assert "Every matched statement credit" not in queue.text
    with engine.read_conn(app_env) as conn:
        line = conn.execute(
            """
            SELECT match_status, matched_transaction_id
            FROM statement_lines WHERE id=?
            """,
            (line_id,),
        ).fetchone()
        transaction = conn.execute(
            """
            SELECT flow_kind, amount_cents, source, source_document_id
            FROM transactions WHERE id=?
            """,
            (line["matched_transaction_id"],),
        ).fetchone()
        event = conn.execute(
            """
            SELECT action_kind, prior_statement_match_status
            FROM positive_flow_decision_events
            WHERE statement_line_id=?
            """,
            (line_id,),
        ).fetchone()
        assert line["match_status"] == "promoted"
        assert tuple(transaction) == (
            "unknown",
            10000,
            "statement",
            doc_id,
        )
        assert tuple(event) == ("recover_positive_line", "ignored")


def test_positive_flow_desktop_queue_accepts_and_undoes_income(app_env):
    doc_id = _insert_doc(name="positive-income.csv")
    line_id = _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-20",
        amount_cents=10000,
        description="AMBIGUOUS CREDIT",
        row_hash="h-positive-income",
        statement_period="2026-06",
    )
    client = _client()
    promoted = client.post(
        f"/recon/line/{line_id}/promote",
        data={"flow_kind": "unknown", "month": "2026-06"},
        follow_redirects=False,
    )
    assert promoted.status_code == 303

    page = client.get("/recon?month=2026-06")
    assert page.status_code == 200
    assert "Money in to explain" in page.text
    assert "Scores rank evidence only" in page.text
    assert "AMBIGUOUS CREDIT" in page.text
    assert "row confidence" in page.text
    assert "inspect source" in page.text
    assert "Mark as earned income" in page.text
    assert "Create for money-in review" not in page.text

    with engine.read_conn(app_env) as conn:
        review = positive_flows.list_positive_flow_reviews(conn, "2026-06")[0]
        subject_id = int(review["subject"]["transaction_id"])
        proposal = next(
            item
            for item in review["proposals"]
            if item["proposed_flow_kind"] == "income"
        )
        before_amount = int(review["subject"]["amount_cents"])
    accepted = client.post(
        f"/recon/positive/{subject_id}/accept-classification",
        data={
            "month": "2026-06",
            "flow_kind": "income",
            "evidence_fingerprint": proposal["evidence_fingerprint"],
            "operation_key": "ui:accept-income",
            "reason": "synthetic pay evidence",
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    with engine.read_conn(app_env) as conn:
        transaction = conn.execute(
            "SELECT amount_cents, flow_kind FROM transactions WHERE id=?",
            (subject_id,),
        ).fetchone()
        assert tuple(transaction) == (before_amount, "income")
        report = conn.execute(
            "SELECT income_cents FROM v_cashflow_monthly WHERE month='2026-06'"
        ).fetchone()
        assert report["income_cents"] == before_amount
        resolved = positive_flows.list_positive_flow_reviews(conn, "2026-06")[0]
        acceptance = resolved["acceptance"]

    accepted_page = client.get("/recon?month=2026-06")
    assert "Undo accounting decision" in accepted_page.text
    undone = client.post(
        f"/recon/positive/acceptance/{acceptance['event_id']}/undo",
        data={
            "month": "2026-06",
            "evidence_fingerprint": acceptance["undo_fingerprint"],
            "operation_key": "ui:undo-income",
            "reason": "synthetic correction",
        },
        follow_redirects=False,
    )
    assert undone.status_code == 303
    with engine.read_conn(app_env) as conn:
        transaction = conn.execute(
            "SELECT amount_cents, flow_kind FROM transactions WHERE id=?",
            (subject_id,),
        ).fetchone()
        assert tuple(transaction) == (before_amount, "unknown")


def test_positive_flow_queue_shows_pair_evidence_and_rejected_recovery(app_env):
    doc_id = _insert_doc(name="positive-refund.csv")
    line_id = _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-20",
        amount_cents=2500,
        description="MERCHANT CREDIT",
        row_hash="h-positive-refund",
        statement_period="2026-06",
    )
    client = _client()
    assert client.post(
        f"/recon/line/{line_id}/promote",
        data={"flow_kind": "unknown", "month": "2026-06"},
        follow_redirects=False,
    ).status_code == 303
    with engine.write_tx(app_env) as conn:
        category_id = repo_ledger.ensure_uncategorized(conn)
        candidate_id = repo_ledger.insert_transaction(
            conn,
            account_id=1,
            posted_on="2026-06-10",
            description="Merchant original purchase",
            counterparty="Merchant",
            amount_cents=-5000,
            source="manual",
            external_id="ui-refund-candidate",
            source_document_id=None,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        assert candidate_id is not None
        repo_ledger.insert_split(
            conn,
            transaction_id=candidate_id,
            category_id=category_id,
            amount_cents=-5000,
        )
        review = positive_flows.list_positive_flow_reviews(conn, "2026-06")[0]
        subject_id = int(review["subject"]["transaction_id"])
        proposal = next(
            item
            for item in review["proposals"]
            if item["proposed_flow_kind"] == "refund"
            and item["candidate_transaction_id"] == candidate_id
        )

    page = client.get("/recon?month=2026-06")
    assert "Pair as refund" in page.text
    assert "Not a refund" in page.text
    assert "other relationship interpretations for this transaction remain" in (
        page.text
    )
    assert "descriptor similarity" in page.text
    rejected = client.post(
        f"/recon/positive/{subject_id}/reject",
        data={
            "month": "2026-06",
            "proposal_key": proposal["proposal_key"],
            "evidence_fingerprint": proposal["evidence_fingerprint"],
            "operation_key": "ui:reject-refund",
            "reason": "merchant evidence differs",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    rejected_page = client.get("/recon?month=2026-06")
    assert "rejected suggestion" in rejected_page.text
    assert "Not a refund" in rejected_page.text
    assert "Pair as reimbursement" in rejected_page.text
    assert "Restore suggestion" in rejected_page.text


def test_positive_flow_split_candidate_shows_provenance_and_allocation_effect(
    app_env,
):
    doc_id = _insert_doc(name="positive-split-refund.csv")
    line_id = _insert_line(
        doc_id=doc_id,
        account_id=1,
        posted_on="2026-06-20",
        amount_cents=4000,
        description="HOTEL DINNER CREDIT",
        row_hash="h-positive-split-refund",
        statement_period="2026-06",
    )
    client = _client()
    assert client.post(
        f"/recon/line/{line_id}/promote",
        data={"flow_kind": "unknown", "month": "2026-06"},
        follow_redirects=False,
    ).status_code == 303
    with engine.write_tx(app_env) as conn:
        receipt_id = int(
            conn.execute(
                """
                INSERT INTO source_documents(
                  kind, original_name, storage_ref, sha256, mime_type, status
                ) VALUES (
                  'receipt', 'hotel-dinner-receipt.jpg',
                  'receipts/ui-split', ?, 'image/jpeg', 'processed'
                )
                """,
                ("a" * 64,),
            ).lastrowid
        )
        travel_id = int(
            conn.execute(
                """
                INSERT INTO categories(name, kind, brand_owner)
                VALUES ('UI Split Travel', 'expense', 'shared')
                """
            ).lastrowid
        )
        meals_id = int(
            conn.execute(
                """
                INSERT INTO categories(name, kind, brand_owner)
                VALUES ('UI Split Meals', 'expense', 'shared')
                """
            ).lastrowid
        )
        candidate_id = repo_ledger.insert_transaction(
            conn,
            account_id=1,
            posted_on="2026-06-10",
            description="Hotel and dinner",
            counterparty="Synthetic Hotel",
            amount_cents=-10000,
            source="receipt",
            external_id="ui-split-candidate",
            source_document_id=receipt_id,
            source_confidence=1.0,
            flow_kind="purchase",
        )
        assert candidate_id is not None
        repo_ledger.insert_split(
            conn,
            transaction_id=candidate_id,
            category_id=travel_id,
            amount_cents=-6000,
            memo="hotel",
        )
        repo_ledger.insert_split(
            conn,
            transaction_id=candidate_id,
            category_id=meals_id,
            amount_cents=-4000,
            memo="dinner",
        )
        review = positive_flows.list_positive_flow_reviews(conn, "2026-06")[0]
        subject_id = int(review["subject"]["transaction_id"])
        proposal = next(
            item
            for item in review["proposals"]
            if item["proposed_flow_kind"] == "refund"
            and item["candidate_transaction_id"] == candidate_id
        )

    page = client.get("/recon?month=2026-06")
    assert "hotel-dinner-receipt.jpg" in page.text
    assert "inspect candidate source document" in page.text
    assert "UI Split Travel" in page.text
    assert "UI Split Meals" in page.text
    assert "CAD" in page.text
    assert "Projected report effect" in page.text
    assert "neither source amount changes" in page.text
    assert 'name="selected_category_id"' in page.text
    assert "expense category this credit offsets" in page.text

    accepted = client.post(
        f"/recon/positive/{subject_id}/accept-pair",
        data={
            "month": "2026-06",
            "proposal_key": proposal["proposal_key"],
            "evidence_fingerprint": proposal["evidence_fingerprint"],
            "operation_key": "ui:accept-split-refund",
            "reason": "dinner credit offsets meals",
            "selected_category_id": str(meals_id),
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    with engine.read_conn(app_env) as conn:
        subject = conn.execute(
            "SELECT amount_cents, flow_kind FROM transactions WHERE id=?",
            (subject_id,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT amount_cents FROM transactions WHERE id=?",
            (candidate_id,),
        ).fetchone()
        split = conn.execute(
            """
            SELECT category_id, amount_cents
            FROM transaction_splits WHERE transaction_id=?
            """,
            (subject_id,),
        ).fetchone()
        assert tuple(subject) == (4000, "refund")
        assert candidate["amount_cents"] == -10000
        assert tuple(split) == (meals_id, 4000)
