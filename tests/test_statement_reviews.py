from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import fitz
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import (
    engine,
    repo_documents,
    repo_statement_expectations,
    repo_statement_reviews,
    repo_statements,
)
from app.ingest.schemas import ExtractedStatement, StatementRow
from app.web.routes.review import router


def _pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic statement page 1 of 1")
    return document.tobytes()


def _parsed(*, confidence: float = 0.96, truncated: bool = False):
    fields = {
        "period_start_on": confidence,
        "period_end_on": confidence,
        "statement_issued_on": confidence,
        "opening_balance_cents": confidence,
        "closing_balance_cents": confidence,
        "currency": confidence,
        "account_fingerprint": confidence,
        "zero_activity": confidence,
    }
    return ExtractedStatement(
        institution="Example",
        account_hint="Credit",
        account_last4="4242",
        currency="CAD",
        statement_period="2026-06",
        period_start_on="2026-06-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=10000,
        closing_balance_cents=11000,
        declared_page_count=1,
        declared_row_count=1,
        field_confidence=fields,
        field_pages={key: 1 for key in fields},
        rows=[
            StatementRow(
                posted_on="2026-06-12",
                description="Synthetic shop",
                amount_cents=-1000,
                page_number=1,
                field_confidence={
                    "posted_on": confidence,
                    "description": confidence,
                    "amount_cents": confidence,
                },
            )
        ],
        confidence=confidence,
        observed_page_count=1,
        extracted_page_count=1,
        extraction_truncated=truncated,
    )


def _zero_activity_parsed():
    parsed = _parsed()
    return parsed.model_copy(
        update={
            "opening_balance_cents": 10000,
            "closing_balance_cents": 10000,
            "declared_row_count": 0,
            "zero_activity": True,
            "rows": [],
        }
    )


