from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

import pytest

from app.db import (
    engine,
    repo_admin,
    repo_close,
    repo_ledger,
    repo_merchant_knowledge,
    repo_period_policy,
    repo_structured_imports,
)
from app.db.repo_merchant_knowledge import Evidence
from app.ingest.promote import promote_receipt
from app.ingest.schemas import ExtractedReceipt


LOCK_MESSAGE = "period policy rejects write to closed period"


def _account(conn, name: str = "Test") -> int:
    return int(
        conn.execute(
            """INSERT INTO accounts(name, institution, kind, currency)
               VALUES (?, 'Test', 'cash', 'CAD')""",
            (name,),
        ).lastrowid
    )


def _transaction(
    conn,
    *,
    account_id: int,
    posted_on: str,
    amount_cents: int = -1000,
    flow_kind: str = "purchase",
    external_id: str,
) -> tuple[int, int]:
    category_id = repo_ledger.ensure_uncategorized(conn)
    transaction_id = int(
        repo_ledger.insert_transaction(
            conn,
            account_id=account_id,
            posted_on=posted_on,
            description="Synthetic",
            counterparty="Synthetic",
            amount_cents=amount_cents,
            source="test",
            external_id=external_id,
            source_document_id=None,
            source_confidence=1.0,
            flow_kind=flow_kind,
        )
    )
    split_id = repo_ledger.insert_split(
        conn,
        transaction_id=transaction_id,
        category_id=category_id,
        amount_cents=amount_cents,
    )
    return transaction_id, split_id


def _close(conn, month: str) -> None:
    repo_period_policy.close_period(
        conn,
        month,
        snapshot={"month": month},
        exceptions=[],
        actor="test:operator",
        reason="writer matrix lock",
        operation_key=f"writer-matrix:close:{month}",
    )


def _reopen(conn, month: str) -> None:
    repo_period_policy.reopen_period(
        conn,
        month,
        actor="test:operator",
        reason="writer matrix reopen",
        operation_key=f"writer-matrix:reopen:{month}",
        affected_ids={"month": month},
    )


def _assert_rejected(db_path, sql: str, args: tuple = ()) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=LOCK_MESSAGE):
        with engine.write_tx(db_path) as conn:
            conn.execute(sql, args)


def _assert_call_rejected(
    db_path,
    callback: Callable[[sqlite3.Connection], object],
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=LOCK_MESSAGE):
        with engine.write_tx(db_path) as conn:
            callback(conn)


def _document(
    conn,
    *,
    tag: str,
    kind: str = "statement",
    status: str = "processed",
) -> tuple[int, str]:
    source_sha256 = f"{abs(hash(tag)):064x}"[-64:]
    document_id = int(
        conn.execute(
            """INSERT INTO source_documents(
                 kind, original_name, storage_ref, sha256, mime_type, status
               ) VALUES (?, ?, ?, ?, 'application/octet-stream', ?)""",
            (kind, f"{tag}.dat", f"local:{tag}", source_sha256, status),
        ).lastrowid
    )
    return document_id, source_sha256


def _statement_line(
    conn,
    *,
    document_id: int,
    account_id: int,
    month: str,
    tag: str,
    amount_cents: int = -100,
) -> int:
    return int(
        conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description,
                 amount_cents, statement_period, row_hash
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                document_id,
                account_id,
                f"{month}-05",
                f"SYNTHETIC {tag}",
                amount_cents,
                month,
                f"writer-matrix:{tag}",
            ),
        ).lastrowid
    )


