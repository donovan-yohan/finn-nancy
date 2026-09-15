from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from app.accounting.contract import FlowKind
from app.close import checklist
from app.db import (
    engine,
    repo_close,
    repo_close_inbox,
    repo_ledger,
    repo_statements,
)
from app.reconcile import apply, engine as reconcile_engine, positive_flows


MONTH = "2026-06"


def _account(
    conn: sqlite3.Connection,
    name: str,
    *,
    kind: str = "chequing",
    currency: str = "CAD",
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO accounts(name, institution, kind, currency)
            VALUES (?, 'Synthetic Bank', ?, ?)
            """,
            (name, kind, currency),
        ).lastrowid
    )


def _category(
    conn: sqlite3.Connection, name: str, kind: str = "expense"
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO categories(name, kind, brand_owner)
            VALUES (?, ?, 'shared')
            """,
            (name, kind),
        ).lastrowid
    )


def _transaction(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    category_id: int,
    amount_cents: int,
    flow_kind: FlowKind | str,
    posted_on: str,
    external_id: str,
    source_document_id: int | None = None,
) -> int:
    transaction_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=posted_on,
        description=external_id.replace("-", " "),
        counterparty=external_id.replace("-", " "),
        amount_cents=amount_cents,
        source="statement" if source_document_id is not None else "manual",
        external_id=external_id,
        source_document_id=source_document_id,
        source_confidence=1.0,
        flow_kind=flow_kind,
    )
    assert transaction_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=transaction_id,
        category_id=category_id,
        amount_cents=amount_cents,
        memo=f"split {external_id}",
    )
    return transaction_id


def _positive_subject(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    category_id: int,
    amount_cents: int = 1000,
    posted_on: str = "2026-06-20",
    match_status: str = "promoted",
    currency: str = "CAD",
) -> dict[str, int]:
    token = uuid.uuid4().hex
    document_id = int(
        conn.execute(
            """
            INSERT INTO source_documents(
              kind, original_name, storage_ref, sha256, mime_type, status
            ) VALUES (
              'statement', ?, ?, ?, 'text/csv', 'matched'
            )
            """,
            (
                f"positive-{token}.csv",
                f"statements/{token}",
                token * 2,
            ),
        ).lastrowid
    )
    transaction_id = _transaction(
        conn,
        account_id=account_id,
        category_id=category_id,
        amount_cents=amount_cents,
        flow_kind=FlowKind.UNKNOWN,
        posted_on=posted_on,
        external_id=f"positive-{token}",
        source_document_id=document_id,
    )
    line_id = int(
        conn.execute(
            """
            INSERT INTO statement_lines(
              source_document_id, account_id, posted_on, raw_description,
              norm_merchant, amount_cents, currency, is_pending,
              statement_period, row_hash, match_status,
              matched_transaction_id, row_confidence
            ) VALUES (?, ?, ?, 'AMBIGUOUS CREDIT', 'AMBIGUOUS CREDIT', ?,
                      ?, 0, ?, ?, ?, ?, 0.74)
            """,
            (
                document_id,
                account_id,
                posted_on,
                amount_cents,
                currency,
                MONTH,
                f"row-{token}",
                match_status,
                transaction_id if match_status in ("matched", "promoted") else None,
            ),
        ).lastrowid
    )
    conn.execute(
        """
        UPDATE transactions
        SET recon_status='cleared', cleared_on=?
        WHERE id=?
        """,
        (posted_on, transaction_id),
    )
    return {
        "document_id": document_id,
        "line_id": line_id,
        "transaction_id": transaction_id,
    }


def _positive_intake_line(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    amount_cents: int = 1000,
    posted_on: str = "2026-06-20",
    match_status: str = "ignored",
    currency: str = "CAD",
) -> dict[str, int]:
    token = uuid.uuid4().hex
    document_id = int(
        conn.execute(
            """
            INSERT INTO source_documents(
              kind, original_name, storage_ref, sha256, mime_type, status
            ) VALUES ('statement', ?, ?, ?, 'text/csv', 'matched')
            """,
            (
                f"intake-{token}.csv",
                f"statements/{token}",
                token * 2,
            ),
        ).lastrowid
    )
    line_id = int(
        conn.execute(
            """
            INSERT INTO statement_lines(
              source_document_id, account_id, posted_on, raw_description,
              norm_merchant, amount_cents, currency, is_pending,
              statement_period, row_hash, match_status, row_confidence
            ) VALUES (?, ?, ?, 'IGNORED CREDIT', 'IGNORED CREDIT', ?, ?,
                      0, ?, ?, ?, 0.81)
            """,
            (
                document_id,
                account_id,
                posted_on,
                amount_cents,
                currency,
                MONTH,
                f"intake-row-{token}",
                match_status,
            ),
        ).lastrowid
    )
    return {"document_id": document_id, "line_id": line_id}


def _proposal(
    conn: sqlite3.Connection,
    subject_id: int,
    *,
    flow_kind: str,
    relationship_kind: str = "",
) -> dict:
    review = next(
        item
        for item in positive_flows.list_positive_flow_reviews(
            conn, MONTH, include_suppressed=True
        )
        if int(item["subject"]["transaction_id"]) == subject_id
    )
    return next(
        item
        for item in review["proposals"]
        if item["proposed_flow_kind"] == flow_kind
        and item["relationship_kind"] == relationship_kind
    )


def _accept_pair(
    conn: sqlite3.Connection,
    *,
    subject_id: int,
    flow_kind: str,
    relationship_kind: str,
) -> int:
    proposal = _proposal(
        conn,
        subject_id,
        flow_kind=flow_kind,
        relationship_kind=relationship_kind,
    )
    selected_category_id = None
    if flow_kind in {"refund", "reimbursement", "reversal"}:
        assert len(proposal["allocation_options"]) == 1
        selected_category_id = int(
            proposal["allocation_options"][0]["category_id"]
        )
    return positive_flows.accept_pair(
        conn,
        subject_transaction_id=subject_id,
        month=MONTH,
        proposal_key=proposal["proposal_key"],
        evidence_fingerprint=proposal["evidence_fingerprint"],
        operation_key=f"test:{uuid.uuid4().hex}",
        actor="test:operator",
        reason=f"confirmed {flow_kind}",
        selected_category_id=selected_category_id,
    )


