from __future__ import annotations

import sqlite3

import pytest

from app.db import engine, repo_close, repo_statement_expectations as expectations


def test_lifecycle_transition_table_is_explicit_and_closed():
    assert expectations.LEGAL_LIFECYCLE_TRANSITIONS == {
        (
            expectations.LifecycleState.EXPECTED,
            expectations.LifecycleEvent.DOCUMENT_ATTACHED,
        ): expectations.LifecycleState.RECEIVED,
        (
            expectations.LifecycleState.RECEIVED,
            expectations.LifecycleEvent.REVIEW_APPROVED,
        ): expectations.LifecycleState.REVIEWED,
        (
            expectations.LifecycleState.REVIEWED,
            expectations.LifecycleEvent.RECONCILED,
        ): expectations.LifecycleState.RECONCILED,
        (
            expectations.LifecycleState.RECONCILED,
            expectations.LifecycleEvent.UNRECONCILED,
        ): expectations.LifecycleState.REVIEWED,
        (
            expectations.LifecycleState.REVIEWED,
            expectations.LifecycleEvent.NEW_EVIDENCE,
        ): expectations.LifecycleState.RECEIVED,
        (
            expectations.LifecycleState.RECONCILED,
            expectations.LifecycleEvent.NEW_EVIDENCE,
        ): expectations.LifecycleState.RECEIVED,
        (
            expectations.LifecycleState.RECEIVED,
            expectations.LifecycleEvent.LAST_DOCUMENT_REMOVED,
        ): expectations.LifecycleState.EXPECTED,
        (
            expectations.LifecycleState.REVIEWED,
            expectations.LifecycleEvent.LAST_DOCUMENT_REMOVED,
        ): expectations.LifecycleState.EXPECTED,
        (
            expectations.LifecycleState.RECONCILED,
            expectations.LifecycleEvent.LAST_DOCUMENT_REMOVED,
        ): expectations.LifecycleState.EXPECTED,
    }


def _account(conn: sqlite3.Connection, *, name: str = "Synthetic Card", kind: str = "credit") -> int:
    cursor = conn.execute(
        """INSERT INTO accounts(name, institution, kind, currency)
           VALUES (?, 'Test Institution', ?, 'CAD')""",
        (name, kind),
    )
    return int(cursor.lastrowid)


def _required_policy(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    effective_from_month: str = "2026-01",
    cadence: str = "monthly",
    anchor_month: int | None = None,
    active_from: str | None = None,
    active_to: str | None = None,
):
    return expectations.record_policy(
        conn,
        account_id=account_id,
        effective_from_month=effective_from_month,
        configuration_state="configured",
        requirement_mode="required",
        cadence=cadence,
        anchor_month=anchor_month,
        active_from=active_from,
        active_to=active_to,
        actor="test:policy",
        reason="synthetic expectation fixture",
    )


def _prepare(
    conn: sqlite3.Connection, account_id: int, month: str = "2026-04"
) -> sqlite3.Row:
    row, created = expectations.prepare_account_period(
        conn,
        account_id=account_id,
        month=month,
        actor="test:prepare",
        reason="synthetic month preparation",
    )
    assert created
    return row


def _statement_document(
    conn: sqlite3.Connection,
    *,
    account_id: int | None,
    period: str,
    ref: str,
    match_status: str = "unmatched",
    is_pending: int = 0,
) -> tuple[int, int]:
    cursor = conn.execute(
        """INSERT INTO source_documents(
             kind, original_name, storage_ref, sha256, mime_type, status
           )
           VALUES ('statement', ?, ?, ?, 'application/pdf', 'processed')""",
        (f"{ref}.pdf", f"synthetic/{ref}", f"sha-{ref}"),
    )
    document_id = int(cursor.lastrowid)
    cursor = conn.execute(
        """INSERT INTO statement_lines(
             source_document_id, account_id, posted_on, raw_description,
             norm_merchant, amount_cents, currency, is_pending,
             statement_period, row_hash, match_status
           )
           VALUES (?,?,?,'Synthetic merchant','SYNTHETIC',-1234,'CAD',?,?,?,?)""",
        (
            document_id,
            account_id,
            f"{period}-15" if len(period) == 7 else "2026-04-15",
            is_pending,
            period,
            f"row-{ref}",
            match_status,
        ),
    )
    return document_id, int(cursor.lastrowid)