def _statement_review_chain(
    conn,
    *,
    account_id: int,
    month: str,
    tag: str,
) -> dict[str, int | str]:
    document_id, source_sha256 = _document(conn, tag=tag)
    line_id = _statement_line(
        conn,
        document_id=document_id,
        account_id=account_id,
        month=month,
        tag=tag,
    )
    review_id = int(
        conn.execute(
            """INSERT INTO statement_reviews(
                 source_document_id, account_id, period_start_on, period_end_on,
                 period_month, currency, activity_kind
               ) VALUES (?, ?, ?, ?, ?, 'CAD', 'transactions')""",
            (
                document_id,
                account_id,
                f"{month}-01",
                f"{month}-28",
                month,
            ),
        ).lastrowid
    )
    page_id = int(
        conn.execute(
            """INSERT INTO statement_review_pages(
                 statement_review_id, page_number, source_sha256, page_sha256
               ) VALUES (?, 1, ?, ?)""",
            (review_id, source_sha256, "a" * 64),
        ).lastrowid
    )
    anchor_id = int(
        conn.execute(
            """INSERT INTO statement_source_anchors(
                 statement_review_id, page_id, locator_kind, locator_json,
                 source_sha256, created_by
               ) VALUES (?, ?, 'page', '{"page":1}', ?, 'test:operator')""",
            (review_id, page_id, source_sha256),
        ).lastrowid
    )
    conn.execute(
        """INSERT INTO statement_field_evidence(
             evidence_key, statement_review_id, statement_line_id, field_name,
             original_value_json, confidence, source_anchor_id, origin
           ) VALUES (?, ?, ?, 'amount_cents', '-100', 1.0, ?, 'manual')""",
        (f"writer-matrix:evidence:{tag}", review_id, line_id, anchor_id),
    )
    conn.execute(
        """INSERT INTO statement_review_audit(
             operation_key, statement_review_id, event_kind,
             old_values_json, new_values_json, actor, reason
           ) VALUES (?, ?, 'review_created', '{}', '{}',
                     'test:operator', 'synthetic review')""",
        (f"writer-matrix:review-audit:{tag}", review_id),
    )
    return {
        "document_id": document_id,
        "source_sha256": source_sha256,
        "line_id": line_id,
        "review_id": review_id,
        "page_id": page_id,
        "anchor_id": anchor_id,
    }


def test_open_and_explicitly_reopened_months_accept_period_bound_writes(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        open_transaction, open_split = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-03",
            external_id="open-before-close",
        )
        _close(conn, "2026-07")
        _reopen(conn, "2026-07")

        reopened_transaction, reopened_split = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-04",
            external_id="reopened-write",
        )
        conn.execute(
            "UPDATE transactions SET description='reopened edit' WHERE id=?",
            (open_transaction,),
        )
        conn.execute(
            "UPDATE transaction_splits SET memo='reopened split' WHERE id=?",
            (open_split,),
        )

    with engine.read_conn(empty_db) as conn:
        assert repo_period_policy.current_state(conn, "2026-07") == "reopened"
        assert conn.execute(
            "SELECT description FROM transactions WHERE id=?",
            (open_transaction,),
        ).fetchone()[0] == "reopened edit"
        assert conn.execute(
            "SELECT memo FROM transaction_splits WHERE id=?",
            (open_split,),
        ).fetchone()[0] == "reopened split"
        assert conn.execute(
            "SELECT transaction_id FROM transaction_splits WHERE id=?",
            (reopened_split,),
        ).fetchone()[0] == reopened_transaction


