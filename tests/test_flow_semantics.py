from __future__ import annotations

import sqlite3

import pytest

from app.accounting import flows
from app.accounting.contract import FlowKind
from app.close import checklist
from app.db import engine, repo_ledger


MONTH = "2026-06"


def _account(conn: sqlite3.Connection, name: str, kind: str = "chequing") -> int:
    return int(
        conn.execute(
            """INSERT INTO accounts(name, institution, kind, currency)
               VALUES (?, 'Synthetic Bank', ?, 'CAD')""",
            (name, kind),
        ).lastrowid
    )


def _category(conn: sqlite3.Connection, name: str, kind: str) -> int:
    return int(
        conn.execute(
            """INSERT INTO categories(name, kind, brand_owner)
               VALUES (?, ?, 'shared')""",
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
    external_id: str,
) -> int:
    transaction_id = repo_ledger.insert_transaction(
        conn,
        account_id=account_id,
        posted_on=f"{MONTH}-10",
        description=external_id,
        counterparty="Synthetic Counterparty",
        amount_cents=amount_cents,
        source="test",
        external_id=external_id,
        source_document_id=None,
        source_confidence=1.0,
        flow_kind=flow_kind,
    )
    assert transaction_id is not None
    repo_ledger.insert_split(
        conn,
        transaction_id=transaction_id,
        category_id=category_id,
        amount_cents=amount_cents,
    )
    return transaction_id


def test_schema_rejects_invalid_flow_and_self_relationship(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Primary")
        category_id = _category(conn, "Purchases", "expense")
        purchase_id = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.PURCHASE,
            external_id="schema-purchase",
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transactions(
                     account_id, posted_on, description, amount_cents,
                     source, external_id, flow_kind)
                   VALUES (?, ?, 'invalid', -100, 'test', 'invalid-flow', 'guess')""",
                (account_id, f"{MONTH}-11"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transaction_relationships(
                     relationship_kind, source_transaction_id,
                     target_transaction_id, created_by, reason)
                   VALUES ('refund_of', ?, ?, 'test', 'self edge')""",
                (purchase_id, purchase_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transaction_relationships(
                     relationship_kind, source_transaction_id,
                     target_transaction_id, created_by, reason)
                   VALUES ('unsupported', ?, ?, 'test', 'invalid kind')""",
                (purchase_id, purchase_id + 1),
            )


def test_relationship_transition_is_validated_audited_and_append_only(empty_db):
    with engine.write_tx(empty_db) as conn:
        chequing = _account(conn, "Chequing")
        savings = _account(conn, "Savings", "savings")
        transfer_category = _category(conn, "Transfers", "transfer")
        outgoing = _transaction(
            conn,
            account_id=chequing,
            category_id=transfer_category,
            amount_cents=-5000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="pair-out",
        )
        incoming = _transaction(
            conn,
            account_id=savings,
            category_id=transfer_category,
            amount_cents=5000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="pair-in",
        )
        relationship_id = flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
            source_transaction_id=outgoing,
            target_transaction_id=incoming,
            actor="test:user",
            reason="confirmed owned-account transfer",
        )

        with pytest.raises(sqlite3.IntegrityError, match="created active"):
            conn.execute(
                """INSERT INTO transaction_relationships(
                     relationship_kind, source_transaction_id,
                     target_transaction_id, status, created_by, reason,
                     revoked_at, revoked_by, revocation_reason)
                   VALUES (
                     'transfer_pair', ?, ?, 'revoked', 'test:user',
                     'synthetic history', CURRENT_TIMESTAMP, 'test:user',
                     'bypassed lifecycle'
                   )""",
                (outgoing, incoming),
            )
        with pytest.raises(ValueError, match="equal-and-opposite"):
            flows.create_relationship(
                conn,
                relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
                source_transaction_id=incoming,
                target_transaction_id=outgoing,
                actor="test:user",
                reason="reverse duplicate",
            )
        with pytest.raises(sqlite3.IntegrityError, match="invalidate active relationship"):
            conn.execute(
                "UPDATE transactions SET amount_cents=4000 WHERE id=?",
                (incoming,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM transaction_relationships WHERE id=?",
                (relationship_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE transaction_relationships SET reason='rewritten history' WHERE id=?",
                (relationship_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                """UPDATE transaction_relationships
                   SET target_transaction_id=?, relationship_kind='payment_for'
                   WHERE id=?""",
                (outgoing, relationship_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                """UPDATE transaction_relationships
                   SET id=id + 100000, status='revoked',
                       revoked_at=CURRENT_TIMESTAMP, revoked_by='test:user',
                       revocation_reason='rewrite row identity'
                   WHERE id=?""",
                (relationship_id,),
            )

        flows.revoke_relationship(
            conn,
            relationship_id,
            actor="test:user",
            reason="pair was linked to the wrong statement line",
        )
        relationship = conn.execute(
            "SELECT * FROM transaction_relationships WHERE id=?",
            (relationship_id,),
        ).fetchone()
        assert relationship["status"] == "revoked"
        assert relationship["created_by"] == "test:user"
        assert relationship["revoked_by"] == "test:user"
        assert relationship["revoked_at"]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                """UPDATE transaction_relationships
                   SET status='active', revoked_at=NULL,
                       revoked_by='', revocation_reason=''
                   WHERE id=?""",
                (relationship_id,),
            )


def test_transfer_leg_can_belong_to_only_one_oriented_pair(empty_db):
    with engine.write_tx(empty_db) as conn:
        chequing = _account(conn, "Pair Source")
        savings_one = _account(conn, "Pair Target One", "savings")
        savings_two = _account(conn, "Pair Target Two", "savings")
        category = _category(conn, "Pair Transfers", "transfer")
        outgoing = _transaction(
            conn,
            account_id=chequing,
            category_id=category,
            amount_cents=-5000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="single-pair-out",
        )
        incoming_one = _transaction(
            conn,
            account_id=savings_one,
            category_id=category,
            amount_cents=5000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="single-pair-in-one",
        )
        incoming_two = _transaction(
            conn,
            account_id=savings_two,
            category_id=category,
            amount_cents=5000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="single-pair-in-two",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
            source_transaction_id=outgoing,
            target_transaction_id=incoming_one,
            actor="test:user",
            reason="first confirmed pair",
        )

        with pytest.raises(ValueError, match="only one active pair"):
            flows.create_relationship(
                conn,
                relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
                source_transaction_id=outgoing,
                target_transaction_id=incoming_two,
                actor="test:user",
                reason="invalid second pair",
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transaction_relationships(
                     relationship_kind, source_transaction_id,
                     target_transaction_id, created_by, reason)
                   VALUES ('transfer_pair', ?, ?, 'raw:test', 'cross-direction')""",
                (incoming_two, outgoing),
            )


def test_offsets_are_capped_in_aggregate_and_reversal_is_exclusive(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Offset Account")
        category_id = _category(conn, "Offset Purchases", "expense")
        purchase = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=-10000,
            flow_kind=FlowKind.PURCHASE,
            external_id="offset-target",
        )
        refund = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=8000,
            flow_kind=FlowKind.REFUND,
            external_id="offset-refund",
        )
        reimbursement = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=3000,
            flow_kind=FlowKind.REIMBURSEMENT,
            external_id="offset-reimbursement",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            source_transaction_id=refund,
            target_transaction_id=purchase,
            actor="test:user",
            reason="partial refund",
        )

        with pytest.raises(ValueError, match="aggregate refunds/reimbursements"):
            flows.create_relationship(
                conn,
                relationship_kind=flows.RelationshipKind.REIMBURSEMENT_FOR,
                source_transaction_id=reimbursement,
                target_transaction_id=purchase,
                actor="test:user",
                reason="would over-offset",
            )
        with pytest.raises(sqlite3.IntegrityError, match="aggregate offsets"):
            conn.execute(
                """INSERT INTO transaction_relationships(
                     relationship_kind, source_transaction_id,
                     target_transaction_id, created_by, reason)
                   VALUES ('reimbursement_for', ?, ?, 'raw:test', 'over cap')""",
                (reimbursement, purchase),
            )

        second_purchase = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=-1000,
            flow_kind=FlowKind.PURCHASE,
            external_id="reversal-target",
        )
        reversal = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=1000,
            flow_kind=FlowKind.REVERSAL,
            external_id="reversal-one",
        )
        second_reversal = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=1000,
            flow_kind=FlowKind.REVERSAL,
            external_id="reversal-two",
        )
        post_reversal_refund = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=500,
            flow_kind=FlowKind.REFUND,
            external_id="refund-after-reversal",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REVERSAL_OF,
            source_transaction_id=reversal,
            target_transaction_id=second_purchase,
            actor="test:user",
            reason="full reversal",
        )
        with pytest.raises(ValueError, match="exclusive"):
            flows.create_relationship(
                conn,
                relationship_kind=flows.RelationshipKind.REVERSAL_OF,
                source_transaction_id=second_reversal,
                target_transaction_id=second_purchase,
                actor="test:user",
                reason="duplicate reversal",
            )
        with pytest.raises(ValueError, match="cannot also receive partial offsets"):
            flows.create_relationship(
                conn,
                relationship_kind=flows.RelationshipKind.REFUND_OF,
                source_transaction_id=post_reversal_refund,
                target_transaction_id=second_purchase,
                actor="test:user",
                reason="invalid refund after reversal",
            )