def _mutation_snapshot(
    conn: sqlite3.Connection, transaction_ids: list[int]
) -> dict:
    placeholders = ",".join("?" for _ in transaction_ids)
    return {
        "events": [
            tuple(row)
            for row in conn.execute(
                """
                SELECT id, operation_key, event_kind, request_fingerprint
                FROM positive_flow_decision_events ORDER BY id
                """
            )
        ],
        "transactions": [
            tuple(row)
            for row in conn.execute(
                f"""
                SELECT id, flow_kind, amount_cents, recon_status, cleared_on
                FROM transactions
                WHERE id IN ({placeholders})
                ORDER BY id
                """,
                transaction_ids,
            )
        ],
        "splits": [
            tuple(row)
            for row in conn.execute(
                f"""
                SELECT transaction_id, id, category_id, amount_cents, memo
                FROM transaction_splits
                WHERE transaction_id IN ({placeholders})
                ORDER BY transaction_id, id
                """,
                transaction_ids,
            )
        ],
        "relationships": [
            tuple(row)
            for row in conn.execute(
                """
                SELECT id, relationship_kind, source_transaction_id,
                       target_transaction_id, status, revoked_at,
                       revoked_by, revocation_reason
                FROM transaction_relationships ORDER BY id
                """
            )
        ],
    }


def test_ambiguous_positive_is_not_income_and_classification_is_reversible(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Income Review")
        expense_id = _category(conn, "Review Placeholder")
        subject = _positive_subject(
            conn, account_id=account_id, category_id=expense_id
        )

    with engine.read_conn(empty_db) as conn:
        ambiguous_report = conn.execute(
            "SELECT income_cents FROM v_cashflow_monthly WHERE month=?",
            (MONTH,),
        ).fetchone()
        assert ambiguous_report["income_cents"] == 0
        coverage = conn.execute(
            """
            SELECT income_cents, positive_review_cents, coverage_bucket
            FROM v_statement_coverage_lines WHERE line_id=?
            """,
            (subject["line_id"],),
        ).fetchone()
        assert tuple(coverage) == (0, 1000, "flow_review")

    with engine.write_tx(empty_db) as conn:
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.INCOME.value,
        )
        event_id = positive_flows.accept_classification(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            flow_kind=FlowKind.INCOME,
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:accept-income",
            actor="test:operator",
            reason="pay stub confirms earned income",
        )
        assert positive_flows.accept_classification(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            flow_kind=FlowKind.INCOME,
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:accept-income",
            actor="test:operator",
            reason="pay stub confirms earned income",
        ) == event_id
        with pytest.raises(ValueError, match="different positive-flow request"):
            positive_flows.accept_classification(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month="2026-05",
                flow_kind=FlowKind.INCOME,
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:accept-income",
                actor="test:operator",
                reason="pay stub confirms earned income",
            )
        amount = conn.execute(
            "SELECT amount_cents FROM transactions WHERE id=?",
            (subject["transaction_id"],),
        ).fetchone()[0]
        split = conn.execute(
            """
            SELECT split.amount_cents, category.kind
            FROM transaction_splits split
            JOIN categories category ON category.id=split.category_id
            WHERE split.transaction_id=?
            """,
            (subject["transaction_id"],),
        ).fetchone()
        assert amount == 1000
        assert tuple(split) == (1000, "income")
        report = conn.execute(
            "SELECT * FROM v_cashflow_monthly WHERE month=?", (MONTH,)
        ).fetchone()
        assert report["income_cents"] == 1000
        accepted = positive_flows.list_positive_flow_reviews(conn, MONTH)[0]
        assert accepted["status"] == "resolved"
        assert accepted["acceptance"]["event_id"] == event_id
        undo_fingerprint = accepted["acceptance"]["undo_fingerprint"]
        prior_split_json = conn.execute(
            """
            SELECT prior_subject_splits_json
            FROM positive_flow_decision_events WHERE id=?
            """,
            (event_id,),
        ).fetchone()[0]

    with engine.write_tx(empty_db) as conn:
        undo_id = positive_flows.undo_acceptance(
            conn,
            accept_event_id=event_id,
            month=MONTH,
            evidence_fingerprint=undo_fingerprint,
            operation_key="test:undo-income",
            actor="test:operator",
            reason="pay stub was attached to the wrong row",
        )
        assert positive_flows.undo_acceptance(
            conn,
            accept_event_id=event_id,
            month=MONTH,
            evidence_fingerprint=undo_fingerprint,
            operation_key="test:undo-income",
            actor="test:operator",
            reason="pay stub was attached to the wrong row",
        ) == undo_id
        with pytest.raises(ValueError, match="different positive-flow request"):
            positive_flows.undo_acceptance(
                conn,
                accept_event_id=event_id,
                month=MONTH,
                evidence_fingerprint=undo_fingerprint,
                operation_key="test:undo-income",
                actor="test:operator",
                reason="different undo reason",
            )
        transaction = conn.execute(
            "SELECT amount_cents, flow_kind FROM transactions WHERE id=?",
            (subject["transaction_id"],),
        ).fetchone()
        assert tuple(transaction) == (1000, "unknown")
        restored = positive_flows._split_json(
            positive_flows._split_snapshot(conn, subject["transaction_id"])
        )
        assert restored == prior_split_json
        assert [
            row["event_kind"]
            for row in conn.execute(
            """
            SELECT event_kind FROM positive_flow_decision_events
                WHERE subject_transaction_id=? ORDER BY id
                """,
                (subject["transaction_id"],),
            )
        ] == ["accept", "undo"]


