from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import engine, repo_period_policy


def test_period_policy_exposes_typed_locked_error():
    error = repo_period_policy.PeriodLockedError("2026-07")
    assert error.month == "2026-07"
    assert isinstance(error, ValueError)


def _exception(**overrides):
    value = {
        "exception_type": "missing_statement",
        "subject_kind": "account_period",
        "subject_id": "checking:2026-07",
        "affected_ids": {"account_id": 7, "month": "2026-07"},
        "evidence": {"requirement": "required", "document_id": None},
        "reason": "required checking statement is missing",
        "resolution_href": "/close?month=2026-07#statements",
    }
    value.update(overrides)
    return value


def _acknowledge(
    conn,
    month: str,
    exceptions,
    *,
    operation_prefix: str,
):
    return [
        repo_period_policy.acknowledge_preclose_exception(
            conn,
            month,
            exception,
            actor="human:owner",
            reason=f"reviewed {exception['exception_type']} before close",
            operation_key=f"{operation_prefix}:{index}",
            evidence={"review_surface": "test"},
        )
        for index, exception in enumerate(exceptions)
    ]


def test_close_state_is_derived_from_typed_exceptions(empty_db):
    with engine.write_tx(empty_db) as conn:
        clean = repo_period_policy.close_period(
            conn,
            "2026-06",
            snapshot={"month": "2026-06", "income_cents": 120_00},
            exceptions=[],
            actor="human:owner",
            reason="monthly sign-off",
            operation_key="close:2026-06:v1",
        )
        exceptions = [_exception()]
        _acknowledge(
            conn,
            "2026-07",
            exceptions,
            operation_prefix="preack:2026-07:derived",
        )
        with_exceptions = repo_period_policy.close_period(
            conn,
            "2026-07",
            snapshot={"month": "2026-07", "income_cents": 90_00},
            exceptions=exceptions,
            actor="human:owner",
            reason="close with known missing statement",
            operation_key="close:2026-07:v1",
        )

    assert clean["close_state"] == "clean_closed"
    assert clean["exception_count"] == 0
    assert with_exceptions["close_state"] == "closed_with_exceptions"
    assert with_exceptions["exception_count"] == 1
    with engine.read_conn(empty_db) as conn:
        assert repo_period_policy.current_state(conn, "2026-06") == "clean_closed"
        assert (
            repo_period_policy.current_state(conn, "2026-07")
            == "closed_with_exceptions"
        )
        exception = conn.execute(
            """SELECT exception_type, subject_id
               FROM period_close_exceptions
               WHERE cycle_id=?""",
            (with_exceptions["cycle_id"],),
        ).fetchone()
    assert tuple(exception) == ("missing_statement", "checking:2026-07")


def test_exception_close_requires_every_exact_current_preclose_ack(empty_db):
    missing = _exception(subject_id="checking:2026-08")
    category = _exception(
        exception_type="unconfirmed_merchant_category",
        subject_kind="transaction",
        subject_id="transaction:42",
        affected_ids={"transaction_id": 42, "month": "2026-08"},
        evidence={"merchant_status": "unconfirmed"},
        reason="merchant and category require confirmation",
        resolution_href="/review?month=2026-08",
    )
    with engine.write_tx(empty_db) as conn:
        _acknowledge(
            conn,
            "2026-08",
            [missing],
            operation_prefix="preack:2026-08:partial",
        )
        with pytest.raises(
            repo_period_policy.PeriodAcknowledgementRequired
        ) as error:
            repo_period_policy.close_period(
                conn,
                "2026-08",
                snapshot={"month": "2026-08"},
                exceptions=[missing, category],
                actor="human:owner",
                reason="attempt incomplete exception close",
                operation_key="close:2026-08:partial",
            )
        assert len(error.value.exception_tokens) == 1
        assert repo_period_policy.current_state(conn, "2026-08") == "open"
        assert conn.execute(
            "SELECT COUNT(*) FROM period_close_cycles"
        ).fetchone()[0] == 0

        category_ack = _acknowledge(
            conn,
            "2026-08",
            [category],
            operation_prefix="preack:2026-08:complete",
        )[0]
        repo_period_policy.withdraw_preclose_acknowledgement(
            conn,
            int(category_ack["id"]),
            actor="human:owner",
            reason="need to recheck category evidence",
            operation_key="preack:2026-08:withdraw",
        )
        with pytest.raises(repo_period_policy.PeriodAcknowledgementRequired):
            repo_period_policy.close_period(
                conn,
                "2026-08",
                snapshot={"month": "2026-08"},
                exceptions=[missing, category],
                actor="human:owner",
                reason="attempt withdrawn exception close",
                operation_key="close:2026-08:withdrawn",
            )

        _acknowledge(
            conn,
            "2026-08",
            [category],
            operation_prefix="preack:2026-08:replacement",
        )
        closed = repo_period_policy.close_period(
            conn,
            "2026-08",
            snapshot={"month": "2026-08"},
            exceptions=[missing, category],
            actor="human:owner",
            reason="all exact exceptions reviewed",
            operation_key="close:2026-08:complete",
        )
        assert closed["close_state"] == "closed_with_exceptions"
        assert conn.execute(
            "SELECT COUNT(*) FROM v_period_close_current_acknowledgements"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM v_period_close_current_preacknowledgements"
        ).fetchone()[0] == 0