def test_v1_reversal_rejects_negative_source_and_income_target(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Reversal Account")
        expense_category = _category(conn, "Reversal Expense", "expense")
        income_category = _category(conn, "Reversal Income", "income")
        with pytest.raises(ValueError, match="requires a positive amount"):
            _transaction(
                conn,
                account_id=account_id,
                category_id=expense_category,
                amount_cents=-1000,
                flow_kind=FlowKind.REVERSAL,
                external_id="negative-reversal",
            )

        income = _transaction(
            conn,
            account_id=account_id,
            category_id=income_category,
            amount_cents=1000,
            flow_kind=FlowKind.INCOME,
            external_id="income-reversal-target",
        )
        raw_reversal = int(
            conn.execute(
                """INSERT INTO transactions(
                     account_id, posted_on, description, amount_cents,
                     source, external_id, flow_kind)
                   VALUES (?, ?, 'raw negative reversal', -1000,
                           'test', 'raw-negative-reversal', 'reversal')""",
                (account_id, f"{MONTH}-11"),
            ).lastrowid
        )
        with pytest.raises(ValueError, match="positive reversal"):
            flows.create_relationship(
                conn,
                relationship_kind=flows.RelationshipKind.REVERSAL_OF,
                source_transaction_id=raw_reversal,
                target_transaction_id=income,
                actor="test:user",
                reason="invalid income reversal",
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO transaction_relationships(
                     relationship_kind, source_transaction_id,
                     target_transaction_id, created_by, reason)
                   VALUES ('reversal_of', ?, ?, 'raw:test', 'invalid income reversal')""",
                (raw_reversal, income),
            )


def test_unknown_flow_enters_review_and_resolution_is_audited(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Review Account")
        category_id = _category(conn, "Review Expense", "expense")
        transaction_id = _transaction(
            conn,
            account_id=account_id,
            category_id=category_id,
            amount_cents=-2500,
            flow_kind=FlowKind.UNKNOWN,
            external_id="review-me",
        )
        pending = flows.pending_flow_reviews(conn)
        assert [int(row["transaction_id"]) for row in pending] == [transaction_id]

        assert flows.set_flow_kind(
            conn,
            transaction_id,
            FlowKind.PURCHASE,
            actor="test:reviewer",
            reason="receipt proves a purchase",
        )
        review = conn.execute(
            "SELECT * FROM transaction_flow_reviews WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()
        assert review["status"] == "resolved"
        assert review["resolved_by"] == "test:reviewer"
        assert review["resolution_flow_kind"] == "purchase"
        audit = conn.execute(
            "SELECT * FROM transaction_flow_audit WHERE transaction_id=?",
            (transaction_id,),
        ).fetchall()
        assert [
            (row["old_flow_kind"], row["new_flow_kind"], row["actor"])
            for row in audit
        ] == [("unknown", "purchase", "test:reviewer")]

        with pytest.raises(ValueError, match="requires a positive amount"):
            flows.set_flow_kind(
                conn,
                transaction_id,
                FlowKind.REFUND,
                actor="test:reviewer",
                reason="invalid mutation",
            )


def test_deterministic_backfill_is_repeatable_and_conservative(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Legacy Account")
        rows = (
            ("legacy-receipt", "receipt", -1000),
            ("legacy-positive-receipt", "receipt", 1000),
            ("legacy-opening", "opening", 5000),
            ("legacy-adjustment", "adjustment", -50),
            ("legacy-manual", "manual", -700),
            ("legacy-statement", "statement", 800),
        )
        for external_id, source, amount_cents in rows:
            conn.execute(
                """INSERT INTO transactions(
                     account_id, posted_on, description, amount_cents,
                     source, external_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    f"{MONTH}-12",
                    external_id,
                    amount_cents,
                    source,
                    external_id,
                ),
            )

        assert flows.backfill_deterministic_flow_kinds(conn) == 3
        assert flows.backfill_deterministic_flow_kinds(conn) == 0
        observed = {
            row["external_id"]: row["flow_kind"]
            for row in conn.execute(
                "SELECT external_id, flow_kind FROM transactions ORDER BY id"
            )
        }
        assert observed == {
            "legacy-receipt": "purchase",
            "legacy-positive-receipt": "unknown",
            "legacy-opening": "opening",
            "legacy-adjustment": "adjustment",
            "legacy-manual": "unknown",
            "legacy-statement": "unknown",
        }
        pending = {
            row["description"] for row in flows.pending_flow_reviews(conn)
        }
        assert pending == {
            "legacy-positive-receipt",
            "legacy-manual",
            "legacy-statement",
        }


def test_typed_reports_exclude_unknown_and_transfer_and_net_refunds(empty_db):
    with engine.write_tx(empty_db) as conn:
        chequing = _account(conn, "Reporting Chequing")
        savings = _account(conn, "Reporting Savings", "savings")
        income_category = _category(conn, "Salary", "income")
        expense_category = _category(conn, "Groceries", "expense")
        transfer_category = _category(conn, "Owned Transfer", "transfer")

        _transaction(
            conn,
            account_id=chequing,
            category_id=income_category,
            amount_cents=10000,
            flow_kind=FlowKind.INCOME,
            external_id="report-income",
        )
        purchase = _transaction(
            conn,
            account_id=chequing,
            category_id=expense_category,
            amount_cents=-5000,
            flow_kind=FlowKind.PURCHASE,
            external_id="report-purchase",
        )
        refund = _transaction(
            conn,
            account_id=chequing,
            category_id=expense_category,
            amount_cents=1000,
            flow_kind=FlowKind.REFUND,
            external_id="report-refund",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            source_transaction_id=refund,
            target_transaction_id=purchase,
            actor="test:user",
            reason="partial grocery refund",
        )
        transfer_out = _transaction(
            conn,
            account_id=chequing,
            category_id=transfer_category,
            amount_cents=-2000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="report-transfer-out",
        )
        transfer_in = _transaction(
            conn,
            account_id=savings,
            category_id=transfer_category,
            amount_cents=2000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="report-transfer-in",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
            source_transaction_id=transfer_out,
            target_transaction_id=transfer_in,
            actor="test:user",
            reason="owned-account transfer",
        )
        _transaction(
            conn,
            account_id=chequing,
            category_id=income_category,
            amount_cents=777,
            flow_kind=FlowKind.UNKNOWN,
            external_id="report-ambiguous-positive",
        )

    with engine.read_conn(empty_db) as conn:
        report = conn.execute(
            "SELECT * FROM v_cashflow_monthly WHERE month=?",
            (MONTH,),
        ).fetchone()
        assert dict(report) == {
            "month": MONTH,
            "income_cents": 10000,
            "expense_cents": 4000,
            "net_cents": 6000,
        }
        pending_ids = {
            int(row["transaction_id"]) for row in flows.pending_flow_reviews(conn)
        }
        ambiguous_id = conn.execute(
            "SELECT id FROM transactions WHERE external_id='report-ambiguous-positive'"
        ).fetchone()["id"]
        assert pending_ids == {int(ambiguous_id)}


def test_relationship_required_flow_is_excluded_and_blocks_close_until_linked(empty_db):
    with engine.write_tx(empty_db) as conn:
        chequing = _account(conn, "Completeness Chequing")
        savings = _account(conn, "Completeness Savings", "savings")
        expense_category = _category(conn, "Completeness Expense", "expense")
        transfer_category = _category(conn, "Completeness Transfer", "transfer")
        purchase = _transaction(
            conn,
            account_id=chequing,
            category_id=expense_category,
            amount_cents=-5000,
            flow_kind=FlowKind.PURCHASE,
            external_id="completeness-purchase",
        )
        refund = _transaction(
            conn,
            account_id=chequing,
            category_id=expense_category,
            amount_cents=1000,
            flow_kind=FlowKind.REFUND,
            external_id="completeness-refund",
        )
        transfer_out = _transaction(
            conn,
            account_id=chequing,
            category_id=transfer_category,
            amount_cents=-2000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="completeness-transfer-out",
        )
        transfer_in = _transaction(
            conn,
            account_id=savings,
            category_id=transfer_category,
            amount_cents=2000,
            flow_kind=FlowKind.INTERNAL_TRANSFER,
            external_id="completeness-transfer-in",
        )

        statuses = {
            int(row["transaction_id"]): row["semantic_status"]
            for row in conn.execute(
                """SELECT * FROM v_transaction_flow_status
                   WHERE transaction_id IN (?, ?, ?)""",
                (refund, transfer_out, transfer_in),
            )
        }
        assert statuses == {
            refund: "missing_relationship",
            transfer_out: "missing_relationship",
            transfer_in: "missing_relationship",
        }
        report_before = conn.execute(
            "SELECT * FROM v_cashflow_monthly WHERE month=?",
            (MONTH,),
        ).fetchone()
        assert report_before["expense_cents"] == 5000
        close_before = checklist.build_checklist(conn, MONTH)
        assert close_before["flow_review_count"] == 3
        assert close_before["has_blocking_flow_reviews"] is True

        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.REFUND_OF,
            source_transaction_id=refund,
            target_transaction_id=purchase,
            actor="test:user",
            reason="confirmed refund",
        )
        flows.create_relationship(
            conn,
            relationship_kind=flows.RelationshipKind.TRANSFER_PAIR,
            source_transaction_id=transfer_out,
            target_transaction_id=transfer_in,
            actor="test:user",
            reason="confirmed owned transfer",
        )

        report_after = conn.execute(
            "SELECT * FROM v_cashflow_monthly WHERE month=?",
            (MONTH,),
        ).fetchone()
        assert report_after["expense_cents"] == 4000
        close_after = checklist.build_checklist(conn, MONTH)
        assert close_after["flow_review_count"] == 0
        assert close_after["has_blocking_flow_reviews"] is False