@pytest.mark.parametrize(
    ("policy", "month", "expected"),
    [
        (
            {
                "configuration_state": "unconfigured",
                "requirement_mode": None,
                "cadence": None,
                "anchor_month": None,
                "active_from": None,
                "active_to": None,
            },
            "2026-04",
            expectations.RequirementState.UNCONFIGURED,
        ),
        (
            {
                "configuration_state": "configured",
                "requirement_mode": "no_statement",
                "cadence": "none",
                "anchor_month": None,
                "active_from": None,
                "active_to": None,
            },
            "2026-04",
            expectations.RequirementState.EXEMPT,
        ),
        (
            {
                "configuration_state": "configured",
                "requirement_mode": "required",
                "cadence": "monthly",
                "anchor_month": None,
                "active_from": "2026-04-30",
                "active_to": "2026-04-30",
            },
            "2026-04",
            expectations.RequirementState.REQUIRED,
        ),
        (
            {
                "configuration_state": "configured",
                "requirement_mode": "required",
                "cadence": "monthly",
                "anchor_month": None,
                "active_from": "2026-05-01",
                "active_to": None,
            },
            "2026-04",
            expectations.RequirementState.NOT_DUE,
        ),
        (
            {
                "configuration_state": "configured",
                "requirement_mode": "required",
                "cadence": "quarterly",
                "anchor_month": 2,
                "active_from": None,
                "active_to": None,
            },
            "2026-05",
            expectations.RequirementState.REQUIRED,
        ),
        (
            {
                "configuration_state": "configured",
                "requirement_mode": "required",
                "cadence": "quarterly",
                "anchor_month": 2,
                "active_from": None,
                "active_to": None,
            },
            "2026-04",
            expectations.RequirementState.NOT_DUE,
        ),
        (
            {
                "configuration_state": "configured",
                "requirement_mode": "required",
                "cadence": "annual",
                "anchor_month": 11,
                "active_from": None,
                "active_to": None,
            },
            "2026-11",
            expectations.RequirementState.REQUIRED,
        ),
    ],
)
def test_requirement_derivation_keeps_policy_axes_orthogonal(policy, month, expected):
    assert expectations.requirement_for_month(policy, month) == expected