def test_changed_exception_evidence_invalidates_preclose_ack(empty_db):
    original = _exception(subject_id="checking:2026-09")
    changed = _exception(
        subject_id="checking:2026-09",
        evidence={"requirement": "required", "document_id": 91},
    )
    with engine.write_tx(empty_db) as conn:
        _acknowledge(
            conn,
            "2026-09",
            [original],
            operation_prefix="preack:2026-09:original",
        )
        previews = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            "2026-09",
            [changed],
        )
        assert len(previews) == 1
        assert previews[0]["is_acknowledged"] is False
        with pytest.raises(repo_period_policy.PeriodAcknowledgementRequired):
            repo_period_policy.close_period(
                conn,
                "2026-09",
                snapshot={"month": "2026-09"},
                exceptions=[changed],
                actor="human:owner",
                reason="stale acknowledgement must not close",
                operation_key="close:2026-09:stale",
            )


def test_database_rejects_exception_close_event_without_frozen_ack(empty_db):
    digest = "0" * 64
    with engine.write_tx(empty_db) as conn:
        cycle_id = conn.execute(
            """INSERT INTO period_close_cycles(
                 cycle_key, month, cycle_number, created_by, reason,
                 operation_key
               ) VALUES (?,?,?,?,?,?)""",
            (
                "test:cycle:unacknowledged",
                "2026-10",
                1,
                "human:owner",
                "direct SQL invariant probe",
                "test:cycle:unacknowledged:operation",
            ),
        ).lastrowid
        conn.execute(
            """INSERT INTO period_close_exceptions(
                 exception_key, cycle_id, lineage_key, exception_type,
                 subject_kind, subject_id, affected_ids_json, evidence_json,
                 evidence_digest, reason, created_by, operation_key
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "test:exception:unacknowledged",
                cycle_id,
                "test:lineage:unacknowledged",
                "missing_statement",
                "account_period",
                "checking:2026-10",
                '{"account_id":7}',
                '{"required":true}',
                digest,
                "required statement is missing",
                "human:owner",
                "test:exception:unacknowledged:operation",
            ),
        )
        snapshot_id = conn.execute(
            """INSERT INTO period_close_snapshots(
                 snapshot_key, cycle_id, snapshot_number, close_state,
                 exception_count, request_exception_digest, exception_digest,
                 request_snapshot_digest, snapshot_json, snapshot_digest,
                 created_by, reason, operation_key
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "test:snapshot:unacknowledged",
                cycle_id,
                1,
                "closed_with_exceptions",
                1,
                digest,
                digest,
                digest,
                '{"month":"2026-10"}',
                digest,
                "human:owner",
                "direct SQL invariant probe",
                "test:snapshot:unacknowledged:operation",
            ),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="durable exception"):
            conn.execute(
                """INSERT INTO period_close_events(
                     event_key, cycle_id, event_kind, from_state, to_state,
                     snapshot_id, actor, reason, affected_ids_json,
                     evidence_json, evidence_digest, operation_key
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "test:event:unacknowledged",
                    cycle_id,
                    "closed_with_exceptions",
                    "open",
                    "closed_with_exceptions",
                    snapshot_id,
                    "human:owner",
                    "direct SQL invariant probe",
                    '{"period_month":"2026-10"}',
                    "{}",
                    digest,
                    "test:event:unacknowledged:operation",
                ),
            )


def test_closed_write_rejects_or_atomically_reopens_with_audited_override(empty_db):
    with engine.write_tx(empty_db) as conn:
        repo_period_policy.close_period(
            conn,
            "2026-07",
            snapshot={"month": "2026-07"},
            exceptions=[],
            actor="human:owner",
            reason="monthly sign-off",
            operation_key="close:2026-07:guard",
        )

    with pytest.raises(repo_period_policy.PeriodLockedError):
        with engine.write_tx(empty_db) as conn:
            repo_period_policy.guard_months(conn, ["2026-07"])

    with engine.write_tx(empty_db) as conn:
        guarded = repo_period_policy.guard_months(
            conn,
            ["2026-07"],
            override=True,
            actor="human:owner",
            reason="correct duplicate transaction",
            operation_key="write:dedupe:42",
            affected_ids={"transaction_id": 42},
            evidence={"request": "ui:ledger"},
        )
        assert guarded == ["2026-07"]
        assert repo_period_policy.current_state(conn, "2026-07") == "reopened"
        assert conn.execute(
            "SELECT COUNT(*) FROM period_write_overrides"
        ).fetchone()[0] == 1
        repeated = repo_period_policy.override_periods(
            conn,
            ["2026-07"],
            actor="human:owner",
            reason="correct duplicate transaction",
            operation_key="write:dedupe:42",
            affected_ids={"transaction_id": 42},
            evidence={"request": "ui:ledger"},
        )
        assert len(repeated) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM period_write_overrides"
        ).fetchone()[0] == 1

    with engine.read_conn(empty_db) as conn:
        history = conn.execute(
            """SELECT close_state, is_current
               FROM v_period_close_snapshot_history
               WHERE month='2026-07'"""
        ).fetchall()
        current = conn.execute(
            """SELECT *
               FROM v_current_period_close_snapshot
               WHERE month='2026-07'"""
        ).fetchone()
    assert [tuple(row) for row in history] == [("clean_closed", 0)]
    assert current is None


def test_reclose_appends_new_cycle_and_preserves_old_snapshot(empty_db):
    with engine.write_tx(empty_db) as conn:
        first = repo_period_policy.close_period(
            conn,
            "2026-05",
            snapshot={"version": 1},
            exceptions=[],
            actor="human:owner",
            reason="first close",
            operation_key="close:2026-05:v1",
        )
        repo_period_policy.reopen_period(
            conn,
            "2026-05",
            actor="human:owner",
            reason="late receipt",
            operation_key="reopen:2026-05:v1",
            affected_ids={"receipt_id": 9},
        )
        exceptions = [_exception(subject_id="savings:2026-05")]
        _acknowledge(
            conn,
            "2026-05",
            exceptions,
            operation_prefix="preack:2026-05:v2",
        )
        second = repo_period_policy.close_period(
            conn,
            "2026-05",
            snapshot={"version": 2},
            exceptions=exceptions,
            actor="human:owner",
            reason="second close",
            operation_key="close:2026-05:v2",
        )

    assert second["cycle_id"] != first["cycle_id"]
    with engine.read_conn(empty_db) as conn:
        rows = conn.execute(
            """SELECT snapshot_number, close_state, snapshot_json, is_current
               FROM v_period_close_snapshot_history
               WHERE month='2026-05'
               ORDER BY snapshot_number"""
        ).fetchall()
    assert [
        (
            int(row["snapshot_number"]),
            str(row["close_state"]),
            json.loads(str(row["snapshot_json"]))["version"],
            int(row["is_current"]),
        )
        for row in rows
    ] == [
        (1, "clean_closed", 1, 0),
        (2, "closed_with_exceptions", 2, 1),
    ]


def test_acknowledgement_never_resolves_or_upgrades_close(empty_db):
    with engine.write_tx(empty_db) as conn:
        exceptions = [_exception()]
        preclose_acknowledgements = _acknowledge(
            conn,
            "2026-07",
            exceptions,
            operation_prefix="preack:2026-07:ack",
        )
        snapshot = repo_period_policy.close_period(
            conn,
            "2026-07",
            snapshot={"month": "2026-07"},
            exceptions=exceptions,
            actor="human:owner",
            reason="known exception",
            operation_key="close:2026-07:ack",
        )
        exception_id = conn.execute(
            "SELECT id FROM period_close_exceptions WHERE cycle_id=?",
            (snapshot["cycle_id"],),
        ).fetchone()[0]

    with engine.read_conn(empty_db) as conn:
        assert (
            repo_period_policy.current_state(conn, "2026-07")
            == "closed_with_exceptions"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM v_period_close_active_exceptions"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM v_period_close_current_acknowledgements"
        ).fetchone()[0] == 1
        frozen = conn.execute(
            """SELECT exception_id, source_preclose_acknowledgement_id
               FROM v_period_close_current_acknowledgements"""
        ).fetchone()
        assert tuple(frozen) == (
            exception_id,
            int(preclose_acknowledgements[0]["id"]),
        )
        authoritative = json.loads(str(snapshot["snapshot_json"]))
        assert authoritative["exception_acknowledgement_count"] == 1


def test_unresolved_exception_carries_forward_and_blocks_clean_reclose(empty_db):
    with engine.write_tx(empty_db) as conn:
        first_exceptions = [_exception(subject_id="checking:2026-02")]
        _acknowledge(
            conn,
            "2026-02",
            first_exceptions,
            operation_prefix="preack:2026-02:v1",
        )
        first = repo_period_policy.close_period(
            conn,
            "2026-02",
            snapshot={"version": 1},
            exceptions=first_exceptions,
            actor="human:owner",
            reason="close with known gap",
            operation_key="close:2026-02:v1",
        )
        first_exception = conn.execute(
            "SELECT id FROM period_close_exceptions WHERE cycle_id=?",
            (first["cycle_id"],),
        ).fetchone()[0]
        repo_period_policy.reopen_period(
            conn,
            "2026-02",
            actor="human:owner",
            reason="attempt to complete evidence",
            operation_key="reopen:2026-02:v1",
        )
        carried_preview = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            "2026-02",
            [],
        )
        _acknowledge(
            conn,
            "2026-02",
            carried_preview,
            operation_prefix="preack:2026-02:v2",
        )
        second = repo_period_policy.close_period(
            conn,
            "2026-02",
            snapshot={"version": 2},
            exceptions=[],
            actor="human:owner",
            reason="reclose",
            operation_key="close:2026-02:v2",
        )
        carried = conn.execute(
            """SELECT prior_exception_id, exception_type
               FROM period_close_exceptions
               WHERE cycle_id=?""",
            (second["cycle_id"],),
        ).fetchone()

    assert second["close_state"] == "closed_with_exceptions"
    assert second["exception_count"] == 1
    assert tuple(carried) == (first_exception, "missing_statement")
    second_snapshot = json.loads(str(second["snapshot_json"]))
    assert second_snapshot["close_state"] == "closed_with_exceptions"
    assert second_snapshot["exception_count"] == 1
    assert second_snapshot["exception_type_counts"] == {"missing_statement": 1}


def test_explicit_resolution_allows_clean_reclose_but_reversal_carries_forward(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        first_exceptions = [_exception(subject_id="checking:2025-12")]
        _acknowledge(
            conn,
            "2025-12",
            first_exceptions,
            operation_prefix="preack:2025-12:v1",
        )
        first = repo_period_policy.close_period(
            conn,
            "2025-12",
            snapshot={"version": 1},
            exceptions=first_exceptions,
            actor="human:owner",
            reason="close with known gap",
            operation_key="close:2025-12:v1",
        )
        exception_id = conn.execute(
            "SELECT id FROM period_close_exceptions WHERE cycle_id=?",
            (first["cycle_id"],),
        ).fetchone()[0]
        repo_period_policy.reopen_period(
            conn,
            "2025-12",
            actor="human:owner",
            reason="statement arrived",
            operation_key="reopen:2025-12:v1",
        )
        resolution = repo_period_policy.resolve_exception(
            conn,
            exception_id,
            actor="human:owner",
            reason="statement reviewed and reconciled",
            operation_key="resolve:2025-12:statement",
            evidence={"statement_review_id": 12},
        )
        clean = repo_period_policy.close_period(
            conn,
            "2025-12",
            snapshot={"version": 2},
            exceptions=[],
            actor="human:owner",
            reason="clean reclose",
            operation_key="close:2025-12:v2",
        )
        assert clean["close_state"] == "clean_closed"
        repo_period_policy.reopen_period(
            conn,
            "2025-12",
            actor="human:owner",
            reason="resolution evidence invalidated",
            operation_key="reopen:2025-12:v2",
        )
        repo_period_policy.reverse_resolution(
            conn,
            int(resolution["id"]),
            actor="human:owner",
            reason="statement was for the wrong account",
            operation_key="reverse-resolution:2025-12:statement",
        )
        reversed_preview = repo_period_policy.apply_preclose_acknowledgements(
            conn,
            "2025-12",
            [],
        )
        _acknowledge(
            conn,
            "2025-12",
            reversed_preview,
            operation_prefix="preack:2025-12:v3",
        )
        third = repo_period_policy.close_period(
            conn,
            "2025-12",
            snapshot={"version": 3},
            exceptions=[],
            actor="human:owner",
            reason="reclose after correction",
            operation_key="close:2025-12:v3",
        )

    assert third["close_state"] == "closed_with_exceptions"
    assert third["exception_count"] == 1


def test_append_only_records_reject_update_and_delete(empty_db):
    with engine.write_tx(empty_db) as conn:
        snapshot = repo_period_policy.close_period(
            conn,
            "2026-04",
            snapshot={"month": "2026-04"},
            exceptions=[],
            actor="human:owner",
            reason="signed off",
            operation_key="close:2026-04:immutable",
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with engine.write_tx(empty_db) as conn:
            conn.execute(
                "UPDATE period_close_snapshots SET reason='changed' WHERE id=?",
                (snapshot["id"],),
            )


def test_close_operation_key_is_idempotent_but_not_reusable(empty_db):
    kwargs = {
        "snapshot": {"month": "2026-03"},
        "exceptions": [],
        "actor": "human:owner",
        "reason": "signed off",
        "operation_key": "close:2026-03:v1",
    }
    with engine.write_tx(empty_db) as conn:
        first = repo_period_policy.close_period(conn, "2026-03", **kwargs)
        again = repo_period_policy.close_period(conn, "2026-03", **kwargs)
        assert again["id"] == first["id"]
        with pytest.raises(repo_period_policy.PeriodOperationConflict):
            repo_period_policy.close_period(
                conn,
                "2026-03",
                **{**kwargs, "snapshot": {"month": "2026-03", "changed": True}},
            )


def test_close_operation_key_rejects_same_count_different_exception_payload(empty_db):
    kwargs = {
        "snapshot": {"month": "2026-01"},
        "exceptions": [_exception(subject_id="checking:2026-01")],
        "actor": "human:owner",
        "reason": "known gap",
        "operation_key": "close:2026-01:v1",
    }
    with engine.write_tx(empty_db) as conn:
        _acknowledge(
            conn,
            "2026-01",
            kwargs["exceptions"],
            operation_prefix="preack:2026-01:v1",
        )
        repo_period_policy.close_period(conn, "2026-01", **kwargs)
        with pytest.raises(repo_period_policy.PeriodOperationConflict):
            repo_period_policy.close_period(
                conn,
                "2026-01",
                **{
                    **kwargs,
                    "exceptions": [_exception(subject_id="savings:2026-01")],
                },
            )
