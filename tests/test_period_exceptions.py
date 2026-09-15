from __future__ import annotations

from app.close import period_exceptions
from app.db import (
    engine,
    repo_assertions,
    repo_ledger,
    repo_statement_expectations,
)


def _account(conn, name: str = "Checking") -> int:
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
    amount_cents: int,
    flow_kind: str,
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


def test_collector_emits_typed_manual_category_balance_and_statement_exceptions(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        repo_statement_expectations.record_policy(
            conn,
            account_id=account_id,
            effective_from_month="2026-07",
            configuration_state="configured",
            requirement_mode="required",
            cadence="monthly",
            actor="test:operator",
            reason="synthetic monthly statement policy",
        )
        _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-03",
            amount_cents=-1250,
            flow_kind="purchase",
            external_id="purchase:1",
        )
        _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-04",
            amount_cents=300,
            flow_kind="adjustment",
            external_id="adjustment:1",
        )
        repo_assertions.record_assertion(
            conn,
            account_id=account_id,
            asof_date="2026-07-31",
            asserted_cents=0,
            statement_period="2026-07",
        )

    with engine.read_conn(empty_db) as conn:
        exceptions = period_exceptions.collect_period_exceptions(conn, "2026-07")
        snapshot = period_exceptions.build_close_snapshot(
            conn,
            "2026-07",
            exceptions,
        )

    types = {item["exception_type"] for item in exceptions}
    assert {
        "missing_statement",
        "unconfirmed_merchant_category",
        "unexplained_balance_delta",
        "manual_adjustment",
    } <= types
    assert snapshot["close_state"] == "closed_with_exceptions"
    assert snapshot["exception_count"] == len(exceptions)
    assert snapshot["planning_signals_are_non_authoritative"] is True


def test_automatic_final_match_is_evidence_gap_while_authority_disabled(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        transaction_id, _ = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-06",
            amount_cents=-900,
            flow_kind="purchase",
            external_id="match:1",
        )
        document_id = int(
            conn.execute(
                """INSERT INTO source_documents(
                     kind, original_name, storage_ref, sha256, mime_type, status
                   ) VALUES (
                     'statement', 'synthetic.pdf', 'local:synthetic',
                     'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                     'application/pdf', 'processed'
                   )"""
            ).lastrowid
        )
        conn.execute(
            """INSERT INTO statement_lines(
                 source_document_id, account_id, posted_on, raw_description,
                 norm_merchant, amount_cents, currency, statement_period,
                 row_hash, match_status, matched_transaction_id, match_method,
                 match_score, match_rationale, flow_kind
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document_id,
                account_id,
                "2026-07-06",
                "SYNTHETIC",
                "synthetic",
                -900,
                "CAD",
                "2026-07",
                "automatic-authority-row",
                "matched",
                transaction_id,
                "exact",
                1.0,
                "controlled scorer result",
                "purchase",
            ),
        )

    with engine.read_conn(empty_db) as conn:
        exceptions = period_exceptions.collect_period_exceptions(conn, "2026-07")

    automatic = [
        item
        for item in exceptions
        if item["subject_kind"] == "automatic_match"
    ]
    assert len(automatic) == 1
    assert automatic[0]["exception_type"] == "evidence_gap"
    assert (
        automatic[0]["evidence"]["automation_authority"]
        == "disabled_pending_production_evidence"
    )


def test_pending_negative_transaction_flow_prevents_clean_close(empty_db):
    with engine.write_tx(empty_db) as conn:
        account_id = _account(conn)
        transaction_id, _ = _transaction(
            conn,
            account_id=account_id,
            posted_on="2026-07-08",
            amount_cents=-450,
            flow_kind="unknown",
            external_id="unknown-flow:1",
        )

    with engine.read_conn(empty_db) as conn:
        exceptions = period_exceptions.collect_period_exceptions(conn, "2026-07")
        snapshot = period_exceptions.build_close_snapshot(
            conn,
            "2026-07",
            exceptions,
        )

    flow = [
        item
        for item in exceptions
        if item["subject_kind"] == "transaction_flow"
        and item["subject_id"] == str(transaction_id)
    ]
    assert len(flow) == 1
    assert flow[0]["exception_type"] == "evidence_gap"
    assert flow[0]["evidence"]["semantic_status"] == "unknown"
    assert snapshot["close_state"] == "closed_with_exceptions"
