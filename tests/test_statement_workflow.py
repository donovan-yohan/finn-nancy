from __future__ import annotations

import hashlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.close import period_exceptions
from app.db import (
    engine,
    repo_close,
    repo_documents,
    repo_period_policy,
    repo_statement_expectations as expectations,
    repo_statement_reviews,
    repo_statements,
)
from app.ingest.schemas import ExtractedStatement, StatementRow
from app.web.routes import close, recon, review


MONTH = "2026-06"


def _account(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str = "credit",
) -> int:
    cursor = conn.execute(
        """INSERT INTO accounts(name, institution, kind, currency)
           VALUES (?, 'Synthetic Bank', ?, 'CAD')""",
        (name, kind),
    )
    return int(cursor.lastrowid)


def _required_policy(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    cadence: str = "monthly",
    anchor_month: int | None = None,
) -> None:
    expectations.record_policy(
        conn,
        account_id=account_id,
        effective_from_month="2026-01",
        configuration_state="configured",
        requirement_mode="required",
        cadence=cadence,
        anchor_month=anchor_month,
        actor="test:policy",
        reason="synthetic workflow policy",
    )


def _statement(
    conn: sqlite3.Connection,
    *,
    account_id: int | None,
    period: str = MONTH,
    ref: str,
    status: str = "unmatched",
    pending: bool = False,
    posted_on: str = "2026-05-31",
    description: str = "Synthetic merchant",
    document_status: str = "processed",
) -> tuple[int, int]:
    cursor = conn.execute(
        """INSERT INTO source_documents(
             kind, original_name, storage_ref, sha256, mime_type, status
           )
           VALUES ('statement', ?, ?, ?, 'application/pdf', ?)""",
        (
            f"{ref}.pdf",
            f"synthetic/{ref}.pdf",
            f"sha-{ref}",
            document_status,
        ),
    )
    document_id = int(cursor.lastrowid)
    cursor = conn.execute(
        """INSERT INTO statement_lines(
             source_document_id, account_id, posted_on, raw_description,
             norm_merchant, amount_cents, currency, is_pending,
             statement_period, row_hash, match_status, flow_kind
           )
           VALUES (?,?,?,?,?,-1200,'CAD',?,?,?,?,'purchase')""",
        (
            document_id,
            account_id,
            posted_on,
            description,
            description.upper(),
            int(pending),
            period,
            f"row-{ref}",
            status,
        ),
    )
    return document_id, int(cursor.lastrowid)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(review.router)
    app.include_router(recon.router)
    app.include_router(close.router)
    return TestClient(app)