def _review(db_path, parsed, *, source_suffix: str = ""):
    raw = _pdf()
    with engine.write_tx(db_path) as conn:
        account = conn.execute(
            """INSERT INTO accounts(
                 name, institution, kind, currency, external_ref
               ) VALUES ('Synthetic Card', 'Example', 'credit', 'CAD', 'card:4242')"""
        )
        account_id = int(account.lastrowid)
        doc_id = repo_documents.insert_source_document(
            conn,
            kind="statement",
            original_name=f"synthetic{source_suffix}.pdf",
            storage_ref=f"blobs/synthetic{source_suffix}",
            sha256=hashlib.sha256(raw).hexdigest(),
            mime_type="application/pdf",
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
        envelope = repo_statement_reviews.create_from_extraction(
            conn,
            source_document_id=doc_id,
            extraction_id=extraction_id,
            account_id=account_id,
            parsed=parsed,
            raw=raw,
            actor="test:extract",
        )
        repo_statements.stage_lines(
            conn,
            source_document_id=doc_id,
            account_id=account_id,
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
            account_id=account_id,
            anchors=envelope.page_anchor_ids,
        )
        return doc_id, int(envelope.review["id"])


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_complete_statement_has_typed_anchored_evidence(empty_db):
    _, review_id = _review(empty_db, _parsed())
    with engine.read_conn(empty_db) as conn:
        result = repo_statement_reviews.completeness(conn, review_id)
        assert result["complete"] is True
        assert result["hard_blockers"] == []
        assert result["review_blockers"] == []
        review = repo_statement_reviews.get_review(conn, review_id)
        assert review["period_end_on"] == "2026-06-30"
        assert review["period_month"] == "2026-06"
        assert len(review["account_fingerprint"]) == 64


def test_truncated_or_low_confidence_cannot_auto_approve(empty_db):
    _, review_id = _review(empty_db, _parsed(confidence=0.0, truncated=True))
    with engine.read_conn(empty_db) as conn:
        result = repo_statement_reviews.completeness(conn, review_id)
        assert result["complete"] is False
        assert "source_truncated" in result["review_blockers"]
        assert "row_low_confidence" in result["review_blockers"]
        assert "currency_low_confidence" in result["review_blockers"]


def test_untrusted_counts_and_confidence_fail_into_review_not_ingest_error(
    empty_db,
):
    parsed = _parsed().model_copy(
        update={
            "declared_page_count": 10**100,
            "declared_row_count": 10**100,
            "rows": [
                _parsed().rows[0].model_copy(
                    update={
                        "field_confidence": {
                            "posted_on": 0.99,
                            "description": 0.99,
                            "amount_cents": float("nan"),
                        }
                    }
                )
            ],
        }
    )

    doc_id, review_id = _review(empty_db, parsed)

    with engine.read_conn(empty_db) as conn:
        review = repo_statement_reviews.get_review(conn, review_id)
        line = repo_statements.lines_for_document(conn, doc_id)[0]
        result = repo_statement_reviews.completeness(conn, review_id)
        assert review["declared_page_count"] is None
        assert review["declared_row_count"] is None
        assert line["row_confidence"] == 0.0
        assert "declared_page_count_missing" in result["review_blockers"]
        assert "declared_row_count_missing" in result["review_blockers"]
        assert "row_low_confidence" in result["review_blockers"]
        assert conn.execute(
            """SELECT json_valid(original_value_json)
               FROM statement_field_evidence
               WHERE statement_line_id=? AND field_name='row_snapshot'""",
            (int(line["id"]),),
        ).fetchone()[0] == 1


def test_account_currency_mismatch_is_a_hard_approval_blocker(empty_db):
    _, review_id = _review(empty_db, _parsed())
    with engine.write_tx(empty_db) as conn:
        review = repo_statement_reviews.get_review(conn, review_id)
        conn.execute(
            "UPDATE accounts SET currency='USD' WHERE id=?",
            (int(review["account_id"]),),
        )
        result = repo_statement_reviews.completeness(conn, review_id)
        assert "account_unsupported_currency" in result["hard_blockers"]
        assert "account_currency_mismatch" in result["hard_blockers"]


def test_row_correction_exclusion_and_restore_are_audited(empty_db):
    doc_id, review_id = _review(empty_db, _parsed())
    with engine.write_tx(empty_db) as conn:
        line = repo_statements.lines_for_document(conn, doc_id)[0]
        corrected = repo_statement_reviews.correct_row(
            conn,
            int(line["id"]),
            expected_review_revision=1,
            expected_line_revision=1,
            actor="test:reviewer",
            reason="source page shows corrected amount",
            source_page=1,
            posted_on="2026-06-12",
            description="Synthetic shop corrected",
            amount_cents=-900,
            currency="CAD",
            balance_cents=None,
            is_pending=False,
        )
        assert corrected["review_revision"] == 2
        excluded = repo_statement_reviews.exclude_row(
            conn,
            int(line["id"]),
            expected_review_revision=1,
            expected_line_revision=2,
            actor="test:reviewer",
            reason="summary row was not a transaction",
        )
        assert excluded["review_disposition"] == "excluded"
        restored = repo_statement_reviews.restore_row(
            conn,
            int(line["id"]),
            expected_review_revision=1,
            expected_line_revision=3,
            actor="test:reviewer",
            reason="operator restored row after checking page",
        )
        assert restored["review_disposition"] == "active"
        events = [
            row["event_kind"]
            for row in conn.execute(
                """SELECT event_kind FROM statement_review_audit
                   WHERE statement_line_id=? ORDER BY id""",
                (int(line["id"]),),
            )
        ]
        assert events == ["row_corrected", "row_excluded", "row_restored"]


def test_excluded_tombstone_frees_dedupe_identity_and_tracks_lifecycle(
    empty_db,
):
    parsed = _parsed()
    doc_id, review_id = _review(empty_db, parsed)
    with engine.write_tx(empty_db) as conn:
        review = repo_statement_reviews.get_review(conn, review_id)
        repo_statement_expectations.record_policy(
            conn,
            account_id=int(review["account_id"]),
            effective_from_month="2026-06",
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            anchor_month=None,
            actor="test:policy",
            reason="exercise active evidence lifecycle",
        )
        _, attached = repo_statement_expectations.attach_exact_document(
            conn,
            doc_id,
            actor="test:review",
            reason="initial exact row identity",
        )
        assert attached is True
        line = repo_statements.lines_for_document(conn, doc_id)[0]

        excluded = repo_statement_reviews.exclude_row(
            conn,
            int(line["id"]),
            expected_review_revision=1,
            expected_line_revision=1,
            actor="test:reviewer",
            reason="temporarily exclude source row",
        )
        assert excluded["row_hash"].startswith(f"excluded:{int(line['id'])}:")
        assert (
            repo_statement_expectations.active_link_for_document(conn, doc_id)
            is None
        )
        restored = repo_statement_reviews.restore_row(
            conn,
            int(line["id"]),
            expected_review_revision=1,
            expected_line_revision=2,
            actor="test:reviewer",
            reason="source confirms transaction row",
        )
        assert restored["row_hash"] == line["row_hash"]
        assert (
            repo_statement_expectations.active_link_for_document(conn, doc_id)
            is not None
        )
        excluded = repo_statement_reviews.exclude_row(
            conn,
            int(line["id"]),
            expected_review_revision=1,
            expected_line_revision=3,
            actor="test:reviewer",
            reason="exclude before source re-export",
        )
        anchor = conn.execute(
            """SELECT anchor.id
               FROM statement_source_anchors anchor
               JOIN statement_review_pages page ON page.id=anchor.page_id
               WHERE anchor.statement_review_id=? AND page.page_number=1""",
            (review_id,),
        ).fetchone()
        staged = repo_statements.stage_lines(
            conn,
            source_document_id=doc_id,
            account_id=int(review["account_id"]),
            parsed=parsed,
            row_anchor_ids=[int(anchor["id"])],
        )
        assert staged == {"staged": 1, "duplicates": 0}
        with pytest.raises(
            ValueError, match="restored row duplicates another active"
        ):
            repo_statement_reviews.restore_row(
                conn,
                int(line["id"]),
                expected_review_revision=1,
                expected_line_revision=int(excluded["review_revision"]),
                actor="test:reviewer",
                reason="stale tombstone must not duplicate re-export",
            )
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_review_audit
               WHERE statement_line_id=? AND event_kind='row_restored'""",
            (int(line["id"]),),
        ).fetchone()[0] == 1


def test_reexport_advances_duplicate_occurrence_after_second_tombstone(
    empty_db,
):
    first = _parsed().rows[0]
    parsed = _parsed().model_copy(
        update={"declared_row_count": 2, "rows": [first, first.model_copy()]}
    )
    doc_id, review_id = _review(empty_db, parsed)
    with engine.write_tx(empty_db) as conn:
        lines = repo_statements.lines_for_document(conn, doc_id)
        assert len(lines) == 2
        second = lines[1]
        repo_statement_reviews.exclude_row(
            conn,
            int(second["id"]),
            expected_review_revision=1,
            expected_line_revision=1,
            actor="test:reviewer",
            reason="tombstone the second identical source charge",
        )
        anchor = conn.execute(
            """SELECT anchor.id
               FROM statement_source_anchors anchor
               JOIN statement_review_pages page ON page.id=anchor.page_id
               WHERE anchor.statement_review_id=? AND page.page_number=1""",
            (review_id,),
        ).fetchone()

        staged = repo_statements.stage_lines(
            conn,
            source_document_id=doc_id,
            account_id=int(lines[0]["account_id"]),
            parsed=parsed,
            row_anchor_ids=[int(anchor["id"]), int(anchor["id"])],
        )

        assert staged == {"staged": 1, "duplicates": 1}
        assert len(repo_statements.lines_for_document(conn, doc_id)) == 2


def test_review_evidence_edges_cannot_cross_source_scope(empty_db):
    doc_one, review_one = _review(empty_db, _parsed())
    doc_two, review_two = _review(
        empty_db, _parsed(), source_suffix="-second"
    )
    with engine.write_tx(empty_db) as conn:
        line_one = repo_statements.lines_for_document(conn, doc_one)[0]
        line_two = repo_statements.lines_for_document(conn, doc_two)[0]
        anchor_two = conn.execute(
            """SELECT id FROM statement_source_anchors
               WHERE statement_review_id=? ORDER BY id LIMIT 1""",
            (review_two,),
        ).fetchone()
        extraction_one = repo_statement_reviews.get_review(
            conn, review_one
        )["extraction_id"]

        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            conn.execute(
                """INSERT INTO statement_field_evidence(
                     evidence_key, statement_review_id, statement_line_id,
                     extraction_id, field_name, original_value_json,
                     confidence, source_anchor_id, origin
                   )
                   VALUES (?,?,?,?,?,'{}',1.0,?,'manual')""",
                (
                    "test:cross-source-field",
                    review_one,
                    int(line_one["id"]),
                    int(extraction_one),
                    "cross_source",
                    int(anchor_two["id"]),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            conn.execute(
                """INSERT INTO statement_review_audit(
                     operation_key, statement_review_id, statement_line_id,
                     event_kind, actor, reason
                   )
                   VALUES (?,?,?,?,?,?)""",
                (
                    "test:cross-source-audit",
                    review_one,
                    int(line_two["id"]),
                    "row_corrected",
                    "test:scope",
                    "must remain within the review source",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="scope mismatch"):
            conn.execute(
                """INSERT INTO statement_lines(
                     source_document_id, account_id, posted_on,
                     raw_description, amount_cents, statement_period,
                     row_hash, source_anchor_id
                   )
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    doc_one,
                    int(line_one["account_id"]),
                    "2026-06-20",
                    "Cross source",
                    -50,
                    "2026-06",
                    "cross-source-row",
                    int(anchor_two["id"]),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                """UPDATE statement_lines SET source_document_id=?
                   WHERE id=?""",
                (doc_two, int(line_one["id"])),
            )