def test_decision_events_are_append_only_and_one_accept_is_live_per_subject(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Decision Constraint")
        category_id = _category(conn, "Decision Placeholder")
        subject = _positive_subject(
            conn, account_id=account_id, category_id=category_id
        )
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.INCOME.value,
        )
        event_id = positive_flows.accept_classification(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            flow_kind=FlowKind.INCOME,
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:constraint-accept",
            actor="test:operator",
            reason="constraint fixture",
        )
        accepted = conn.execute(
            "SELECT * FROM positive_flow_decision_events WHERE id=?",
            (event_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                """
                UPDATE positive_flow_decision_events
                SET reason='rewritten'
                WHERE id=?
                """,
                (event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM positive_flow_decision_events WHERE id=?",
                (event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="not available"):
            conn.execute(
                """
                INSERT INTO positive_flow_decision_events(
                  operation_key, subject_transaction_id, statement_line_id,
                  statement_line_revision, proposal_key, evidence_fingerprint,
                  action_kind, selected_statement_month, request_fingerprint,
                  event_kind, proposed_flow_kind, prior_subject_flow_kind,
                  prior_subject_splits_json, accepted_subject_splits_json,
                  evidence_json, actor, reason
                ) VALUES (
                  'test:second-live-accept', ?, ?, ?, ?, ?,
                  'accept_classification', ?, ?,
                  'accept', 'income', 'unknown', ?, ?, '{}',
                  'test:raw', 'must fail globally'
                )
                """,
                (
                    subject["transaction_id"],
                    subject["line_id"],
                    int(accepted["statement_line_revision"]),
                    "different-proposal-key",
                    "1" * 64,
                    MONTH,
                    "2" * 64,
                    str(accepted["prior_subject_splits_json"]),
                    str(accepted["accepted_subject_splits_json"]),
                ),
            )


def test_operation_replay_rejects_forged_fingerprint_request_identity(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Replay Identity")
        category_id = _category(conn, "Replay Identity Placeholder")
        subject = _positive_subject(
            conn, account_id=account_id, category_id=category_id
        )
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.INCOME.value,
        )
        forged_fingerprint = positive_flows._request_fingerprint(
            action_kind="reject_proposal",
            subject_transaction_id=subject["transaction_id"],
            statement_line_id=subject["line_id"],
            selected_statement_month=MONTH,
            proposal_key=proposal["proposal_key"],
            candidate_transaction_id=None,
            proposed_flow_kind=FlowKind.INCOME.value,
            relationship_kind="",
            proposed_candidate_flow_kind="",
            evidence_fingerprint=proposal["evidence_fingerprint"],
            actor="test:caller-b",
            reason="caller B request",
            selected_category_id=None,
            allocation_explicit=False,
        )
        conn.execute(
            """
            INSERT INTO positive_flow_decision_events(
              operation_key, subject_transaction_id, statement_line_id,
              statement_line_revision, proposal_key, evidence_fingerprint,
              action_kind, selected_statement_month, request_fingerprint,
              event_kind, proposed_flow_kind, evidence_json, actor, reason
            ) VALUES (
              'test:forged-replay', ?, ?, ?, ?, ?,
              'reject_proposal', ?, ?, 'reject', 'income', ?, ?, ?
            )
            """,
            (
                subject["transaction_id"],
                subject["line_id"],
                1,
                proposal["proposal_key"],
                proposal["evidence_fingerprint"],
                MONTH,
                forged_fingerprint,
                json.dumps(
                    proposal["audit_evidence"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "test:stored-a",
                "stored A request",
            ),
        )

        with pytest.raises(ValueError, match="different positive-flow request"):
            positive_flows.reject_proposal(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=proposal["proposal_key"],
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:forged-replay",
                actor="test:caller-b",
                reason="caller B request",
            )

        exact_subject = _positive_subject(
            conn, account_id=account_id, category_id=category_id
        )
        exact_proposal = _proposal(
            conn,
            exact_subject["transaction_id"],
            flow_kind=FlowKind.INCOME.value,
        )
        exact_id = positive_flows.reject_proposal(
            conn,
            subject_transaction_id=exact_subject["transaction_id"],
            month=MONTH,
            proposal_key=exact_proposal["proposal_key"],
            evidence_fingerprint=exact_proposal["evidence_fingerprint"],
            operation_key="test:exact-replay",
            actor="test:operator",
            reason="exact request",
        )
        assert positive_flows.reject_proposal(
            conn,
            subject_transaction_id=exact_subject["transaction_id"],
            month=MONTH,
            proposal_key=exact_proposal["proposal_key"],
            evidence_fingerprint=exact_proposal["evidence_fingerprint"],
            operation_key="test:exact-replay",
            actor="test:operator",
            reason="exact request",
        ) == exact_id
        assert conn.execute(
            """
            SELECT COUNT(*) FROM positive_flow_decision_events
            WHERE operation_key='test:exact-replay'
            """
        ).fetchone()[0] == 1


def test_undo_fails_without_mutation_when_accepted_split_state_drifted(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Undo Drift")
        category_id = _category(conn, "Undo Drift Placeholder")
        subject = _positive_subject(
            conn, account_id=account_id, category_id=category_id
        )
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.INCOME.value,
        )
        event_id = positive_flows.accept_classification(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            flow_kind=FlowKind.INCOME,
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:drift-accept",
            actor="test:operator",
            reason="drift fixture",
        )
        replacement_category = _category(
            conn, "Reviewed Other Income", "income"
        )
        conn.execute(
            """
            UPDATE transaction_splits SET category_id=?
            WHERE transaction_id=?
            """,
            (replacement_category, subject["transaction_id"]),
        )
        refreshed = positive_flows.list_positive_flow_reviews(conn, MONTH)[0]
        with pytest.raises(ValueError, match="splits changed"):
            positive_flows.undo_acceptance(
                conn,
                accept_event_id=event_id,
                month=MONTH,
                evidence_fingerprint=refreshed["acceptance"][
                    "undo_fingerprint"
                ],
                operation_key="test:drift-undo",
                actor="test:operator",
                reason="must fail without overwriting later categorization",
            )
        assert conn.execute(
            "SELECT flow_kind FROM transactions WHERE id=?",
            (subject["transaction_id"],),
        ).fetchone()[0] == "income"
        assert conn.execute(
            """
            SELECT COUNT(*) FROM positive_flow_decision_events
            WHERE event_kind='undo' AND reverts_event_id=?
            """,
            (event_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    (
        "flow_kind",
        "relationship_kind",
        "subject_amount",
        "candidate_amount",
        "subject_account_kind",
        "candidate_account_kind",
        "same_account",
        "expected_expense",
    ),
    [
        ("refund", "refund_of", 400, -1000, "credit", "credit", True, 600),
        (
            "reimbursement",
            "reimbursement_for",
            400,
            -1000,
            "chequing",
            "credit",
            False,
            600,
        ),
        ("reversal", "reversal_of", 1000, -1000, "credit", "credit", True, 0),
        (
            "internal_transfer",
            "transfer_pair",
            1000,
            -1000,
            "savings",
            "chequing",
            False,
            0,
        ),
        (
            "card_payment",
            "transfer_pair",
            1000,
            -1000,
            "credit",
            "chequing",
            False,
            0,
        ),
    ],
)
def test_every_positive_pair_flow_creates_one_typed_edge_and_report_effect(
    empty_db,
    flow_kind,
    relationship_kind,
    subject_amount,
    candidate_amount,
    subject_account_kind,
    candidate_account_kind,
    same_account,
    expected_expense,
):
    with engine.write_tx(empty_db) as conn:
        subject_account = _account(
            conn, "Positive Account", kind=subject_account_kind
        )
        candidate_account = (
            subject_account
            if same_account
            else _account(conn, "Candidate Account", kind=candidate_account_kind)
        )
        placeholder_id = _category(conn, "Positive Placeholder")
        purchase_id = _category(conn, "Original Purchase")
        subject = _positive_subject(
            conn,
            account_id=subject_account,
            category_id=placeholder_id,
            amount_cents=subject_amount,
        )
        candidate_id = _transaction(
            conn,
            account_id=candidate_account,
            category_id=purchase_id,
            amount_cents=candidate_amount,
            flow_kind=FlowKind.PURCHASE
            if flow_kind not in ("internal_transfer", "card_payment")
            else FlowKind.UNKNOWN,
            posted_on="2026-06-10",
            external_id=f"candidate-{flow_kind}",
        )
        original_amounts = {
            row["id"]: row["amount_cents"]
            for row in conn.execute(
                "SELECT id, amount_cents FROM transactions"
            )
        }
        event_id = _accept_pair(
            conn,
            subject_id=subject["transaction_id"],
            flow_kind=flow_kind,
            relationship_kind=relationship_kind,
        )

        relationship = conn.execute(
            """
            SELECT * FROM transaction_relationships
            WHERE status='active'
            """
        ).fetchall()
        assert len(relationship) == 1
        assert relationship[0]["relationship_kind"] == relationship_kind
        assert conn.execute(
            """
            SELECT COUNT(*) FROM positive_flow_decision_events
            WHERE id=? AND accepted_relationship_id=?
            """,
            (event_id, relationship[0]["id"]),
        ).fetchone()[0] == 1
        assert {
            row["id"]: row["amount_cents"]
            for row in conn.execute(
                "SELECT id, amount_cents FROM transactions"
            )
        } == original_amounts
        subject_state = conn.execute(
            "SELECT flow_kind FROM transactions WHERE id=?",
            (subject["transaction_id"],),
        ).fetchone()[0]
        assert subject_state == flow_kind
        report = conn.execute(
            "SELECT * FROM v_cashflow_monthly WHERE month=?", (MONTH,)
        ).fetchone()
        assert report["income_cents"] == 0
        assert report["expense_cents"] == expected_expense
        subject_category_kind = conn.execute(
            """
            SELECT category.kind
            FROM transaction_splits split
            JOIN categories category ON category.id=split.category_id
            WHERE split.transaction_id=?
            """,
            (subject["transaction_id"],),
        ).fetchone()[0]
        assert subject_category_kind == (
            "transfer"
            if flow_kind in ("internal_transfer", "card_payment")
            else "expense"
        )


def test_reject_restore_stale_evidence_and_operation_key_replay(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Reject Review", kind="credit")
        category_id = _category(conn, "Reject Purchase")
        subject = _positive_subject(
            conn, account_id=account_id, category_id=category_id
        )
        candidate_id = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.PURCHASE,
            posted_on="2026-06-01",
            external_id="reject-candidate",
        )
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind="refund",
            relationship_kind="refund_of",
        )
        reject_id = positive_flows.reject_proposal(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            proposal_key=proposal["proposal_key"],
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:reject",
            actor="test:operator",
            reason="not the same merchant",
        )
        assert positive_flows.reject_proposal(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            proposal_key=proposal["proposal_key"],
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:reject",
            actor="test:operator",
            reason="not the same merchant",
        ) == reject_id
        with pytest.raises(ValueError, match="different positive-flow request"):
            positive_flows.reject_proposal(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=proposal["proposal_key"],
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:reject",
                actor="test:operator",
                reason="different rejection reason",
            )
        suppressed = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind="refund",
            relationship_kind="refund_of",
        )
        assert suppressed["state"] == "suppressed"
        reimbursement = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind="reimbursement",
            relationship_kind="reimbursement_for",
        )
        assert reimbursement["state"] == "available"
        with pytest.raises(ValueError, match="different positive-flow request"):
            positive_flows.restore_proposal(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=suppressed["proposal_key"],
                evidence_fingerprint=suppressed["evidence_fingerprint"],
                operation_key="test:reject",
                actor="test:operator",
                reason="not the same merchant",
            )
        with pytest.raises(ValueError, match="not available"):
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=proposal["proposal_key"],
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:accept-rejected",
                actor="test:operator",
                reason="stale acceptance",
            )
        restore_id = positive_flows.restore_proposal(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            proposal_key=suppressed["proposal_key"],
            evidence_fingerprint=suppressed["evidence_fingerprint"],
            operation_key="test:restore",
            actor="test:operator",
            reason="reconsider after checking evidence",
        )
        assert positive_flows.restore_proposal(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            proposal_key=suppressed["proposal_key"],
            evidence_fingerprint=suppressed["evidence_fingerprint"],
            operation_key="test:restore",
            actor="test:operator",
            reason="reconsider after checking evidence",
        ) == restore_id
        with pytest.raises(ValueError, match="different positive-flow request"):
            positive_flows.restore_proposal(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=suppressed["proposal_key"],
                evidence_fingerprint=suppressed["evidence_fingerprint"],
                operation_key="test:restore",
                actor="test:other",
                reason="reconsider after checking evidence",
            )
        conn.execute(
            "UPDATE transactions SET description='changed evidence' WHERE id=?",
            (candidate_id,),
        )
        with pytest.raises(ValueError, match="evidence changed"):
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=proposal["proposal_key"],
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:stale",
                actor="test:operator",
                reason="stale page",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM transaction_relationships"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT flow_kind FROM transactions WHERE id=?",
            (subject["transaction_id"],),
        ).fetchone()[0] == "unknown"


def test_pending_line_wrong_month_scope_and_cross_currency_fail_closed(empty_db):
    with engine.write_tx(empty_db) as conn:
        cad_account = _account(conn, "CAD Account")
        usd_account = _account(conn, "USD Account", currency="USD")
        category_id = _category(conn, "Scope Purchase")
        pending = _positive_subject(
            conn,
            account_id=cad_account,
            category_id=category_id,
            match_status="needs_review",
        )
        with pytest.raises(ValueError, match="active matched statement row"):
            positive_flows.reject_proposal(
                conn,
                subject_transaction_id=pending["transaction_id"],
                month=MONTH,
                proposal_key="not-a-proposal",
                evidence_fingerprint="0" * 64,
                operation_key="test:pending-reject",
                actor="test:operator",
                reason="must promote first",
            )

        subject = _positive_subject(
            conn, account_id=cad_account, category_id=category_id
        )
        _transaction(
            conn,
            account_id=usd_account,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.PURCHASE,
            posted_on="2026-06-10",
            external_id="usd-candidate",
        )
        with pytest.raises(ValueError, match="selected month"):
            positive_flows.accept_classification(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month="2026-05",
                flow_kind=FlowKind.INCOME,
                evidence_fingerprint="0" * 64,
                operation_key="test:wrong-month",
                actor="test:operator",
                reason="wrong statement scope",
            )
        review = next(
            item
            for item in positive_flows.list_positive_flow_reviews(conn, MONTH)
            if int(item["subject"]["transaction_id"])
            == subject["transaction_id"]
        )
        assert not any(
            item["candidate_transaction_id"]
            and int(item["candidate_transaction_id"])
            != subject["transaction_id"]
            for item in review["proposals"]
            if item["proposal_kind"] == "pair"
        )


def test_ignored_unpromoted_positive_is_a_statement_month_close_exception(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Cross Month Review")
        subject = _positive_intake_line(
            conn,
            account_id=account_id,
            posted_on="2026-07-02",
            match_status="ignored",
        )
        june = checklist.build_checklist(conn, MONTH)
        assert june["has_blocking_flow_reviews"] is True
        assert june["flow_review_count"] == 1
        flow_row = next(
            item
            for item in june["rows"]
            if item["key"] == "flow_review"
        )
        assert flow_row["positive_line_count"] == 1
        assert flow_row["href"] == (
            f"/recon?month={MONTH}#positive-intake-{subject['line_id']}"
        )
        inbox = repo_close_inbox.build_inbox(conn, MONTH)
        positive_items = [
            item for item in inbox if item.source == "positive_flow"
        ]
        assert len(positive_items) == 1
        assert positive_items[0].amount_cents == 1000
        assert positive_items[0].href == (
            f"/recon?month={MONTH}#positive-intake-{subject['line_id']}"
        )
        assert str(subject["line_id"]) in positive_items[0].ident


def test_targeted_positive_intake_is_audited_idempotent_and_line_scoped(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Targeted Intake")
        intake = _positive_intake_line(
            conn,
            account_id=account_id,
            match_status="ignored",
        )
        sibling_id = int(
            conn.execute(
                """
                INSERT INTO statement_lines(
                  source_document_id, account_id, posted_on, raw_description,
                  norm_merchant, amount_cents, currency, is_pending,
                  statement_period, row_hash, match_status
                ) VALUES (?, ?, '2026-06-21', 'SIBLING CREDIT',
                          'SIBLING CREDIT', 2000, 'CAD', 0, ?, ?,
                          'ignored')
                """,
                (
                    intake["document_id"],
                    account_id,
                    MONTH,
                    f"intake-sibling-{uuid.uuid4().hex}",
                ),
            ).lastrowid
        )
        conn.execute(
            "UPDATE source_documents SET status='needs_review' WHERE id=?",
            (intake["document_id"],),
        )
        sibling_before = tuple(
            conn.execute(
                "SELECT * FROM statement_lines WHERE id=?",
                (sibling_id,),
            ).fetchone()
        )
        item = next(
            row
            for row in positive_flows.list_positive_flow_intake(conn, MONTH)
            if int(row["line"]["statement_line_id"]) == intake["line_id"]
        )
        event_id = positive_flows.recover_positive_line(
            conn,
            statement_line_id=intake["line_id"],
            month=MONTH,
            evidence_fingerprint=item["evidence_fingerprint"],
            operation_key="test:intake",
            actor="test:operator",
            reason="recover only this ignored credit",
        )
        assert positive_flows.recover_positive_line(
            conn,
            statement_line_id=intake["line_id"],
            month=MONTH,
            evidence_fingerprint=item["evidence_fingerprint"],
            operation_key="test:intake",
            actor="test:operator",
            reason="recover only this ignored credit",
        ) == event_id

        event = conn.execute(
            "SELECT * FROM positive_flow_decision_events WHERE id=?",
            (event_id,),
        ).fetchone()
        transaction = conn.execute(
            "SELECT * FROM transactions WHERE id=?",
            (event["subject_transaction_id"],),
        ).fetchone()
        line = conn.execute(
            "SELECT * FROM statement_lines WHERE id=?",
            (intake["line_id"],),
        ).fetchone()
        assert event["action_kind"] == "recover_positive_line"
        assert event["selected_statement_month"] == MONTH
        assert len(event["request_fingerprint"]) == 64
        assert event["prior_statement_match_status"] == "ignored"
        assert tuple(
            transaction[key]
            for key in (
                "account_id",
                "amount_cents",
                "source",
                "source_document_id",
                "external_id",
                "flow_kind",
            )
        ) == (
            account_id,
            1000,
            "statement",
            intake["document_id"],
            line["row_hash"],
            "unknown",
        )
        assert tuple(
            line[key] for key in ("match_status", "matched_transaction_id")
        ) == ("promoted", transaction["id"])
        assert tuple(
            conn.execute(
                "SELECT * FROM statement_lines WHERE id=?",
                (sibling_id,),
            ).fetchone()
        ) == sibling_before
        assert conn.execute(
            "SELECT status FROM source_documents WHERE id=?",
            (intake["document_id"],),
        ).fetchone()[0] == "needs_review"

        for changed in (
            {
                "statement_line_id": intake["line_id"],
                "month": MONTH,
                "actor": "test:other",
                "reason": "recover only this ignored credit",
            },
            {
                "statement_line_id": intake["line_id"],
                "month": MONTH,
                "actor": "test:operator",
                "reason": "different reason",
            },
            {
                "statement_line_id": intake["line_id"],
                "month": "2026-05",
                "actor": "test:operator",
                "reason": "recover only this ignored credit",
            },
            {
                "statement_line_id": sibling_id,
                "month": MONTH,
                "actor": "test:operator",
                "reason": "recover only this ignored credit",
            },
        ):
            with pytest.raises(ValueError, match="different positive-flow request"):
                positive_flows.recover_positive_line(
                    conn,
                    evidence_fingerprint=item["evidence_fingerprint"],
                    operation_key="test:intake",
                    **changed,
                )
        assert conn.execute(
            "SELECT COUNT(*) FROM positive_flow_decision_events"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("undo_before_unreconcile", [False, True])
def test_recovery_reattaches_preserved_canonical_statement_transaction(
    empty_db,
    undo_before_unreconcile,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Canonical Recovery", kind="credit")
        staged = _positive_intake_line(
            conn,
            account_id=account_id,
            match_status="unmatched",
        )
        transaction_id = reconcile_engine.promote_from_line(
            conn,
            conn.execute(
                "SELECT * FROM statement_lines WHERE id=?",
                (staged["line_id"],),
            ).fetchone(),
        )
        assert transaction_id is not None
        subject = {
            **staged,
            "transaction_id": int(transaction_id),
        }
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.INCOME.value,
        )
        accept_id = positive_flows.accept_classification(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            flow_kind=FlowKind.INCOME,
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key=f"test:canonical-accept:{undo_before_unreconcile}",
            actor="test:operator",
            reason="preserve this reviewed income decision",
        )
        expected_flow = FlowKind.INCOME.value
        if undo_before_unreconcile:
            accepted = next(
                item
                for item in positive_flows.list_positive_flow_reviews(
                    conn, MONTH
                )
                if int(item["subject"]["transaction_id"])
                == subject["transaction_id"]
            )["acceptance"]
            positive_flows.undo_acceptance(
                conn,
                accept_event_id=accept_id,
                month=MONTH,
                evidence_fingerprint=accepted["undo_fingerprint"],
                operation_key="test:canonical-undo",
                actor="test:operator",
                reason="return this credit to unknown review",
            )
            expected_flow = FlowKind.UNKNOWN.value

        transaction_count = conn.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM positive_flow_decision_events"
        ).fetchone()[0]
        line_identity_before = tuple(
            conn.execute(
                """
                SELECT source_document_id, account_id, posted_on, amount_cents,
                       currency, row_hash, review_disposition
                FROM statement_lines WHERE id=?
                """,
                (subject["line_id"],),
            ).fetchone()
        )

        apply.unreconcile_document(conn, subject["document_id"])
        intake = next(
            item
            for item in positive_flows.list_positive_flow_intake(conn, MONTH)
            if int(item["line"]["statement_line_id"]) == subject["line_id"]
        )
        recovery_id = positive_flows.recover_positive_line(
            conn,
            statement_line_id=subject["line_id"],
            month=MONTH,
            evidence_fingerprint=intake["evidence_fingerprint"],
            operation_key=f"test:canonical-recovery:{undo_before_unreconcile}",
            actor="test:operator",
            reason="reattach the canonical statement transaction",
        )

        recovered = conn.execute(
            "SELECT * FROM positive_flow_decision_events WHERE id=?",
            (recovery_id,),
        ).fetchone()
        line = conn.execute(
            "SELECT * FROM statement_lines WHERE id=?",
            (subject["line_id"],),
        ).fetchone()
        transaction = conn.execute(
            "SELECT * FROM transactions WHERE id=?",
            (subject["transaction_id"],),
        ).fetchone()
        assert recovered["event_kind"] == "intake"
        assert recovered["subject_transaction_id"] == subject["transaction_id"]
        assert recovered["proposed_flow_kind"] == expected_flow
        assert tuple(
            line[key]
            for key in (
                "source_document_id",
                "account_id",
                "posted_on",
                "amount_cents",
                "currency",
                "row_hash",
                "review_disposition",
            )
        ) == line_identity_before
        assert tuple(
            line[key] for key in ("match_status", "matched_transaction_id")
        ) == ("promoted", subject["transaction_id"])
        assert transaction["flow_kind"] == expected_flow
        assert transaction["recon_status"] == "cleared"
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0] == transaction_count
        assert conn.execute(
            "SELECT COUNT(*) FROM positive_flow_decision_events"
        ).fetchone()[0] == event_count + 1


def test_competing_endpoint_and_closed_endpoint_are_atomic(empty_db):
    with engine.write_tx(empty_db) as conn:
        source_account = _account(conn, "Transfer Source")
        target_a = _account(conn, "Transfer A", kind="savings")
        target_b = _account(conn, "Transfer B", kind="savings")
        category_id = _category(conn, "Transfer Placeholder")
        candidate_id = _transaction(
            conn,
            account_id=source_account,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.UNKNOWN,
            posted_on="2026-05-31",
            external_id="shared-transfer-source",
        )
        first = _positive_subject(
            conn, account_id=target_a, category_id=category_id
        )
        second = _positive_subject(
            conn, account_id=target_b, category_id=category_id
        )
        first_proposal = _proposal(
            conn,
            first["transaction_id"],
            flow_kind="internal_transfer",
            relationship_kind="transfer_pair",
        )
        second_proposal = _proposal(
            conn,
            second["transaction_id"],
            flow_kind="internal_transfer",
            relationship_kind="transfer_pair",
        )
        repo_close.mark_closed(conn, "2026-05", reason="test closed endpoint")
        with pytest.raises(repo_close.MonthLockedError):
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=first["transaction_id"],
                month=MONTH,
                proposal_key=first_proposal["proposal_key"],
                evidence_fingerprint=first_proposal["evidence_fingerprint"],
                operation_key="test:closed-endpoint",
                actor="test:operator",
                reason="must not cross a closed month",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM transaction_relationships"
        ).fetchone()[0] == 0
        repo_close.reopen(conn, "2026-05", reason="continue atomicity test")
        positive_flows.accept_pair(
            conn,
            subject_transaction_id=first["transaction_id"],
            month=MONTH,
            proposal_key=first_proposal["proposal_key"],
            evidence_fingerprint=first_proposal["evidence_fingerprint"],
            operation_key="test:first-transfer",
            actor="test:operator",
            reason="confirmed first transfer",
        )
        accepted = next(
            item
            for item in positive_flows.list_positive_flow_reviews(conn, MONTH)
            if int(item["subject"]["transaction_id"])
            == first["transaction_id"]
        )["acceptance"]
        repo_close.mark_closed(conn, "2026-05", reason="lock undo endpoint")
        with pytest.raises(repo_close.MonthLockedError):
            positive_flows.undo_acceptance(
                conn,
                accept_event_id=accepted["event_id"],
                month=MONTH,
                evidence_fingerprint=accepted["undo_fingerprint"],
                operation_key="test:closed-endpoint-undo",
                actor="test:operator",
                reason="must not undo across a closed month",
            )
        assert conn.execute(
            "SELECT status FROM transaction_relationships"
        ).fetchone()[0] == "active"
        repo_close.reopen(conn, "2026-05", reason="continue competing test")
        with pytest.raises(ValueError):
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=second["transaction_id"],
                month=MONTH,
                proposal_key=second_proposal["proposal_key"],
                evidence_fingerprint=second_proposal["evidence_fingerprint"],
                operation_key="test:competing-transfer",
                actor="test:operator",
                reason="stale competing target",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM transaction_relationships WHERE status='active'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT flow_kind FROM transactions WHERE id=?",
            (second["transaction_id"],),
        ).fetchone()[0] == "unknown"
        assert conn.execute(
            """
            SELECT flow_kind FROM transactions WHERE id=?
            """,
            (candidate_id,),
        ).fetchone()[0] == "internal_transfer"