def test_transaction_and_split_insert_update_delete_cover_old_and_new_months(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        closed_transaction, closed_split = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-03",
            external_id="closed",
        )
        open_transaction, open_split = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-08-03",
            external_id="open",
        )
        category_id = repo_ledger.ensure_uncategorized(conn)
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        """INSERT INTO transactions(
             account_id, posted_on, description, amount_cents, source,
             external_id, flow_kind
           ) VALUES (?, '2026-07-09', 'blocked', -10, 'test', 'blocked', 'purchase')""",
        (account_id,),
    )
    _assert_rejected(
        empty_db,
        "UPDATE transactions SET description='blocked' WHERE id=?",
        (closed_transaction,),
    )
    _assert_rejected(
        empty_db,
        "UPDATE transactions SET posted_on='2026-07-12' WHERE id=?",
        (open_transaction,),
    )
    _assert_rejected(
        empty_db,
        "UPDATE transactions SET posted_on='2026-08-12' WHERE id=?",
        (closed_transaction,),
    )
    _assert_rejected(
        empty_db,
        "DELETE FROM transactions WHERE id=?",
        (closed_transaction,),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO transaction_splits(
             transaction_id, category_id, amount_cents, memo
           ) VALUES (?, ?, -1, 'blocked')""",
        (closed_transaction, category_id),
    )
    _assert_rejected(
        empty_db,
        "UPDATE transaction_splits SET category_id=? WHERE id=?",
        (category_id, closed_split),
    )
    _assert_rejected(
        empty_db,
        "UPDATE transaction_splits SET transaction_id=? WHERE id=?",
        (closed_transaction, open_split),
    )
    _assert_rejected(
        empty_db,
        "UPDATE transaction_splits SET transaction_id=? WHERE id=?",
        (open_transaction, closed_split),
    )
    _assert_rejected(
        empty_db,
        "DELETE FROM transaction_splits WHERE id=?",
        (closed_split,),
    )


def test_relationship_guard_checks_both_transaction_legs(empty_db):
    with engine.write_tx(empty_db) as conn:
        open_source, _ = _transaction(
            conn,
            account_id=_account(conn, "Source"),
            posted_on="2026-08-02",
            amount_cents=-500,
            flow_kind="internal_transfer",
            external_id="source",
        )
        closed_target, _ = _transaction(
            conn,
            account_id=_account(conn, "Target"),
            posted_on="2026-07-02",
            amount_cents=500,
            flow_kind="internal_transfer",
            external_id="target",
        )
        closed_source, _ = _transaction(
            conn,
            account_id=_account(conn, "Closed source"),
            posted_on="2026-07-03",
            amount_cents=-700,
            flow_kind="internal_transfer",
            external_id="closed-source",
        )
        open_target, _ = _transaction(
            conn,
            account_id=_account(conn, "Open target"),
            posted_on="2026-08-03",
            amount_cents=700,
            flow_kind="internal_transfer",
            external_id="open-target",
        )
        relationship_id = int(
            conn.execute(
                """INSERT INTO transaction_relationships(
                     relationship_kind, source_transaction_id,
                     target_transaction_id, created_by, reason
                   ) VALUES (
                     'transfer_pair', ?, ?, 'test:operator',
                     'synthetic target-leg pair'
                   )""",
                (open_source, closed_target),
            ).lastrowid
        )
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        """INSERT INTO transaction_relationships(
             relationship_kind, source_transaction_id, target_transaction_id,
             created_by, reason
           ) VALUES ('transfer_pair', ?, ?, 'test:operator', 'synthetic pair')""",
        (closed_source, open_target),
    )
    _assert_rejected(
        empty_db,
        """UPDATE transaction_relationships
           SET status='revoked', revoked_at=CURRENT_TIMESTAMP,
               revoked_by='test:operator', revocation_reason='blocked'
           WHERE id=?""",
        (relationship_id,),
    )


def test_statement_review_evidence_and_expectation_chains_fail_closed(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        chain = _statement_review_chain(
            conn,
            account_id=account_id,
            month="2026-07",
            tag="closed-review",
        )
        new_document_id, _ = _document(conn, tag="new-closed-review")
        policy_id = int(
            conn.execute(
                """INSERT INTO account_statement_policies(
                     account_id, effective_from_month, configuration_state,
                     requirement_mode, cadence, created_by, reason
                   ) VALUES (
                     ?, '2026-07', 'configured', 'required', 'monthly',
                     'test:operator', 'synthetic policy'
                   )""",
                (account_id,),
            ).lastrowid
        )
        expectation_id = int(
            conn.execute(
                """INSERT INTO account_statement_expectations(
                     account_id, period_month, policy_id, origin,
                     requirement_state, lifecycle_state, created_by, reason
                   ) VALUES (
                     ?, '2026-07', ?, 'policy', 'required', 'expected',
                     'test:operator', 'synthetic expectation'
                   )""",
                (account_id, policy_id),
            ).lastrowid
        )
        link_id = int(
            conn.execute(
                """INSERT INTO statement_expectation_documents(
                     expectation_id, source_document_id, attached_by,
                     attach_reason
                   ) VALUES (?, ?, 'test:operator', 'synthetic link')""",
                (expectation_id, int(chain["document_id"])),
            ).lastrowid
        )
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        """INSERT INTO statement_reviews(
             source_document_id, account_id, period_start_on, period_end_on,
             period_month, currency, activity_kind
           ) VALUES (
             ?, ?, '2026-07-01', '2026-07-28', '2026-07',
             'CAD', 'transactions'
           )""",
        (new_document_id, account_id),
    )
    _assert_rejected(
        empty_db,
        "UPDATE statement_reviews SET currency='USD' WHERE id=?",
        (int(chain["review_id"]),),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO statement_review_pages(
             statement_review_id, page_number, source_sha256, page_sha256
           ) VALUES (?, 2, ?, ?)""",
        (int(chain["review_id"]), str(chain["source_sha256"]), "b" * 64),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO statement_source_anchors(
             statement_review_id, locator_kind, locator_json,
             source_sha256, created_by
           ) VALUES (?, 'raw_row', '{"row":2}', ?, 'test:operator')""",
        (int(chain["review_id"]), str(chain["source_sha256"])),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO statement_field_evidence(
             evidence_key, statement_review_id, statement_line_id, field_name,
             original_value_json, confidence, source_anchor_id, origin
           ) VALUES (
             'writer-matrix:blocked-evidence', ?, ?, 'description',
             '"blocked"', 1.0, ?, 'manual'
           )""",
        (
            int(chain["review_id"]),
            int(chain["line_id"]),
            int(chain["anchor_id"]),
        ),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO statement_review_audit(
             operation_key, statement_review_id, statement_line_id, event_kind,
             old_values_json, new_values_json, actor, reason
           ) VALUES (
             'writer-matrix:blocked-review-audit', ?, ?,
             'row_corrected', '{}', '{}', 'test:operator', 'blocked'
           )""",
        (int(chain["review_id"]), int(chain["line_id"])),
    )
    _assert_rejected(
        empty_db,
        """UPDATE account_statement_expectations
           SET lifecycle_state='received' WHERE id=?""",
        (expectation_id,),
    )
    _assert_rejected(
        empty_db,
        """UPDATE statement_expectation_documents
           SET status='detached', detached_at=CURRENT_TIMESTAMP,
               detached_by='test:operator', detach_reason='blocked'
           WHERE id=?""",
        (link_id,),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO statement_expectation_audit(
             operation_key, event_kind, expectation_id, account_id,
             period_month, actor, reason
           ) VALUES (
             'writer-matrix:blocked-expectation-audit',
             'lifecycle_transition', ?, ?, '2026-07',
             'test:operator', 'blocked'
           )""",
        (expectation_id, account_id),
    )