def test_statement_review_page_connects_source_metadata_rows_and_gate(app_env):
    doc_id, _ = _review(app_env, _parsed())

    response = _client().get(f"/review/statement/{doc_id}")

    assert response.status_code == 200
    assert 'title="Source statement: synthetic.pdf"' in response.text
    assert "Actual closing date" in response.text
    assert "2026-06-30" in response.text
    assert "Synthetic shop" in response.text
    assert "source evidence" in response.text
    assert "Source and rows are complete" in response.text
    assert f'action="/review/statement/{doc_id}/approve"' in response.text


def test_statement_review_has_accessible_responsive_browser_contract(app_env):
    doc_id, _ = _review(app_env, _parsed())
    response = _client().get(f"/review/statement/{doc_id}")
    css = (
        Path(__file__).parents[1] / "app" / "web" / "static" / "app.css"
    ).read_text()

    assert 'aria-labelledby="statement-gate-title"' in response.text
    assert 'aria-label="Statement pages"' in response.text
    assert 'title="Source statement: synthetic.pdf"' in response.text
    assert 'target="statement-source"' in response.text
    assert "#page=1" in response.text
    assert "statement-workspace" in response.text
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 620px)" in css
    assert ".statement-workspace" in css
    assert "grid-template-columns: 1fr;" in css
    assert ".statement-source-frame" in css