def test_closed_statement_month_blocks_classification_before_any_mutation(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "July Income Endpoint")
        category_id = _category(conn, "July Income Placeholder")
        subject = _positive_subject(
            conn,
            account_id=account_id,
            category_id=category_id,
            posted_on="2026-07-02",
        )
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.INCOME.value,
        )
        assert not repo_close.is_month_locked(conn, "2026-07")
        repo_close.mark_closed(
            conn, MONTH, reason="closed statement period regression"
        )
        before = _mutation_snapshot(conn, [subject["transaction_id"]])
        with pytest.raises(repo_close.MonthLockedError, match=MONTH):
            positive_flows.accept_classification(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                flow_kind=FlowKind.INCOME,
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:closed-statement-classification",
                actor="test:operator",
                reason="must fail before mutation",
            )
        assert _mutation_snapshot(
            conn, [subject["transaction_id"]]
        ) == before


def test_closed_statement_month_blocks_pair_before_any_endpoint_mutation(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        source_account = _account(conn, "May Pair Endpoint")
        target_account = _account(
            conn, "July Pair Endpoint", kind="savings"
        )
        category_id = _category(conn, "Pair Placeholder")
        candidate_id = _transaction(
            conn,
            account_id=source_account,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.UNKNOWN,
            posted_on="2026-05-31",
            external_id="closed-statement-pair-source",
        )
        subject = _positive_subject(
            conn,
            account_id=target_account,
            category_id=category_id,
            posted_on="2026-07-02",
        )
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.INTERNAL_TRANSFER.value,
            relationship_kind="transfer_pair",
        )
        assert not repo_close.is_month_locked(conn, "2026-05")
        assert not repo_close.is_month_locked(conn, "2026-07")
        repo_close.mark_closed(
            conn, MONTH, reason="closed statement period regression"
        )
        transaction_ids = [candidate_id, subject["transaction_id"]]
        before = _mutation_snapshot(conn, transaction_ids)
        with pytest.raises(repo_close.MonthLockedError, match=MONTH):
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=proposal["proposal_key"],
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:closed-statement-pair",
                actor="test:operator",
                reason="must fail before endpoint mutation",
            )
        assert _mutation_snapshot(conn, transaction_ids) == before