def _pending_statement_extraction(
    conn: sqlite3.Connection,
    *,
    ref: str,
    period: str = MONTH,
    posted_on: str = "2026-05-31",
    account_id: int | None = None,
) -> tuple[int, int, int]:
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
        institution="Synthetic Bank",
        account_hint="Workflow account",
        account_last4="4242",
        currency="CAD",
        statement_period=period,
        period_start_on="2026-05-01",
        period_end_on="2026-06-30",
        statement_issued_on="2026-07-01",
        opening_balance_cents=0,
        closing_balance_cents=1200,
        declared_page_count=1,
        declared_row_count=1,
        field_confidence=field_confidence,
        field_pages={field: 1 for field in field_confidence},
        rows=[
            StatementRow(
                posted_on=posted_on,
                description=f"{ref} merchant",
                amount_cents=-1200,
                page_number=1,
                field_confidence={
                    "posted_on": 0.96,
                    "description": 0.96,
                    "amount_cents": 0.96,
                },
            )
        ],
        confidence=0.96,
        observed_page_count=1,
        extracted_page_count=1,
    )
    raw = f"synthetic statement {ref}".encode()
    document_id = int(
        conn.execute(
            """INSERT INTO source_documents(
                 kind, original_name, storage_ref, sha256, mime_type, status
               )
               VALUES ('statement', ?, ?, ?, 'application/pdf', 'needs_review')""",
            (
                f"{ref}.pdf",
                f"synthetic/{ref}.pdf",
                hashlib.sha256(raw).hexdigest(),
            ),
        ).lastrowid
    )
    extraction_id = repo_documents.insert_extraction(
        conn,
        source_document_id=document_id,
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
        source_document_id=document_id,
        extraction_id=extraction_id,
        account_id=account_id,
        parsed=parsed,
        raw=raw,
        actor="test:extract",
    )
    repo_statements.stage_lines(
        conn,
        source_document_id=document_id,
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
    line_id = int(
        conn.execute(
            "SELECT id FROM statement_lines WHERE source_document_id=?",
            (document_id,),
        ).fetchone()[0]
    )
    return document_id, extraction_id, line_id


def _attach_and_review(
    conn: sqlite3.Connection, document_id: int
) -> sqlite3.Row:
    row, _ = expectations.attach_exact_document(
        conn,
        document_id,
        actor="test:ingest",
        reason="exact synthetic account and closing period",
    )
    assert row is not None
    row, _ = expectations.mark_document_reviewed(
        conn,
        document_id,
        actor="test:review",
        reason="synthetic statement identity approved",
    )
    assert row is not None
    return row


def _waive_other_required_accounts(
    conn: sqlite3.Connection, *, keep_account_id: int
) -> None:
    rows = expectations.prepare_period(
        conn,
        month=MONTH,
        actor="test:close",
        reason="prepare complete synthetic account-period matrix",
    )
    for row in rows:
        if (
            int(row["account_id"]) != keep_account_id
            and row["requirement_state"] == "required"
            and row["lifecycle_state"] == "expected"
        ):
            expectations.waive(
                conn,
                int(row["id"]),
                actor="test:waiver",
                reason="synthetic account has no statement this period",
            )


def test_exact_attachment_requires_one_account_and_declared_period(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, name="Exact card")
        _required_policy(conn, account_id)
        document_id, _ = _statement(
            conn,
            account_id=account_id,
            ref="exact",
        )

        row, attached = expectations.attach_exact_document(
            conn,
            document_id,
            actor="test:ingest",
            reason="exact account and closing period",
        )
        assert attached
        assert row is not None
        assert row["period_month"] == MONTH
        assert row["lifecycle_state"] == "received"

        audit_count = len(expectations.audit_for_expectation(conn, int(row["id"])))
        same, attached_again = expectations.attach_exact_document(
            conn,
            document_id,
            actor="test:retry",
            reason="duplicate workflow retry",
        )
        assert not attached_again
        assert same is not None and same["id"] == row["id"]
        assert len(expectations.audit_for_expectation(conn, int(row["id"]))) == audit_count

        ambiguous_id, _ = _statement(
            conn,
            account_id=None,
            ref="ambiguous",
        )
        unresolved, attached = expectations.attach_exact_document(
            conn,
            ambiguous_id,
            actor="test:ingest",
            reason="ambiguous evidence remains in review",
        )
        assert unresolved is None
        assert not attached
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_expectation_documents
               WHERE source_document_id=?""",
            (ambiguous_id,),
        ).fetchone()[0] == 0


def test_reconciliation_sync_blocks_pending_and_is_idempotent(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, name="Lifecycle card")
        _required_policy(conn, account_id)
        document_id, line_id = _statement(
            conn,
            account_id=account_id,
            ref="lifecycle",
            status="matched",
            pending=True,
        )
        row, _ = expectations.attach_exact_document(
            conn,
            document_id,
            actor="test:ingest",
            reason="statement received",
        )
        assert row is not None
        row, reviewed = expectations.mark_document_reviewed(
            conn,
            document_id,
            actor="test:review",
            reason="statement identity approved",
        )
        assert reviewed and row is not None
        assert row["lifecycle_state"] == "reviewed"

        unchanged, changed = expectations.sync_document_reconciliation(
            conn,
            document_id,
            actor="test:reconcile",
            reason="pending line cannot complete",
        )
        assert not changed
        assert unchanged is not None
        assert unchanged["lifecycle_state"] == "reviewed"

        conn.execute(
            "UPDATE statement_lines SET is_pending=0 WHERE id=?",
            (line_id,),
        )
        reconciled, changed = expectations.sync_document_reconciliation(
            conn,
            document_id,
            actor="test:reconcile",
            reason="every linked line is terminal",
        )
        assert changed
        assert reconciled is not None
        assert reconciled["lifecycle_state"] == "reconciled"
        audit_count = len(
            expectations.audit_for_expectation(conn, int(reconciled["id"]))
        )

        same, changed = expectations.sync_document_reconciliation(
            conn,
            document_id,
            actor="test:retry",
            reason="duplicate completion callback",
        )
        assert not changed
        assert same is not None and same["lifecycle_state"] == "reconciled"
        assert (
            len(expectations.audit_for_expectation(conn, int(reconciled["id"])))
            == audit_count
        )

        downgraded, changed = expectations.unreconcile_document(
            conn,
            document_id,
            actor="test:unreconcile",
            reason="operator reopened statement",
        )
        assert changed
        assert downgraded is not None
        assert downgraded["lifecycle_state"] == "reviewed"


def test_period_matrix_is_virtual_until_explicit_prepare(empty_db):
    with engine.write_tx(empty_db) as conn:
        required_id = _account(conn, name="Required")
        _required_policy(conn, required_id)
        _account(conn, name="Unconfigured")
        cash_id = _account(conn, name="Cash", kind="cash")
        quarterly_id = _account(conn, name="Quarterly")
        _required_policy(conn, quarterly_id, cadence="quarterly", anchor_month=1)

    with engine.read_conn(empty_db) as conn:
        matrix = expectations.period_matrix(conn, MONTH)
        by_name = {row["account_name"]: row for row in matrix}
        assert by_name["Required"]["requirement_state"] == "required"
        assert by_name["Required"]["lifecycle_state"] == "expected"
        assert by_name["Unconfigured"]["requirement_state"] == "unconfigured"
        assert by_name["Cash"]["requirement_state"] == "exempt"
        assert by_name["Quarterly"]["requirement_state"] == "not_due"
        assert all(not row["materialized"] for row in matrix)
        assert conn.execute(
            "SELECT COUNT(*) FROM account_statement_expectations"
        ).fetchone()[0] == 0

    with engine.write_tx(empty_db) as conn:
        first = expectations.prepare_period(
            conn,
            month=MONTH,
            actor="test:close",
            reason="explicit close preparation",
        )
        second = expectations.prepare_period(
            conn,
            month=MONTH,
            actor="test:retry",
            reason="duplicate close preparation",
        )
        assert [row["id"] for row in first] == [row["id"] for row in second]
        blockers = expectations.signoff_blockers(conn, MONTH)
        assert {row["account_name"] for row in blockers} == {
            "Required",
            "Unconfigured",
        }


def test_exact_attachment_uses_frozen_required_snapshot_after_policy_change(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, name="Frozen policy card")
        _required_policy(conn, account_id)
        frozen, created = expectations.prepare_account_period(
            conn,
            account_id=account_id,
            month=MONTH,
            actor="test:prepare",
            reason="freeze required June expectation",
        )
        assert created and frozen["requirement_state"] == "required"
        expectations.record_policy(
            conn,
            account_id=account_id,
            effective_from_month=MONTH,
            configuration_state="configured",
            requirement_mode="no_statement",
            cadence="none",
            actor="test:policy",
            reason="future evidence policy correction",
        )
        document_id, _ = _statement(
            conn,
            account_id=account_id,
            period=MONTH,
            ref="frozen-required",
        )

        attached, changed = expectations.attach_exact_document(
            conn,
            document_id,
            actor="test:ingest",
            reason="attach to frozen required account-period truth",
        )

        assert changed
        assert attached is not None
        assert attached["id"] == frozen["id"]
        assert attached["requirement_state"] == "required"
        assert attached["lifecycle_state"] == "received"


def test_detach_uses_closed_period_guard_and_last_source_regresses(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, name="Guarded")
        _required_policy(conn, account_id)
        document_id, _ = _statement(
            conn,
            account_id=account_id,
            ref="guarded",
        )
        row, _ = expectations.attach_exact_document(
            conn,
            document_id,
            actor="test:ingest",
            reason="statement received",
        )
        assert row is not None
        repo_close.mark_closed(conn, MONTH, reason="synthetic close")
        with pytest.raises(repo_close.MonthLockedError):
            expectations.detach_document_source(
                conn,
                document_id,
                actor="test:delete",
                reason="closed evidence cannot disappear",
            )

    with engine.write_tx(empty_db) as conn:
        repo_close.reopen(conn, MONTH, reason="correct evidence")
        row, detached = expectations.detach_document_source(
            conn,
            document_id,
            actor="test:delete",
            reason="operator rejected source",
        )
        assert detached
        assert row is not None
        assert row["lifecycle_state"] == "expected"


def test_recon_month_uses_declared_period_and_preserves_redirect(app_env):
    with engine.write_tx(app_env) as conn:
        june_doc, june_line = _statement(
            conn,
            account_id=1,
            period=MONTH,
            posted_on="2026-05-31",
            ref="declared-june",
            status="needs_review",
            description="JUNE DECLARED",
            document_status="needs_review",
        )
        _attach_and_review(conn, june_doc)
        may_doc, may_line = _statement(
            conn,
            account_id=1,
            period="2026-05",
            posted_on="2026-06-01",
            ref="declared-may",
            status="needs_review",
            description="MAY DECLARED",
            document_status="needs_review",
        )
        _attach_and_review(conn, may_doc)

    client = _client()
    page = client.get(f"/recon?month={MONTH}")
    assert page.status_code == 200
    assert "JUNE DECLARED" in page.text
    assert "MAY DECLARED" not in page.text

    out_of_scope = client.post(
        f"/recon/line/{may_line}/ignore",
        data={"month": MONTH},
        follow_redirects=False,
    )
    assert out_of_scope.status_code == 404

    ignored = client.post(
        f"/recon/line/{june_line}/ignore",
        data={"month": MONTH},
        follow_redirects=False,
    )
    assert ignored.status_code == 303
    assert ignored.headers["location"] == f"/recon?month={MONTH}"

    rerun = client.post(
        f"/recon/doc/{june_doc}/rerun",
        data={"month": MONTH},
        follow_redirects=False,
    )
    assert rerun.status_code == 303
    assert rerun.headers["location"] == f"/recon?month={MONTH}"

    unreconciled = client.post(
        f"/recon/doc/{june_doc}/unreconcile",
        data={"month": MONTH},
        follow_redirects=False,
    )
    assert unreconciled.status_code == 303
    assert unreconciled.headers["location"] == f"/recon?month={MONTH}"


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("/recon/line/999999/confirm", {"transaction_id": "1"}),
        ("/recon/line/999999/promote", {}),
        ("/recon/line/999999/ignore", {}),
        ("/recon/doc/999999/assign-account", {"account_id": "1"}),
        ("/recon/doc/999999/unreconcile", {}),
        ("/recon/doc/999999/rerun", {}),
    ],
)
def test_recon_mutations_require_valid_selected_month(app_env, path, extra):
    client = _client()
    assert client.post(path, data=extra).status_code == 422
    assert client.post(
        path,
        data={**extra, "month": "June 2026"},
    ).status_code == 400


def test_review_reject_and_delete_detach_and_respect_closed_guard(app_env):
    with engine.write_tx(app_env) as conn:
        reject_doc, reject_extraction, _ = _pending_statement_extraction(
            conn,
            ref="reject-open",
            account_id=1,
        )
        reject_expectation = _attach_and_review(conn, reject_doc)
        delete_doc, _, _ = _pending_statement_extraction(
            conn,
            ref="delete-open",
            account_id=1,
        )
        _attach_and_review(conn, delete_doc)

    client = _client()
    rejected = client.post(
        f"/review/{reject_extraction}/reject", follow_redirects=False
    )
    assert rejected.status_code == 303
    deleted = client.post(f"/review/doc/{delete_doc}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    with engine.read_conn(app_env) as conn:
        assert expectations.active_link_for_document(conn, reject_doc) is None
        assert (
            expectations.expectation(conn, int(reject_expectation["id"]))[
                "lifecycle_state"
            ]
            == "expected"
        )
        archived = repo_documents.get_document(conn, delete_doc)
        assert archived is not None and archived["status"] == "archived"
        assert conn.execute(
            """SELECT COUNT(*) FROM statement_lines
               WHERE source_document_id=? AND review_disposition='excluded'""",
            (delete_doc,),
        ).fetchone()[0] == 1

    with engine.write_tx(app_env) as conn:
        guarded_doc, guarded_extraction, _ = _pending_statement_extraction(
            conn,
            ref="reject-closed",
            account_id=1,
        )
        _attach_and_review(conn, guarded_doc)
        repo_close.mark_closed(conn, MONTH, reason="synthetic lock")

    guarded = client.post(
        f"/review/{guarded_extraction}/reject", follow_redirects=False
    )
    assert guarded.status_code == 409
    guarded_delete = client.post(
        f"/review/doc/{guarded_doc}/delete", follow_redirects=False
    )
    assert guarded_delete.status_code == 409
    with engine.read_conn(app_env) as conn:
        assert expectations.active_link_for_document(conn, guarded_doc) is not None
        assert repo_documents.get_document(conn, guarded_doc) is not None


def test_close_requires_explicit_audited_matrix_prepare_and_is_idempotent(app_env):
    client = _client()
    blocked = client.post(
        "/close/signoff",
        data={
            "month": MONTH,
            "actor": "test:operator",
            "reason": "attempt close before preparing evidence",
            "confirm_close": "1",
        },
        follow_redirects=False,
    )
    assert blocked.status_code == 400
    assert (
        blocked.json()["detail"]
        == "prepare the audited statement matrix before close review"
    )

    with engine.read_conn(app_env) as conn:
        expected_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM account_statement_expectations
                   WHERE period_month=?""",
                (MONTH,),
            ).fetchone()[0]
            == 0
        )
    refused_prepare = client.post(
        "/close/prepare",
        data={
            "month": MONTH,
            "actor": "test:operator",
            "reason": "prepare generated evidence",
        },
        follow_redirects=False,
    )
    assert refused_prepare.status_code == 400
    with engine.read_conn(app_env) as conn:
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM account_statement_expectations
                   WHERE period_month=?""",
                (MONTH,),
            ).fetchone()[0]
            == 0
        )

    audit_count = None
    for _ in range(2):
        prepared = client.post(
            "/close/prepare",
            data={
                "month": MONTH,
                "actor": "test:operator",
                "reason": "retry audited matrix preparation",
                "confirm_prepare": "1",
            },
            follow_redirects=False,
        )
        assert prepared.status_code == 303
        assert prepared.headers["location"] == f"/close?month={MONTH}"
        with engine.read_conn(app_env) as conn:
            current_audit_count = conn.execute(
                """SELECT COUNT(*) FROM statement_expectation_audit audit
                   JOIN account_statement_expectations expectation
                     ON expectation.id=audit.expectation_id
                   WHERE expectation.period_month=?""",
                (MONTH,),
            ).fetchone()[0]
        if audit_count is None:
            audit_count = current_audit_count
        else:
            assert current_audit_count == audit_count

    with engine.read_conn(app_env) as conn:
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM account_statement_expectations
                   WHERE period_month=?""",
                (MONTH,),
            ).fetchone()[0]
            == expected_count
        )
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM statement_expectation_audit audit
                   JOIN account_statement_expectations expectation
                     ON expectation.id=audit.expectation_id
                   WHERE expectation.period_month=?""",
                (MONTH,),
            ).fetchone()[0]
            == audit_count
        )


def test_missing_review_reconcile_close_workflow(app_env):
    with engine.write_tx(app_env) as conn:
        _waive_other_required_accounts(conn, keep_account_id=1)
        document_id, extraction_id, line_id = _pending_statement_extraction(
            conn,
            ref="end-to-end",
            account_id=None,
        )
        row = conn.execute(
            """SELECT * FROM account_statement_expectations
               WHERE account_id=1 AND period_month=?""",
            (MONTH,),
        ).fetchone()
        assert row is not None and row["lifecycle_state"] == "expected"

    client = _client()
    reviewed = client.post(
        f"/review/{extraction_id}/approve-statement",
        data={"account_id": "1"},
        follow_redirects=False,
    )
    assert reviewed.status_code == 303

    with engine.read_conn(app_env) as conn:
        linked = expectations.active_link_for_document(conn, document_id)
        assert linked is not None
        assert expectations.expectation(conn, int(linked["expectation_id"]))[
            "lifecycle_state"
        ] == "received"
        statement_review = repo_statement_reviews.get_for_document(
            conn, document_id
        )
        assert statement_review is not None
        review_revision = int(statement_review["revision"])

    approved = client.post(
        f"/review/statement/{document_id}/approve",
        data={
            "expected_revision": str(review_revision),
            "reason": "source pages and rows reviewed",
            "override_reason": "",
        },
        follow_redirects=False,
    )
    assert approved.status_code == 303

    with engine.read_conn(app_env) as conn:
        linked = expectations.active_link_for_document(conn, document_id)
        assert linked is not None
        assert expectations.expectation(conn, int(linked["expectation_id"]))[
            "lifecycle_state"
        ] == "reviewed"

    reconciled = client.post(
        f"/recon/line/{line_id}/ignore",
        data={"month": MONTH},
        follow_redirects=False,
    )
    assert reconciled.status_code == 303
    assert reconciled.headers["location"] == f"/recon?month={MONTH}"

    with engine.read_conn(app_env) as conn:
        linked = expectations.active_link_for_document(conn, document_id)
        assert linked is not None
        assert expectations.expectation(conn, int(linked["expectation_id"]))[
            "lifecycle_state"
        ] == "reconciled"

    with engine.write_tx(app_env) as conn:
        close_exceptions = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            MONTH,
            period_exceptions.collect_period_exceptions(conn, MONTH),
        )
        for index, exception in enumerate(close_exceptions):
            repo_period_policy.acknowledge_preclose_exception(
                conn,
                MONTH,
                exception,
                actor="test:close-operator",
                reason="reviewed remaining synthetic close exception",
                operation_key=f"test:statement-workflow:preack:{index}",
                evidence={"workflow": "statement review to close"},
            )

    signed_off = client.post(
        "/close/signoff",
        data={
            "month": MONTH,
            "actor": "test:close-operator",
            "reason": "all statement evidence explained",
            "confirm_close": "1",
            "operation_key": "test:statement-workflow:close",
        },
        follow_redirects=False,
    )
    assert signed_off.status_code == 303
    assert signed_off.headers["location"] == f"/close?month={MONTH}"
    with engine.read_conn(app_env) as conn:
        assert repo_close.is_month_locked(conn, MONTH)