def test_statement_review_routes_audit_metadata_and_row_corrections(app_env):
    doc_id, review_id = _review(app_env, _parsed())
    client = _client()
    with engine.read_conn(app_env) as conn:
        initial = repo_statement_reviews.get_review(conn, review_id)
        initial_account_id = int(initial["account_id"])

    metadata = client.post(
        f"/review/statement/{doc_id}/metadata",
        data={
            "expected_revision": "1",
            "source_page": "1",
            "account_id": str(initial_account_id),
            "period_start_on": "2026-06-01",
            "period_end_on": "2026-06-30",
            "statement_issued_on": "2026-07-02",
            "opening_balance": "100.00",
            "closing_balance": "110.00",
            "currency": "CAD",
            "activity_kind": "transactions",
            "declared_page_count": "1",
            "declared_row_count": "1",
            "reason": "confirmed metadata against source",
        },
        follow_redirects=False,
    )
    assert metadata.status_code == 303

    with engine.read_conn(app_env) as conn:
        review = repo_statement_reviews.get_review(conn, review_id)
        line = repo_statements.lines_for_document(conn, doc_id)[0]
        account_id = int(review["account_id"])
    assert review["revision"] == 2
    assert review["statement_issued_on"] == "2026-07-02"

    corrected = client.post(
        f"/review/statement/{doc_id}/row/{line['id']}",
        data={
            "expected_review_revision": "2",
            "expected_line_revision": "1",
            "source_page": "1",
            "posted_on": "2026-06-13",
            "description": "Synthetic shop corrected",
            "amount": "-10.00",
            "currency": "CAD",
            "balance": "",
            "reason": "corrected against source row",
        },
        follow_redirects=False,
    )
    assert corrected.status_code == 303
    excluded = client.post(
        f"/review/statement/{doc_id}/row/{line['id']}/exclude",
        data={
            "expected_review_revision": "2",
            "expected_line_revision": "2",
            "reason": "source row is a non-transaction summary",
        },
        follow_redirects=False,
    )
    assert excluded.status_code == 303
    restored = client.post(
        f"/review/statement/{doc_id}/row/{line['id']}/restore",
        data={
            "expected_review_revision": "2",
            "expected_line_revision": "3",
            "reason": "source row is a real transaction",
        },
        follow_redirects=False,
    )
    assert restored.status_code == 303

    with engine.read_conn(app_env) as conn:
        current = repo_statements.lines_for_document(conn, doc_id)[0]
        assert current["account_id"] == account_id
        assert current["posted_on"] == "2026-06-13"
        assert current["review_disposition"] == "active"
        assert [
            row["event_kind"]
            for row in conn.execute(
                """SELECT event_kind FROM statement_review_audit
                   WHERE statement_review_id=? ORDER BY id""",
                (review_id,),
            )
        ][-4:] == [
            "account_resolved",
            "row_corrected",
            "row_excluded",
            "row_restored",
        ]