def test_closed_statement_month_blocks_undo_before_any_endpoint_mutation(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        source_account = _account(conn, "May Undo Endpoint")
        target_account = _account(
            conn, "July Undo Endpoint", kind="savings"
        )
        category_id = _category(conn, "Undo Pair Placeholder")
        candidate_id = _transaction(
            conn,
            account_id=source_account,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.UNKNOWN,
            posted_on="2026-05-31",
            external_id="closed-statement-undo-source",
        )
        subject = _positive_subject(
            conn,
            account_id=target_account,
            category_id=category_id,
            posted_on="2026-07-02",
        )
        event_id = _accept_pair(
            conn,
            subject_id=subject["transaction_id"],
            flow_kind=FlowKind.INTERNAL_TRANSFER.value,
            relationship_kind="transfer_pair",
        )
        acceptance = next(
            row
            for row in positive_flows.list_positive_flow_reviews(conn, MONTH)
            if int(row["subject"]["transaction_id"])
            == subject["transaction_id"]
        )["acceptance"]
        assert acceptance["event_id"] == event_id
        assert not repo_close.is_month_locked(conn, "2026-05")
        assert not repo_close.is_month_locked(conn, "2026-07")
        repo_close.mark_closed(
            conn, MONTH, reason="closed statement period undo regression"
        )
        transaction_ids = [candidate_id, subject["transaction_id"]]
        before = _mutation_snapshot(conn, transaction_ids)
        with pytest.raises(repo_close.MonthLockedError, match=MONTH):
            positive_flows.undo_acceptance(
                conn,
                accept_event_id=event_id,
                month=MONTH,
                evidence_fingerprint=acceptance["undo_fingerprint"],
                operation_key="test:closed-statement-undo",
                actor="test:operator",
                reason="must fail before undo mutation",
            )
        assert _mutation_snapshot(conn, transaction_ids) == before


def test_closed_statement_month_blocks_positive_intake_before_creation(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "July Intake Endpoint")
        intake = _positive_intake_line(
            conn,
            account_id=account_id,
            posted_on="2026-07-02",
            match_status="ignored",
        )
        item = positive_flows.list_positive_flow_intake(conn, MONTH)[0]
        repo_close.mark_closed(
            conn, MONTH, reason="closed statement intake regression"
        )
        before_line = tuple(
            conn.execute(
                """
                SELECT match_status, matched_transaction_id, flow_kind
                FROM statement_lines WHERE id=?
                """,
                (intake["line_id"],),
            ).fetchone()
        )
        with pytest.raises(repo_close.MonthLockedError, match=MONTH):
            positive_flows.recover_positive_line(
                conn,
                statement_line_id=intake["line_id"],
                month=MONTH,
                evidence_fingerprint=item["evidence_fingerprint"],
                operation_key="test:closed-statement-intake",
                actor="test:operator",
                reason="must fail before intake mutation",
            )
        assert tuple(
            conn.execute(
                """
                SELECT match_status, matched_transaction_id, flow_kind
                FROM statement_lines WHERE id=?
                """,
                (intake["line_id"],),
            ).fetchone()
        ) == before_line
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM positive_flow_decision_events"
        ).fetchone()[0] == 0