def test_every_relationship_required_flow_is_incomplete_without_provenance(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn, "Required Flow Account")
        expense_category = _category(conn, "Required Flow Expense", "expense")
        transfer_category = _category(conn, "Required Flow Transfer", "transfer")
        cases = (
            (FlowKind.INTERNAL_TRANSFER, -1000, transfer_category),
            (FlowKind.CARD_PAYMENT, -1000, transfer_category),
            (FlowKind.REFUND, 1000, expense_category),
            (FlowKind.REIMBURSEMENT, 1000, expense_category),
            (FlowKind.REVERSAL, 1000, expense_category),
        )
        ids = [
            _transaction(
                conn,
                account_id=account_id,
                category_id=category_id,
                amount_cents=amount_cents,
                flow_kind=flow_kind,
                external_id=f"required-{flow_kind.value}",
            )
            for flow_kind, amount_cents, category_id in cases
        ]
        rows = conn.execute(
            f"""SELECT transaction_id, semantic_status, report_eligible
                FROM v_transaction_flow_status
                WHERE transaction_id IN ({','.join('?' for _ in ids)})
                ORDER BY transaction_id""",
            ids,
        ).fetchall()
        assert [
            (row["semantic_status"], int(row["report_eligible"]))
            for row in rows
        ] == [("missing_relationship", 0)] * len(cases)