def test_balance_assertion_insert_update_and_old_new_month_moves_fail_closed(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        closed_assertion = int(
            conn.execute(
                """INSERT INTO account_balance_assertions(
                     account_id, asof_date, asserted_cents, statement_period
                   ) VALUES (?, '2026-07-31', 100, '2026-07')""",
                (account_id,),
            ).lastrowid
        )
        open_assertion = int(
            conn.execute(
                """INSERT INTO account_balance_assertions(
                     account_id, asof_date, asserted_cents, statement_period
                   ) VALUES (?, '2026-08-31', 200, '2026-08')""",
                (account_id,),
            ).lastrowid
        )
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        "UPDATE account_balance_assertions SET asserted_cents=101 WHERE id=?",
        (closed_assertion,),
    )
    _assert_rejected(
        empty_db,
        """UPDATE account_balance_assertions
           SET asof_date='2026-07-30', statement_period='2026-07'
           WHERE id=?""",
        (open_assertion,),
    )
    _assert_rejected(
        empty_db,
        """UPDATE account_balance_assertions
           SET asof_date='2026-08-30', statement_period='2026-08'
           WHERE id=?""",
        (closed_assertion,),
    )


def test_balance_assertion_direct_delete_and_account_cascade_fail_closed(empty_db):
    with engine.write_tx(empty_db) as conn:
        # Every normally-created account receives an immutable statement-policy
        # row whose RESTRICT edge rejects account deletion before SQLite reaches
        # this assertion cascade. Build policy-free legacy-shaped accounts in
        # this isolated database so the ON DELETE CASCADE path itself is proven.
        account_policy_trigger = conn.execute(
            """SELECT sql
               FROM sqlite_master
               WHERE type='trigger'
                 AND name='account_statement_policy_after_account_insert'"""
        ).fetchone()["sql"]
        conn.execute("DROP TRIGGER account_statement_policy_after_account_insert")
        closed_account_id = _account(conn, "Closed assertion account")
        closed_assertion_id = int(
            conn.execute(
                """INSERT INTO account_balance_assertions(
                     account_id, asof_date, asserted_cents, statement_period
                   ) VALUES (?, '2026-07-31', 100, '2026-07')""",
                (closed_account_id,),
            ).lastrowid
        )
        open_account_id = _account(conn, "Open assertion account")
        open_assertion_id = int(
            conn.execute(
                """INSERT INTO account_balance_assertions(
                     account_id, asof_date, asserted_cents, statement_period
                   ) VALUES (?, '2026-08-31', 200, '2026-08')""",
                (open_account_id,),
            ).lastrowid
        )
        conn.execute(str(account_policy_trigger))
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        "DELETE FROM account_balance_assertions WHERE id=?",
        (closed_assertion_id,),
    )
    _assert_rejected(
        empty_db,
        "DELETE FROM accounts WHERE id=?",
        (closed_account_id,),
    )
    with engine.write_tx(empty_db) as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (open_account_id,))

    with engine.read_conn(empty_db) as conn:
        assert conn.execute(
            "SELECT 1 FROM account_balance_assertions WHERE id=?",
            (closed_assertion_id,),
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM accounts WHERE id=?",
            (closed_account_id,),
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM account_balance_assertions WHERE id=?",
            (open_assertion_id,),
        ).fetchone() is None