def test_split_purchase_requires_explicit_offset_category_and_undo_is_exact(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Split Purchase Card", kind="credit")
        placeholder_id = _category(conn, "Positive Review Placeholder")
        category_a = _category(conn, "Travel")
        category_b = _category(conn, "Meals")
        receipt_token = uuid.uuid4().hex
        receipt_id = int(
            conn.execute(
                """
                INSERT INTO source_documents(
                  kind, original_name, storage_ref, sha256, mime_type, status
                ) VALUES (
                  'receipt', 'split-receipt.jpg', ?, ?, 'image/jpeg', 'processed'
                )
                """,
                (
                    f"receipts/{receipt_token}",
                    receipt_token * 2,
                ),
            ).lastrowid
        )
        candidate_id = repo_ledger.insert_transaction(
            conn,
            account_id=account_id,
            posted_on="2026-06-10",
            description="Hotel and dinner",
            counterparty="Synthetic Hotel",
            amount_cents=-10000,
            source="receipt",
            external_id="split-purchase",
            source_document_id=receipt_id,
            source_confidence=1.0,
            flow_kind=FlowKind.PURCHASE,
        )
        assert candidate_id is not None
        repo_ledger.insert_split(
            conn,
            transaction_id=candidate_id,
            category_id=category_a,
            amount_cents=-6000,
            memo="hotel",
        )
        repo_ledger.insert_split(
            conn,
            transaction_id=candidate_id,
            category_id=category_b,
            amount_cents=-4000,
            memo="dinner",
        )
        subject = _positive_subject(
            conn,
            account_id=account_id,
            category_id=placeholder_id,
            amount_cents=4000,
        )
        proposal = _proposal(
            conn,
            subject["transaction_id"],
            flow_kind=FlowKind.REFUND.value,
            relationship_kind="refund_of",
        )
        assert proposal["requires_allocation"] is True
        assert {
            option["category_id"] for option in proposal["allocation_options"]
        } == {category_a, category_b}
        assert proposal["candidate"]["source_document_name"] == (
            "split-receipt.jpg"
        )
        assert proposal["candidate"]["currency"] == "CAD"
        assert {
            (split["category_name"], split["amount_cents"])
            for split in proposal["candidate"]["splits"]
        } == {("Travel", -6000), ("Meals", -4000)}
        transaction_ids = [candidate_id, subject["transaction_id"]]
        before = _mutation_snapshot(conn, transaction_ids)
        with pytest.raises(
            ValueError,
            match="choose the accepted expense category",
        ):
            positive_flows.accept_pair(
                conn,
                subject_transaction_id=subject["transaction_id"],
                month=MONTH,
                proposal_key=proposal["proposal_key"],
                evidence_fingerprint=proposal["evidence_fingerprint"],
                operation_key="test:split-no-allocation",
                actor="test:operator",
                reason="ambiguous allocation must fail",
            )
        assert _mutation_snapshot(conn, transaction_ids) == before

        event_id = positive_flows.accept_pair(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            proposal_key=proposal["proposal_key"],
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:split-explicit",
            actor="test:operator",
            reason="dinner refund offsets meals",
            selected_category_id=category_b,
        )
        assert positive_flows.accept_pair(
            conn,
            subject_transaction_id=subject["transaction_id"],
            month=MONTH,
            proposal_key=proposal["proposal_key"],
            evidence_fingerprint=proposal["evidence_fingerprint"],
            operation_key="test:split-explicit",
            actor="test:operator",
            reason="dinner refund offsets meals",
            selected_category_id=category_b,
        ) == event_id
        for changed_category in (None, category_a):
            with pytest.raises(
                ValueError, match="different positive-flow request"
            ):
                positive_flows.accept_pair(
                    conn,
                    subject_transaction_id=subject["transaction_id"],
                    month=MONTH,
                    proposal_key=proposal["proposal_key"],
                    evidence_fingerprint=proposal["evidence_fingerprint"],
                    operation_key="test:split-explicit",
                    actor="test:operator",
                    reason="dinner refund offsets meals",
                    selected_category_id=changed_category,
                )
        event = conn.execute(
            "SELECT * FROM positive_flow_decision_events WHERE id=?",
            (event_id,),
        ).fetchone()
        assert tuple(
            event[key]
            for key in (
                "action_kind",
                "selected_statement_month",
                "selected_category_id",
                "allocation_explicit",
            )
        ) == ("accept_pair", MONTH, category_b, 1)
        assert {
            row["id"]: row["amount_cents"]
            for row in conn.execute(
                "SELECT id, amount_cents FROM transactions ORDER BY id"
            )
        } == {
            candidate_id: -10000,
            subject["transaction_id"]: 4000,
        }
        subject_split = conn.execute(
            """
            SELECT category_id, amount_cents, memo
            FROM transaction_splits WHERE transaction_id=?
            """,
            (subject["transaction_id"],),
        ).fetchone()
        prior = json.loads(event["prior_subject_splits_json"])[0]
        assert tuple(subject_split) == (
            category_b,
            4000,
            str(prior["memo"]),
        )
        category_totals = {
            int(row["category_id"]): int(row["amount_cents"])
            for row in conn.execute(
                """
                SELECT category_id, amount_cents
                FROM v_category_monthly
                WHERE month=?
                """,
                (MONTH,),
            )
        }
        assert category_totals[category_a] == -6000
        assert category_totals[category_b] == 0

        acceptance = next(
            row
            for row in positive_flows.list_positive_flow_reviews(conn, MONTH)
            if int(row["subject"]["transaction_id"])
            == subject["transaction_id"]
        )["acceptance"]
        positive_flows.undo_acceptance(
            conn,
            accept_event_id=event_id,
            month=MONTH,
            evidence_fingerprint=acceptance["undo_fingerprint"],
            operation_key="test:split-undo",
            actor="test:operator",
            reason="restore exact split state",
        )
        restored = conn.execute(
            """
            SELECT category_id, amount_cents, memo
            FROM transaction_splits WHERE transaction_id=?
            """,
            (subject["transaction_id"],),
        ).fetchone()
        assert tuple(restored) == (
            int(prior["category_id"]),
            int(prior["amount_cents"]),
            str(prior["memo"]),
        )
        category_totals = {
            int(row["category_id"]): int(row["amount_cents"])
            for row in conn.execute(
                """
                SELECT category_id, amount_cents
                FROM v_category_monthly
                WHERE month=?
                """,
                (MONTH,),
            )
        }
        assert category_totals[category_a] == -6000
        assert category_totals[category_b] == -4000


def test_unreconcile_preserves_decision_and_blocks_row_correction_until_undo(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Audited Card", kind="credit")
        category_id = _category(conn, "Audited Purchase")
        subject = _positive_subject(
            conn, account_id=account_id, category_id=category_id
        )
        _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.PURCHASE,
            posted_on="2026-06-01",
            external_id="audited-purchase",
        )
        event_id = _accept_pair(
            conn,
            subject_id=subject["transaction_id"],
            flow_kind="refund",
            relationship_kind="refund_of",
        )
        result = apply.unreconcile_document(conn, subject["document_id"])
        assert result == {"lines": 1, "reset": 1, "removed": 0}
        assert conn.execute(
            "SELECT recon_status FROM transactions WHERE id=?",
            (subject["transaction_id"],),
        ).fetchone()[0] == "uncleared"
        assert conn.execute(
            "SELECT status FROM transaction_relationships"
        ).fetchone()[0] == "active"
        review = positive_flows.list_positive_flow_reviews(conn, MONTH)[0]
        assert review["acceptance"]["event_id"] == event_id
        undo_fingerprint = review["acceptance"]["undo_fingerprint"]
        with pytest.raises(
            sqlite3.IntegrityError, match="undo the positive-flow decision"
        ):
            conn.execute(
                "UPDATE statement_lines SET amount_cents=1100 WHERE id=?",
                (subject["line_id"],),
            )
        positive_flows.undo_acceptance(
            conn,
            accept_event_id=event_id,
            month=MONTH,
            evidence_fingerprint=undo_fingerprint,
            operation_key="test:undo-detached",
            actor="test:operator",
            reason="undo before correcting statement evidence",
        )
        conn.execute(
            "UPDATE statement_lines SET amount_cents=1100 WHERE id=?",
            (subject["line_id"],),
        )
        assert conn.execute(
            "SELECT amount_cents FROM statement_lines WHERE id=?",
            (subject["line_id"],),
        ).fetchone()[0] == 1100