def test_policy_validation_rejects_illegal_combinations(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        with pytest.raises(ValueError, match="unconfigured"):
            expectations.record_policy(
                conn,
                account_id=account_id,
                effective_from_month="2026-01",
                configuration_state="unconfigured",
                requirement_mode="required",
                cadence="monthly",
                actor="test",
                reason="invalid mutant",
            )
        with pytest.raises(ValueError, match="anchor_month"):
            _required_policy(
                conn,
                account_id,
                cadence="quarterly",
                anchor_month=None,
            )
        with pytest.raises(ValueError, match="active_from"):
            _required_policy(
                conn,
                account_id,
                active_from="2026-05-01",
                active_to="2026-04-30",
            )


def test_prepare_is_idempotent_and_refresh_is_explicit(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        first_policy = _required_policy(conn, account_id)
        row = _prepare(conn, account_id)
        original_audit_count = len(expectations.audit_for_expectation(conn, row["id"]))

        same, created = expectations.prepare_account_period(
            conn,
            account_id=account_id,
            month="2026-04",
            actor="test:retry",
            reason="duplicate preparation",
        )
        assert not created
        assert same["id"] == row["id"]
        assert len(expectations.audit_for_expectation(conn, row["id"])) == original_audit_count

        replacement = expectations.record_policy(
            conn,
            account_id=account_id,
            effective_from_month="2026-04",
            configuration_state="configured",
            requirement_mode="no_statement",
            cadence="none",
            actor="test:policy",
            reason="account no longer issues statements",
        )
        unchanged = expectations.expectation(conn, row["id"])
        assert unchanged["policy_id"] == first_policy["id"]
        assert unchanged["requirement_state"] == "required"

        refreshed, changed = expectations.refresh_from_policy(
            conn,
            row["id"],
            actor="test:refresh",
            reason="explicitly accept the current policy",
        )
        assert changed
        assert refreshed["policy_id"] == replacement["id"]
        assert refreshed["requirement_state"] == "exempt"
        assert refreshed["lifecycle_state"] is None
        audit_count = len(expectations.audit_for_expectation(conn, row["id"]))

        _, changed_again = expectations.refresh_from_policy(
            conn,
            row["id"],
            actor="test:refresh",
            reason="idempotent retry",
        )
        assert not changed_again
        assert len(expectations.audit_for_expectation(conn, row["id"])) == audit_count


def test_document_lifecycle_multi_document_idempotency_and_regression(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        _required_policy(conn, account_id)
        row = _prepare(conn, account_id)
        first_doc, first_line = _statement_document(
            conn, account_id=account_id, period="2026-04", ref="first"
        )
        second_doc, second_line = _statement_document(
            conn, account_id=account_id, period="2026-04", ref="supplement"
        )

        row, attached = expectations.attach_document(
            conn,
            row["id"],
            first_doc,
            actor="test:attach",
            reason="primary statement",
            automatic=True,
        )
        assert attached
        assert row["lifecycle_state"] == "received"
        audit_count = len(expectations.audit_for_expectation(conn, row["id"]))

        row, attached_again = expectations.attach_document(
            conn,
            row["id"],
            first_doc,
            actor="test:retry",
            reason="duplicate transport retry",
            automatic=True,
        )
        assert not attached_again
        assert len(expectations.audit_for_expectation(conn, row["id"])) == audit_count

        row = expectations.mark_reviewed(
            conn,
            row["id"],
            actor="test:review",
            reason="statement identity approved",
        )
        assert row["lifecycle_state"] == "reviewed"

        row, attached_second = expectations.attach_document(
            conn,
            row["id"],
            second_doc,
            actor="test:attach",
            reason="supplemental statement pages",
            automatic=True,
        )
        assert attached_second
        assert row["lifecycle_state"] == "received"
        assert conn.execute(
            """SELECT COUNT(*)
               FROM statement_expectation_documents
               WHERE expectation_id=? AND status='active'""",
            (row["id"],),
        ).fetchone()[0] == 2

        row = expectations.mark_reviewed(
            conn,
            row["id"],
            actor="test:review",
            reason="supplement reviewed",
        )
        conn.execute(
            "UPDATE statement_lines SET match_status='matched' WHERE id IN (?,?)",
            (first_line, second_line),
        )
        row = expectations.mark_reconciled(
            conn,
            row["id"],
            actor="test:reconcile",
            reason="all linked lines have terminal dispositions",
        )
        assert row["lifecycle_state"] == "reconciled"

        row = expectations.unreconcile(
            conn,
            row["id"],
            actor="test:unreconcile",
            reason="operator reopened line review",
        )
        assert row["lifecycle_state"] == "reviewed"

        links = conn.execute(
            """SELECT id
               FROM statement_expectation_documents
               WHERE expectation_id=? ORDER BY id""",
            (row["id"],),
        ).fetchall()
        row, detached = expectations.detach_document(
            conn,
            links[0]["id"],
            actor="test:detach",
            reason="duplicate statement source",
        )
        assert detached
        assert row["lifecycle_state"] == "reviewed"
        row, _ = expectations.detach_document(
            conn,
            links[1]["id"],
            actor="test:detach",
            reason="remove last statement source",
        )
        assert row["lifecycle_state"] == "expected"


def test_automatic_attachment_rejects_ambiguous_or_mismatched_sources(empty_db):
    with engine.write_tx(empty_db) as conn:
        first_account = _account(conn, name="First")
        second_account = _account(conn, name="Second")
        _required_policy(conn, first_account)
        _required_policy(conn, second_account)
        first = _prepare(conn, first_account)
        second = _prepare(conn, second_account)

        mismatched, _ = _statement_document(
            conn, account_id=second_account, period="2026-04", ref="wrong-account"
        )
        with pytest.raises(ValueError, match="account"):
            expectations.attach_document(
                conn,
                first["id"],
                mismatched,
                actor="test",
                reason="must remain in review",
                automatic=True,
            )

        wrong_period, _ = _statement_document(
            conn, account_id=first_account, period="2026-03", ref="wrong-period"
        )
        with pytest.raises(ValueError, match="period"):
            expectations.attach_document(
                conn,
                first["id"],
                wrong_period,
                actor="test",
                reason="must remain in review",
                automatic=True,
            )

        ambiguous, _ = _statement_document(
            conn, account_id=None, period="2026-04", ref="ambiguous"
        )
        with pytest.raises(ValueError, match="exactly one account"):
            expectations.attach_document(
                conn,
                first["id"],
                ambiguous,
                actor="test",
                reason="must remain in review",
                automatic=True,
            )

        valid, _ = _statement_document(
            conn, account_id=first_account, period="2026-04", ref="one-source"
        )
        expectations.attach_document(
            conn,
            first["id"],
            valid,
            actor="test",
            reason="valid first link",
            automatic=True,
        )
        with pytest.raises(ValueError, match="another account-period"):
            expectations.attach_document(
                conn,
                second["id"],
                valid,
                actor="test",
                reason="source uniqueness mutant",
            )


def test_waiver_restore_and_closed_period_guards_are_audited(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        _required_policy(conn, account_id)
        row = _prepare(conn, account_id)

        with pytest.raises(ValueError, match="reason"):
            expectations.waive(conn, row["id"], actor="test", reason=" ")
        row = expectations.waive(
            conn,
            row["id"],
            actor="test:waiver",
            reason="issuer confirmed no partial-month statement",
        )
        assert row["requirement_state"] == "waived"
        assert row["lifecycle_state"] is None
        row = expectations.restore_waiver(
            conn,
            row["id"],
            actor="test:restore",
            reason="statement became available",
        )
        assert row["requirement_state"] == "required"
        assert row["lifecycle_state"] == "expected"
        assert [
            audit["event_kind"]
            for audit in expectations.audit_for_expectation(conn, row["id"])
        ][-2:] == ["requirement_waived", "waiver_restored"]

        document_id, _ = _statement_document(
            conn, account_id=account_id, period="2026-04", ref="closed-retry"
        )
        row, _ = expectations.attach_document(
            conn,
            row["id"],
            document_id,
            actor="test",
            reason="received before close",
            automatic=True,
        )
        audit_count = len(expectations.audit_for_expectation(conn, row["id"]))
        repo_close.mark_closed(conn, "2026-04", reason="synthetic close")
        _, prepared_again = expectations.prepare_account_period(
            conn,
            account_id=account_id,
            month="2026-04",
            actor="test",
            reason="closed retry is a no-op",
        )
        assert not prepared_again
        _, attached_again = expectations.attach_document(
            conn,
            row["id"],
            document_id,
            actor="test",
            reason="closed transport retry is a no-op",
            automatic=True,
        )
        assert not attached_again
        assert len(expectations.audit_for_expectation(conn, row["id"])) == audit_count
        with pytest.raises(repo_close.MonthLockedError):
            expectations.waive(
                conn,
                row["id"],
                actor="test",
                reason="closed periods fail",
            )
        with pytest.raises(repo_close.MonthLockedError):
            expectations.record_policy(
                conn,
                account_id=account_id,
                effective_from_month="2026-04",
                configuration_state="configured",
                requirement_mode="required",
                cadence="monthly",
                actor="test",
                reason="retroactive closed mutation",
            )


def test_reconciliation_requires_review_and_terminal_nonpending_lines(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        _required_policy(conn, account_id)
        row = _prepare(conn, account_id)
        document_id, line_id = _statement_document(
            conn, account_id=account_id, period="2026-04", ref="terminal"
        )
        row, _ = expectations.attach_document(
            conn,
            row["id"],
            document_id,
            actor="test",
            reason="statement received",
            automatic=True,
        )
        with pytest.raises(ValueError, match="invalid from lifecycle received"):
            expectations.mark_reconciled(
                conn,
                row["id"],
                actor="test",
                reason="review cannot be skipped",
            )
        row = expectations.mark_reviewed(
            conn,
            row["id"],
            actor="test",
            reason="identity reviewed",
        )
        conn.execute(
            "UPDATE statement_lines SET statement_period='2026-03' WHERE id=?",
            (line_id,),
        )
        with pytest.raises(ValueError, match="expectation period"):
            expectations.mark_reconciled(
                conn,
                row["id"],
                actor="test",
                reason="identity drift cannot reconcile",
            )
        conn.execute(
            "UPDATE statement_lines SET statement_period='2026-04' WHERE id=?",
            (line_id,),
        )
        with pytest.raises(ValueError, match="terminal disposition"):
            expectations.mark_reconciled(
                conn,
                row["id"],
                actor="test",
                reason="unmatched line cannot reconcile",
            )
        conn.execute(
            "UPDATE statement_lines SET match_status='matched', is_pending=1 WHERE id=?",
            (line_id,),
        )
        with pytest.raises(ValueError, match="terminal disposition"):
            expectations.mark_reconciled(
                conn,
                row["id"],
                actor="test",
                reason="pending line cannot reconcile",
            )
        conn.execute(
            "UPDATE statement_lines SET is_pending=0 WHERE id=?",
            (line_id,),
        )
        row = expectations.mark_reconciled(
            conn,
            row["id"],
            actor="test",
            reason="terminal evidence",
        )
        assert row["lifecycle_state"] == "reconciled"


def test_database_guards_make_state_and_audit_mutations_fail_closed(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        policy = _required_policy(conn, account_id)
        row = _prepare(conn, account_id)
        document_id, _ = _statement_document(
            conn, account_id=account_id, period="2026-04", ref="guard"
        )
        row, _ = expectations.attach_document(
            conn,
            row["id"],
            document_id,
            actor="test",
            reason="guard fixture",
            automatic=True,
        )
        link_id = conn.execute(
            """SELECT id FROM statement_expectation_documents
               WHERE expectation_id=? AND status='active'""",
            (row["id"],),
        ).fetchone()[0]
        audit_id = expectations.audit_for_expectation(conn, row["id"])[0]["id"]

        with pytest.raises(sqlite3.IntegrityError, match="audit"):
            conn.execute(
                """UPDATE account_statement_expectations
                   SET lifecycle_state='reviewed'
                   WHERE id=?""",
                (row["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE statement_expectation_audit SET reason='rewritten' WHERE id=?",
                (audit_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM statement_expectation_audit WHERE id=?",
                (audit_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE account_statement_policies SET cadence='annual' WHERE id=?",
                (policy["id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            conn.execute(
                "DELETE FROM statement_expectation_documents WHERE id=?",
                (link_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM source_documents WHERE id=?", (document_id,))
        with pytest.raises(sqlite3.IntegrityError, match="statement"):
            conn.execute(
                "UPDATE source_documents SET kind='receipt' WHERE id=?",
                (document_id,),
            )