def test_fn149_positive_flow_and_merchant_evidence_events_fail_closed(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        subject_transaction, subject_split = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-06",
            amount_cents=500,
            flow_kind="income",
            external_id="positive-subject",
        )
        document_id, _ = _document(conn, tag="positive-statement")
        line_id = _statement_line(
            conn,
            document_id=document_id,
            account_id=account_id,
            month="2026-07",
            tag="positive-line",
            amount_cents=500,
        )
        category_id = int(
            conn.execute(
                """INSERT INTO categories(name, kind, brand_owner, color)
                   VALUES (
                     'Writer Matrix Expense', 'expense', 'shared', '#123456'
                   )"""
            ).lastrowid
        )
        conn.execute(
            "UPDATE transaction_splits SET category_id=? WHERE id=?",
            (category_id, subject_split),
        )
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        """INSERT INTO positive_flow_decision_events(
             operation_key, subject_transaction_id, statement_line_id,
             statement_line_revision, proposal_key, evidence_fingerprint,
             action_kind, selected_statement_month, request_fingerprint,
             event_kind, proposed_flow_kind, prior_statement_match_status,
             accepted_subject_splits_json, evidence_json, actor, reason
           ) VALUES (
             'writer-matrix:blocked-positive', ?, ?, 1,
             'writer-matrix:positive-proposal',
             ?, 'recover_positive_line', '2026-07', ?,
             'intake', 'income', 'unmatched',
             '[{"category_id":1,"amount_cents":500}]', '{}',
             'test:operator', 'blocked positive-flow event'
           )""",
        (subject_transaction, line_id, "c" * 64, "d" * 64),
    )

    def propose_closed_merchant_evidence(conn: sqlite3.Connection) -> None:
        repo_merchant_knowledge.propose_category(
            conn,
            descriptor="SYNTHETIC PROCESSOR 1234",
            category_id=category_id,
            scope=repo_merchant_knowledge.scope_for_transaction(
                conn,
                subject_transaction,
            ),
            operation_key="writer-matrix:blocked-merchant-event",
            actor_kind="model",
            actor="model:synthetic",
            reason="synthetic proposal must not alter closed evidence",
            evidence=Evidence(
                transaction_id=subject_transaction,
                transaction_split_id=subject_split,
            ),
            provenance_ref="synthetic:writer-matrix",
        )

    _assert_call_rejected(empty_db, propose_closed_merchant_evidence)