def test_statement_review_approval_advances_received_then_reviewed_and_asserts_closing_date(
    app_env,
):
    doc_id, review_id = _review(app_env, _parsed())
    with engine.write_tx(app_env) as conn:
        review = repo_statement_reviews.get_review(conn, review_id)
        repo_statement_expectations.record_policy(
            conn,
            account_id=int(review["account_id"]),
            effective_from_month="2026-06",
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            anchor_month=None,
            actor="test:policy",
            reason="statement review acceptance policy",
        )
        expectation, attached = (
            repo_statement_expectations.attach_exact_document(
                conn,
                doc_id,
                actor="test:review",
                reason="typed statement identity received",
            )
        )
        assert attached is True
        assert expectation is not None
        assert expectation["lifecycle_state"] == "received"

    response = _client().post(
        f"/review/statement/{doc_id}/approve",
        data={
            "expected_revision": "1",
            "reason": "source and rows verified",
            "override_reason": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        approved = repo_statement_reviews.get_review(conn, review_id)
        assert approved["review_state"] == "approved"
        link = repo_statement_expectations.active_link_for_document(conn, doc_id)
        assert link is not None and link["lifecycle_state"] == "reviewed"
        assertion = conn.execute(
            """SELECT * FROM account_balance_assertions
               WHERE source_document_id=?""",
            (doc_id,),
        ).fetchone()
        assert assertion is not None
        assert assertion["asof_date"] == "2026-06-30"
        assert conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE source_document_id=? AND type='reconcile_document'""",
            (doc_id,),
        ).fetchone()[0] == 1
    with engine.write_tx(app_env) as conn:
        repeated, _ = repo_statement_reviews.approve(
            conn,
            review_id,
            expected_revision=1,
            actor="test:review",
            reason="browser retried the same approval",
        )
        assert repeated["review_state"] == "approved"
        assert repeated["revision"] == 2
        assert conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE source_document_id=? AND type='reconcile_document'""",
            (doc_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_review_audit
               WHERE statement_review_id=?
                 AND event_kind IN (
                   'review_approved', 'review_approved_with_override'
                 )""",
            (review_id,),
        ).fetchone()[0] == 1


def test_statement_review_archive_preserves_and_excludes_row_evidence(app_env):
    doc_id, review_id = _review(app_env, _parsed())

    response = _client().post(
        f"/review/statement/{doc_id}/archive",
        data={"reason": "duplicate source upload"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with engine.read_conn(app_env) as conn:
        doc = repo_documents.get_document(conn, doc_id)
        assert doc is not None and doc["status"] == "archived"
        line = conn.execute(
            "SELECT * FROM statement_lines WHERE source_document_id=?",
            (doc_id,),
        ).fetchone()
        assert line is not None and line["review_disposition"] == "excluded"
        assert conn.execute(
            """SELECT COUNT(*) FROM v_statement_coverage_lines
               WHERE source_document_id=?""",
            (doc_id,),
        ).fetchone()[0] == 0
        events = {
            row["event_kind"]
            for row in conn.execute(
                """SELECT event_kind FROM statement_review_audit
                   WHERE statement_review_id=?""",
                (review_id,),
            )
        }
        assert {"row_excluded", "review_archived"} <= events


def test_explicit_zero_activity_uses_balance_proof_without_reconcile_job(app_env):
    doc_id, review_id = _review(app_env, _zero_activity_parsed())
    with engine.write_tx(app_env) as conn:
        review = repo_statement_reviews.get_review(conn, review_id)
        repo_statement_expectations.record_policy(
            conn,
            account_id=int(review["account_id"]),
            effective_from_month="2026-06",
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            anchor_month=None,
            actor="test:policy",
            reason="zero-activity acceptance policy",
        )
        result = repo_statement_reviews.completeness(conn, review_id)
        assert result["complete"] is True
        approved, _ = repo_statement_reviews.approve(
            conn,
            review_id,
            expected_revision=int(review["revision"]),
            actor="test:review",
            reason="explicit zero activity and unchanged balance verified",
        )
        assert approved["review_state"] == "approved"

    with engine.read_conn(app_env) as conn:
        link = repo_statement_expectations.active_link_for_document(conn, doc_id)
        assert link is not None and link["lifecycle_state"] == "reconciled"
        assert conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE source_document_id=? AND type='reconcile_document'""",
            (doc_id,),
        ).fetchone()[0] == 0
        assertion = conn.execute(
            """SELECT * FROM account_balance_assertions
               WHERE source_document_id=?""",
            (doc_id,),
        ).fetchone()
        assert assertion is not None
        assert assertion["asof_date"] == "2026-06-30"