def test_structured_import_preview_projection_and_audit_fail_closed(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        document_id, source_sha256 = _document(
            conn,
            tag="structured-closed",
        )
        imported = repo_structured_imports.create_preview(
            conn,
            source_document_id=document_id,
            account_id=account_id,
            source_sha256=source_sha256,
            adapter_id="mapped_csv",
            adapter_version="writer-matrix.v1",
            mapping_version="writer-matrix.v1",
            mapping={"date": "Date", "amount": "Amount"},
            provider_identity_hash="",
            account_last4="",
            period_start_on="2026-07-01",
            period_end_on="2026-07-31",
            statement_issued_on="2026-08-01",
            currency="CAD",
            opening_balance_cents=0,
            closing_balance_cents=0,
            manual_fields=[],
            row_count=0,
            status="preview_ready",
            review_reasons=[],
            diagnostics=[],
            actor="test:operator",
        )
        new_document_id, new_source_sha256 = _document(
            conn,
            tag="structured-new-closed",
        )
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        """UPDATE structured_statement_imports
           SET status='needs_review', updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (int(imported["id"]),),
    )
    _assert_rejected(
        empty_db,
        """UPDATE structured_statement_imports
           SET period_start_on='2026-08-01', period_end_on='2026-08-31'
           WHERE id=?""",
        (int(imported["id"]),),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO structured_statement_import_audit(
             operation_key, import_id, event_kind,
             old_values_json, new_values_json, actor, reason
           ) VALUES (
             'writer-matrix:blocked-structured-audit', ?,
             'confirmation_blocked', '{}', '{}',
             'test:operator', 'blocked'
           )""",
        (int(imported["id"]),),
    )

    def create_closed_preview(conn: sqlite3.Connection) -> None:
        repo_structured_imports.create_preview(
            conn,
            source_document_id=new_document_id,
            account_id=account_id,
            source_sha256=new_source_sha256,
            adapter_id="mapped_csv",
            adapter_version="writer-matrix.v1",
            mapping_version="writer-matrix.v1",
            mapping={"date": "Date", "amount": "Amount"},
            provider_identity_hash="",
            account_last4="",
            period_start_on="2026-07-01",
            period_end_on="2026-07-31",
            statement_issued_on="2026-08-01",
            currency="CAD",
            opening_balance_cents=0,
            closing_balance_cents=0,
            manual_fields=[],
            row_count=0,
            status="preview_ready",
            review_reasons=[],
            diagnostics=[],
            actor="test:operator",
        )

    _assert_call_rejected(empty_db, create_closed_preview)


def test_proposed_action_scalar_array_update_and_audit_paths_fail_closed(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        transaction_id, _ = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-08",
            external_id="proposal-subject",
        )
        document_id, _ = _document(conn, tag="proposal-statement")
        line_id = _statement_line(
            conn,
            document_id=document_id,
            account_id=account_id,
            month="2026-07",
            tag="proposal-line",
        )
        payload = json.dumps(
            {
                "transaction_id": transaction_id,
                "statement_line_id": line_id,
            },
            sort_keys=True,
        )
        proposal_id = int(
            conn.execute(
                """INSERT INTO proposed_actions(
                     kind, payload_json, original_payload_json
                   ) VALUES ('synthetic', ?, ?)""",
                (payload, payload),
            ).lastrowid
        )
        _close(conn, "2026-07")

    array_payload = json.dumps(
        {
            "transaction_ids": [transaction_id],
            "statement_line_ids": [line_id],
        },
        sort_keys=True,
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO proposed_actions(
             kind, payload_json, original_payload_json
           ) VALUES ('synthetic', ?, ?)""",
        (array_payload, array_payload),
    )
    _assert_rejected(
        empty_db,
        """UPDATE proposed_actions
           SET status='rejected', feedback='blocked'
           WHERE id=?""",
        (proposal_id,),
    )
    _assert_rejected(
        empty_db,
        """UPDATE proposed_actions
           SET payload_json='{"month":"2026-08"}'
           WHERE id=?""",
        (proposal_id,),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO proposed_action_audit(
             proposed_action_id, from_status, to_status, actor,
             payload_snapshot_json, detail_json
           ) VALUES (
             ?, 'proposed', 'rejected', 'test:operator', ?, '{}'
           )""",
        (proposal_id, payload),
    )


def test_source_document_cascade_blocks_closed_lines_but_allows_open_lines(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        closed_document, _ = _document(conn, tag="cascade-closed")
        closed_line = _statement_line(
            conn,
            document_id=closed_document,
            account_id=account_id,
            month="2026-07",
            tag="cascade-closed-line",
        )
        open_document, _ = _document(conn, tag="cascade-open")
        open_line = _statement_line(
            conn,
            document_id=open_document,
            account_id=account_id,
            month="2026-08",
            tag="cascade-open-line",
        )
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        "DELETE FROM source_documents WHERE id=?",
        (closed_document,),
    )
    with engine.write_tx(empty_db) as conn:
        conn.execute("DELETE FROM source_documents WHERE id=?", (open_document,))

    with engine.read_conn(empty_db) as conn:
        assert conn.execute(
            "SELECT 1 FROM statement_lines WHERE id=?",
            (closed_line,),
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM statement_lines WHERE id=?",
            (open_line,),
        ).fetchone() is None


def test_admin_delete_and_receipt_promotion_paths_fail_closed(empty_db):
    receipt = ExtractedReceipt(
        merchant="Synthetic Closed Merchant",
        purchased_on="2026-07-12",
        currency="CAD",
        total_cents=1250,
        confidence=1.0,
    )
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        admin_document, _ = _document(
            conn,
            tag="admin-closed",
            kind="receipt",
        )
        admin_transaction, _ = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-10",
            external_id="admin-closed-transaction",
        )
        conn.execute(
            "UPDATE transactions SET source_document_id=? WHERE id=?",
            (admin_document, admin_transaction),
        )

        ingest_document, ingest_sha256 = _document(
            conn,
            tag="ingest-closed",
            kind="receipt",
        )
        extraction_id = int(
            conn.execute(
                """INSERT INTO ingest_extractions(
                     source_document_id, doc_kind, extracted_json,
                     confidence, review_status
                   ) VALUES (?, 'receipt', ?, 1.0, 'approved')""",
                (ingest_document, receipt.model_dump_json()),
            ).lastrowid
        )
        _close(conn, "2026-07")

    _assert_call_rejected(
        empty_db,
        lambda conn: repo_admin.delete_document(
            conn,
            admin_document,
            actor="test:operator",
            reason="closed admin delete must fail",
        ),
    )
    with pytest.raises(
        (
            sqlite3.IntegrityError,
            repo_close.MonthLockedError,
            repo_period_policy.PeriodLockedError,
        )
    ):
        with engine.write_tx(empty_db) as conn:
            promote_receipt(
                conn,
                source_document_id=ingest_document,
                sha256=ingest_sha256,
                receipt=receipt,
                extraction_id=extraction_id,
                account_id=account_id,
                human_review=True,
            )

    with engine.read_conn(empty_db) as conn:
        assert conn.execute(
            "SELECT 1 FROM source_documents WHERE id=?",
            (admin_document,),
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM transactions WHERE id=?",
            (admin_transaction,),
        ).fetchone() is not None
        assert conn.execute(
            """SELECT COUNT(*) FROM transactions
               WHERE source_document_id=?""",
            (ingest_document,),
        ).fetchone()[0] == 0


def test_statement_assertion_expectation_proposal_and_goal_families_fail_closed(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        transaction_id, _ = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-04",
            external_id="subject",
        )
        document_id = int(
            conn.execute(
                """INSERT INTO source_documents(
                     kind, original_name, storage_ref, sha256, mime_type, status
                   ) VALUES (
                     'statement', 'statement.pdf', 'local:statement',
                     'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                     'application/pdf', 'processed'
                   )"""
            ).lastrowid
        )
        policy_id = int(
            conn.execute(
                """INSERT INTO account_statement_policies(
                     account_id, effective_from_month, configuration_state,
                     requirement_mode, cadence, created_by, reason
                   ) VALUES (
                     ?, '2026-07', 'configured', 'required', 'monthly',
                     'test:operator', 'writer matrix'
                   )""",
                (account_id,),
            ).lastrowid
        )
        goal_id = int(
            conn.execute(
                """INSERT INTO goals(
                     name, kind, target_cents, start_month
                   ) VALUES ('Goal', 'savings_target', 10000, '2026-01')"""
            ).lastrowid
        )
        _close(conn, "2026-07")

    _assert_rejected(
        empty_db,
        """INSERT INTO statement_lines(
             source_document_id, account_id, posted_on, raw_description,
             amount_cents, statement_period, row_hash
           ) VALUES (?, ?, '2026-07-05', 'blocked', -100, '2026-07', 'blocked-row')""",
        (document_id, account_id),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO account_balance_assertions(
             account_id, asof_date, asserted_cents, statement_period
           ) VALUES (?, '2026-07-31', 0, '2026-07')""",
        (account_id,),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO account_statement_expectations(
             account_id, period_month, policy_id, origin, requirement_state,
             lifecycle_state, created_by, reason
           ) VALUES (
             ?, '2026-07', ?, 'policy', 'required', 'expected',
             'test:operator', 'blocked'
           )""",
        (account_id, policy_id),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO proposed_actions(
             kind, payload_json, original_payload_json
           ) VALUES (
             'categorize',
             json_object('transaction_id', ?),
             json_object('transaction_id', ?)
           )""",
        (transaction_id, transaction_id),
    )
    _assert_rejected(
        empty_db,
        """INSERT INTO goal_ledger(
             goal_id, month, planned_cents, actual_cents, source, status
           ) VALUES (?, '2026-07', 100, 100, 'manual', 'applied')""",
        (goal_id,),
    )


def test_every_conventional_guard_has_real_fail_closed_body(empty_db):
    with engine.read_conn(empty_db) as conn:
        rows = conn.execute(
            """SELECT name, sql
               FROM sqlite_master
               WHERE type='trigger'
                 AND name LIKE 'period_policy_guard_%'"""
        ).fetchall()
    assert len(rows) == 38
    for row in rows:
        sql = str(row["sql"])
        assert "v_period_policy_locked_months" in sql, row["name"]
        assert "RAISE(ABORT" in sql, row["name"]
        assert LOCK_MESSAGE in sql, row["name"]
