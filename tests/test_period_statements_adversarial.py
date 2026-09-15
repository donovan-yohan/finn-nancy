from __future__ import annotations

import csv
import io
import re

import fitz
import pytest

from app.accounting.flows import create_relationship, revoke_relationship
from app.db import (
    engine,
    repo_assertions,
    repo_merchant_knowledge,
    repo_period_policy,
    repo_period_statements,
)
from app.db.repo_merchant_knowledge import Evidence
from app.reconcile.merchant_resolution import resolve_descriptor
from app.reporting import period_statements as period_statement_builder
from app.reporting.exports import (
    UnsupportedPdfGlyphError,
    render_period_statement_csv,
    render_period_statement_pdf,
)
from app.reporting.models import EvidenceSet
from app.reporting.period_statements import (
    UnsupportedReportCurrency,
    build_period_statement,
    period_statement_snapshot_payload,
)


MONTH = "2026-06"


def _category(conn, name: str, kind: str) -> int:
    return int(
        conn.execute(
            "INSERT INTO categories(name, kind, brand_owner) VALUES (?, ?, 'shared')",
            (name, kind),
        ).lastrowid
    )


def _account(conn, name: str, kind: str, *, currency: str = "CAD") -> int:
    return int(
        conn.execute(
            """
            INSERT INTO accounts(name, institution, kind, currency)
            VALUES (?, 'Adversarial Bank', ?, ?)
            """,
            (name, kind, currency),
        ).lastrowid
    )


def _document(conn, key: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO source_documents(
              kind, original_name, storage_ref, sha256, mime_type, status
            )
            VALUES ('statement', ?, ?, ?, 'application/pdf', 'matched')
            """,
            (
                f"{key}.pdf",
                f"fixture/{key}.pdf",
                key.encode().hex().ljust(64, "0")[:64],
            ),
        ).lastrowid
    )


def _row(
    conn,
    *,
    account_id: int,
    category_id: int,
    posted_on: str,
    cents: int,
    flow_kind: str,
    key: str,
    document_id: int | None = None,
    statement_currency: str = "CAD",
    description: str | None = None,
) -> tuple[int, int, int | None]:
    descriptor = key if description is None else description
    transaction_id = int(
        conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, counterparty, amount_cents,
              source_document_id, source, statement_period, external_id,
              recon_status, cleared_on, flow_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'cleared', ?, ?)
            """,
            (
                account_id,
                posted_on,
                descriptor,
                descriptor,
                cents,
                document_id,
                "statement" if document_id is not None else "manual",
                posted_on[:7],
                f"adversarial:{key}",
                posted_on,
                flow_kind,
            ),
        ).lastrowid
    )
    split_id = int(
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (?, ?, ?)
            """,
            (transaction_id, category_id, cents),
        ).lastrowid
    )
    if document_id is None:
        return transaction_id, split_id, None
    statement_line_id = int(
        conn.execute(
            """
            INSERT INTO statement_lines(
              source_document_id, account_id, posted_on, raw_description,
              norm_merchant, amount_cents, currency, statement_period,
              row_hash, match_status, matched_transaction_id, match_method,
              match_score, match_rationale, flow_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'matched', ?, 'manual',
                    100, 'adversarial exact row', ?)
            """,
            (
                document_id,
                account_id,
                posted_on,
                descriptor,
                descriptor.casefold(),
                cents,
                statement_currency,
                MONTH,
                f"row:{key}",
                transaction_id,
                flow_kind,
            ),
        ).lastrowid
    )
    return transaction_id, split_id, statement_line_id


def test_opening_balance_sums_every_pre_period_split_with_exact_evidence(empty_db):
    with engine.write_tx(empty_db) as conn:
        opening = _category(conn, "Opening", "transfer")
        expense = _category(conn, "Expense", "expense")
        account = _account(conn, "Cash", "cash")
        first = _row(
            conn,
            account_id=account,
            category_id=opening,
            posted_on="2025-12-31",
            cents=10_000,
            flow_kind="opening",
            key="opening-first",
        )
        second = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-04-15",
            cents=-2_500,
            flow_kind="purchase",
            key="opening-spend",
        )
        third = _row(
            conn,
            account_id=account,
            category_id=opening,
            posted_on="2026-05-31",
            cents=300,
            flow_kind="opening",
            key="opening-last",
        )

    statement = build_period_statement(empty_db, month=MONTH, home_currency="CAD")
    account_statement = statement.accounts[0]

    assert account_statement.opening_balance.cents == 7_800
    assert account_statement.opening_balance.evidence.transaction_ids == tuple(
        row[0] for row in (first, second, third)
    )
    assert account_statement.opening_balance.evidence.transaction_split_ids == tuple(
        row[1] for row in (first, second, third)
    )


def test_internal_transfer_and_card_payment_are_household_neutral_but_keep_legs(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        transfer = _category(conn, "Transfer", "transfer")
        chequing = _account(conn, "Chequing", "chequing")
        savings = _account(conn, "Savings", "savings")
        credit = _account(conn, "Credit", "credit")
        transfer_out = _row(
            conn,
            account_id=chequing,
            category_id=transfer,
            posted_on="2026-06-10",
            cents=-50_000,
            flow_kind="internal_transfer",
            key="transfer-out",
        )[0]
        transfer_in = _row(
            conn,
            account_id=savings,
            category_id=transfer,
            posted_on="2026-06-10",
            cents=50_000,
            flow_kind="internal_transfer",
            key="transfer-in",
        )[0]
        payment_out = _row(
            conn,
            account_id=chequing,
            category_id=transfer,
            posted_on="2026-06-20",
            cents=-30_000,
            flow_kind="card_payment",
            key="payment-out",
        )[0]
        payment_in = _row(
            conn,
            account_id=credit,
            category_id=transfer,
            posted_on="2026-06-20",
            cents=30_000,
            flow_kind="card_payment",
            key="payment-in",
        )[0]
        transfer_relationship = create_relationship(
            conn,
            relationship_kind="transfer_pair",
            source_transaction_id=transfer_out,
            target_transaction_id=transfer_in,
            actor="operator:test",
            reason="paired transfer fixture",
        )
        payment_relationship = create_relationship(
            conn,
            relationship_kind="transfer_pair",
            source_transaction_id=payment_out,
            target_transaction_id=payment_in,
            actor="operator:test",
            reason="paired card payment fixture",
        )

    statement = build_period_statement(empty_db, month=MONTH, home_currency="CAD")

    assert statement.household.income.cents == 0
    assert statement.household.gross_money_out.cents == 0
    assert statement.household.refunds.cents == 0
    assert statement.household.net_money_out.cents == 0
    assert statement.household.external_cash_movement.cents == 0
    assert statement.household.transfer_neutrality_control.cents == 0
    assert {int(row.transaction_id) for row in statement.rows} == {
        transfer_out,
        transfer_in,
        payment_out,
        payment_in,
    }
    assert set(statement.household.transfer_neutrality_control.evidence.relationship_ids) == {
        transfer_relationship,
        payment_relationship,
    }
    by_account = {account.account_id: account for account in statement.accounts}
    assert by_account[chequing].transfers_out.cents == 80_000
    assert by_account[savings].transfers_in.cents == 50_000
    assert by_account[credit].transfers_in.cents == 30_000


def test_offsets_count_once_and_missing_pair_cannot_reduce_money_out(empty_db):
    with engine.write_tx(empty_db) as conn:
        expense = _category(conn, "Expense", "expense")
        account = _account(conn, "Credit", "credit")
        purchase = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-01",
            cents=-10_000,
            flow_kind="purchase",
            key="purchase",
        )[0]
        refund = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-02",
            cents=2_000,
            flow_kind="refund",
            key="refund",
        )[0]
        reimbursement = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-03",
            cents=1_000,
            flow_kind="reimbursement",
            key="reimbursement",
        )[0]
        missing_pair = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-04",
            cents=500,
            flow_kind="refund",
            key="missing-pair",
        )[0]
        create_relationship(
            conn,
            relationship_kind="refund_of",
            source_transaction_id=refund,
            target_transaction_id=purchase,
            actor="operator:test",
            reason="partial refund fixture",
        )
        create_relationship(
            conn,
            relationship_kind="reimbursement_for",
            source_transaction_id=reimbursement,
            target_transaction_id=purchase,
            actor="operator:test",
            reason="partial reimbursement fixture",
        )

    statement = build_period_statement(empty_db, month=MONTH, home_currency="CAD")

    assert statement.household.gross_money_out.cents == 10_000
    assert statement.household.refunds.cents == 3_000
    assert statement.household.net_money_out.cents == 7_000
    assert missing_pair not in statement.household.refunds.evidence.transaction_ids
    assert missing_pair in statement.household.unclassified_movement.evidence.transaction_ids


def test_aggregate_offset_over_pair_is_rejected_before_report(empty_db):
    with engine.write_tx(empty_db) as conn:
        expense = _category(conn, "Expense", "expense")
        account = _account(conn, "Credit", "credit")
        purchase = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-01",
            cents=-5_000,
            flow_kind="purchase",
            key="over-purchase",
        )[0]
        first = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-02",
            cents=3_000,
            flow_kind="refund",
            key="over-first",
        )[0]
        second = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-03",
            cents=3_000,
            flow_kind="reimbursement",
            key="over-second",
        )[0]
        create_relationship(
            conn,
            relationship_kind="refund_of",
            source_transaction_id=first,
            target_transaction_id=purchase,
            actor="operator:test",
            reason="first partial offset",
        )
        with pytest.raises(
            ValueError,
            match="aggregate refunds/reimbursements exceed",
        ):
            create_relationship(
                conn,
                relationship_kind="reimbursement_for",
                source_transaction_id=second,
                target_transaction_id=purchase,
                actor="operator:test",
                reason="would over-allocate purchase",
            )


@pytest.mark.parametrize("account_currency", ["", "CA", "EUR"])
def test_invalid_account_currency_rejects_entire_report(empty_db, account_currency):
    with engine.write_tx(empty_db) as conn:
        expense = _category(conn, "Expense", "expense")
        account = _account(conn, "Invalid currency", "cash", currency=account_currency)
        _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-01",
            cents=-100,
            flow_kind="purchase",
            key=f"account-currency-{account_currency or 'blank'}",
        )

    with pytest.raises(UnsupportedReportCurrency):
        build_period_statement(empty_db, month=MONTH, home_currency="CAD")


def test_reusable_resolution_has_no_report_authority_until_split_human_disposition(
    empty_db,
):
    descriptor = "SQ *CAFÉ CENTRAL 0042"
    with engine.write_tx(empty_db) as conn:
        expense = _category(conn, "Meals", "expense")
        account = _account(conn, "Daily card", "credit")
        document = _document(conn, "authority")
        accepted_transaction, accepted_split, accepted_line = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-01",
            cents=-1_000,
            flow_kind="purchase",
            key="authority-accepted",
            document_id=document,
            description=descriptor,
        )
        reusable_transaction, reusable_split, reusable_line = _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-02",
            cents=-2_000,
            flow_kind="purchase",
            key="authority-reusable",
            document_id=document,
            description=descriptor,
        )
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=account,
            processor_family="square",
            region="CA-ON",
        )
        repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=descriptor,
            canonical_name="Café Central",
            scope=scope,
            operation_key="adversarial:authority:merchant:accepted",
            actor="operator:test",
            reason="human accepted the first transaction merchant",
            evidence=Evidence(
                statement_line_id=accepted_line,
                transaction_id=accepted_transaction,
            ),
        )
        accepted_category_claim = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=descriptor,
            category_id=expense,
            scope=scope,
            operation_key="adversarial:authority:category:accepted",
            actor="operator:test",
            reason="human accepted the first transaction split category",
            evidence=Evidence(
                statement_line_id=accepted_line,
                transaction_id=accepted_transaction,
                transaction_split_id=accepted_split,
            ),
        )
        reusable_resolution = resolve_descriptor(
            conn,
            descriptor=descriptor,
            scope=scope,
        )

    assert reusable_resolution.merchant.status == "resolved"
    assert reusable_resolution.category.status == "resolved"
    assert accepted_category_claim in reusable_resolution.category.claim_ids
    assert reusable_resolution.merchant.automatic_assignment_allowed is False
    assert reusable_resolution.category.automatic_assignment_allowed is False

    before_disposition = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    before_rows = {
        row.transaction_id: row for row in before_disposition.rows
    }
    assert before_rows[accepted_transaction].resolution_disposition == "human_approved"
    assert before_rows[accepted_transaction].canonical_merchant == "Café Central"
    assert before_rows[reusable_transaction].resolution_disposition == "unresolved"
    assert before_rows[reusable_transaction].canonical_merchant is None
    assert (
        before_disposition.expense_resolution.human_approved.cents == 1_000
    )
    assert before_disposition.expense_resolution.unresolved.cents == 2_000
    assert (
        accepted_category_claim
        not in before_rows[reusable_transaction].evidence.category_claim_ids
    )

    with engine.write_tx(empty_db) as conn:
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=account,
            processor_family="square",
            region="CA-ON",
        )
        reusable_merchant_claim = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=descriptor,
            canonical_name="Café Central",
            scope=scope,
            operation_key="adversarial:authority:merchant:reusable",
            actor="operator:test",
            reason="human accepted the second transaction merchant",
            evidence=Evidence(
                statement_line_id=reusable_line,
                transaction_id=reusable_transaction,
            ),
        )
        reusable_category_claim = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=descriptor,
            category_id=expense,
            scope=scope,
            operation_key="adversarial:authority:category:reusable",
            actor="operator:test",
            reason="human accepted the second transaction split category",
            evidence=Evidence(
                statement_line_id=reusable_line,
                transaction_id=reusable_transaction,
                transaction_split_id=reusable_split,
            ),
        )

    after_disposition = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    accepted_reusable = next(
        row
        for row in after_disposition.rows
        if row.transaction_id == reusable_transaction
    )
    assert accepted_reusable.resolution_disposition == "human_approved"
    assert accepted_reusable.canonical_merchant == "Café Central"
    assert accepted_reusable.evidence.canonical_merchant_claim_ids == (
        reusable_merchant_claim,
    )
    assert accepted_reusable.evidence.category_claim_ids == (
        reusable_category_claim,
    )
    assert after_disposition.expense_resolution.human_approved.cents == 3_000
    assert after_disposition.expense_resolution.unresolved.cents == 0


def test_tied_rows_have_fixed_order_and_exact_unicode_csv_evidence(empty_db):
    first_description = "Café, Inc.\nNorth counter"
    second_description = "東京市場, 地下\nTerminal 2"
    with engine.write_tx(empty_db) as conn:
        expense = _category(conn, "Repas, café\nÉquipe", "expense")
        first_account = _account(conn, "Zulu,\nCompte", "credit")
        second_account = _account(conn, "Ångström compte", "credit")
        first_document = _document(conn, "unicode-first")
        second_document = _document(conn, "unicode-second")
        first = _row(
            conn,
            account_id=first_account,
            category_id=expense,
            posted_on="2026-06-15",
            cents=-1_111,
            flow_kind="purchase",
            key="unicode-first",
            document_id=first_document,
            description=first_description,
        )
        second_account_row = _row(
            conn,
            account_id=second_account,
            category_id=expense,
            posted_on="2026-06-15",
            cents=-2_222,
            flow_kind="purchase",
            key="unicode-second",
            document_id=second_document,
            description=second_description,
        )
        tied = _row(
            conn,
            account_id=first_account,
            category_id=expense,
            posted_on="2026-06-15",
            cents=-3_333,
            flow_kind="purchase",
            key="unicode-tied",
            document_id=first_document,
            description="AAA sorts before text but not before transaction ID",
        )
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=first_account,
            processor_family="square",
            region="CA-QC",
        )
        merchant_claim = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=first_description,
            canonical_name="Café, Inc.\nMontréal",
            scope=scope,
            operation_key="adversarial:unicode:merchant",
            actor="operator:test",
            reason="human accepted Unicode merchant evidence",
            evidence=Evidence(
                statement_line_id=first[2],
                transaction_id=first[0],
            ),
        )
        category_claim = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=first_description,
            category_id=expense,
            scope=scope,
            operation_key="adversarial:unicode:category",
            actor="operator:test",
            reason="human accepted Unicode category evidence",
            evidence=Evidence(
                statement_line_id=first[2],
                transaction_id=first[0],
                transaction_split_id=first[1],
            ),
        )
        event_ids = tuple(
            int(row["id"])
            for row in conn.execute(
                """
                SELECT id
                FROM merchant_resolution_events
                WHERE claim_id IN (?, ?)
                ORDER BY id
                """,
                (merchant_claim, category_claim),
            ).fetchall()
        )

    statement = build_period_statement(empty_db, month=MONTH, home_currency="CAD")
    expected_transaction_order = (first[0], tied[0], second_account_row[0])

    assert tuple(account.account_id for account in statement.accounts) == (
        first_account,
        second_account,
    )
    assert tuple(row.transaction_id for row in statement.rows) == (
        expected_transaction_order
    )
    first_row = statement.rows[0]
    assert first_row.description == first_description
    assert first_row.category_name == "Repas, café\nÉquipe"
    assert first_row.canonical_merchant == "Café, Inc.\nMontréal"
    assert first_row.evidence.row_ids == (
        f"txn:{first[0]}:split:{first[1]}",
    )
    assert first_row.evidence.transaction_ids == (first[0],)
    assert first_row.evidence.transaction_split_ids == (first[1],)
    assert first_row.evidence.statement_line_ids == (first[2],)
    assert first_row.evidence.source_document_ids == (first_document,)
    assert first_row.evidence.canonical_merchant_claim_ids == (merchant_claim,)
    assert first_row.evidence.category_claim_ids == (category_claim,)
    assert first_row.evidence.resolution_event_ids == event_ids
    assert first_row.evidence.category_ids == (expense,)

    csv_rows = list(
        csv.DictReader(
            io.StringIO(
                render_period_statement_csv(statement).decode("utf-8")
            )
        )
    )
    evidence_rows = [
        row for row in csv_rows if row["record_type"] == "evidence_row"
    ]
    assert tuple(int(row["transaction_ids"]) for row in evidence_rows) == (
        expected_transaction_order
    )
    assert evidence_rows[0]["label"] == first_description
    assert evidence_rows[0]["statement_line_ids"] == str(first[2])
    assert evidence_rows[0]["source_document_ids"] == str(first_document)
    assert evidence_rows[0]["merchant_claim_ids"] == str(merchant_claim)
    assert evidence_rows[0]["category_claim_ids"] == str(category_claim)
    assert evidence_rows[0]["resolution_event_ids"] == "|".join(
        str(value) for value in event_ids
    )


def test_liquid_position_includes_supported_assets_and_signed_credit_only(empty_db):
    balances = (
        ("Cash", "cash", 10_000),
        ("Chequing", "chequing", 20_000),
        ("Savings", "savings", 30_000),
        ("Credit liability", "credit", -4_000),
        ("Brokerage", "investment", 50_000),
    )
    with engine.write_tx(empty_db) as conn:
        opening = _category(conn, "Opening", "transfer")
        account_ids: dict[str, int] = {}
        for index, (name, kind, cents) in enumerate(balances):
            account_id = _account(conn, name, kind)
            account_ids[name] = account_id
            _row(
                conn,
                account_id=account_id,
                category_id=opening,
                posted_on="2026-05-31",
                cents=cents,
                flow_kind="opening",
                key=f"liquid-{index}",
            )
            repo_assertions.record_assertion(
                conn,
                account_id=account_id,
                asof_date="2026-06-30",
                asserted_cents=cents,
                statement_period=MONTH,
            )

    statement = build_period_statement(empty_db, month=MONTH, home_currency="CAD")
    by_name = {account.name: account for account in statement.accounts}

    assert statement.household.opening_liquid_position.cents == 56_000
    assert statement.household.closing_liquid_position.cents == 56_000
    assert by_name["Credit liability"].opening_balance.cents == -4_000
    assert by_name["Credit liability"].ledger_closing_balance.cents == -4_000
    assert by_name["Credit liability"].liquid_position_included is True
    assert all(
        by_name[name].liquid_position_included
        for name in ("Cash", "Chequing", "Savings")
    )
    assert by_name["Brokerage"].liquid_position_included is False
    assert by_name["Brokerage"].liquid_position_exclusion_reason == (
        "unsupported_account_kind_v1"
    )
    assert [
        (item.account_id, item.account_name, item.reason)
        for item in statement.liquid_position_exclusions
    ] == [
        (
            account_ids["Brokerage"],
            "Brokerage",
            "unsupported_account_kind_v1",
        )
    ]


def test_multipage_pdf_repeats_truth_header_and_retains_every_evidence_line(empty_db):
    with engine.write_tx(empty_db) as conn:
        expense = _category(conn, "Expense", "expense")
        account = _account(conn, "Busy card", "credit")
        for index in range(100):
            _row(
                conn,
                account_id=account,
                category_id=expense,
                posted_on=f"2026-06-{index % 28 + 1:02d}",
                cents=-(index + 1),
                flow_kind="purchase",
                key=f"pdf-{index:03d}",
            )

    statement = build_period_statement(empty_db, month=MONTH, home_currency="CAD")
    pdf_bytes = render_period_statement_pdf(statement)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        page_texts = [page.get_text() for page in document]

    problems: list[str] = []
    if len(page_texts) < 2:
        problems.append("report did not paginate")
    for page_number, text in enumerate(page_texts, start=1):
        if f"Finn Nancy household statement - {MONTH}" not in text:
            problems.append(f"page {page_number} omitted report header")
        if f"Digest: {statement.report_digest}" not in text:
            problems.append(f"page {page_number} omitted report digest")
        if "current=false" not in text:
            problems.append(f"page {page_number} omitted snapshot currentness")
    combined = "\n".join(page_texts)
    parsed_row_ids = re.findall(
        r"(?m)^(txn:\d+:split:\d+)\s",
        combined,
    )
    omitted = sorted(
        set(row.row_id for row in statement.rows) ^ set(parsed_row_ids)
    )
    duplicated = sorted(
        row_id
        for row_id in set(parsed_row_ids)
        if parsed_row_ids.count(row_id) != 1
    )
    if omitted:
        problems.append(
            f"{len(omitted)} evidence rows omitted or unexpected: {omitted[:5]}"
        )
    if duplicated:
        problems.append(
            f"{len(duplicated)} evidence rows duplicated: {duplicated[:5]}"
        )
    assert not problems, "; ".join(problems)


def test_frozen_close_bytes_ignore_later_category_and_merchant_renames(empty_db):
    descriptor = "SQ *OLD CAFÉ 042"
    with engine.write_tx(empty_db) as conn:
        category = _category(conn, "Old category", "expense")
        account = _account(conn, "Frozen card", "credit")
        document = _document(conn, "frozen-rename")
        transaction, split, statement_line = _row(
            conn,
            account_id=account,
            category_id=category,
            posted_on="2026-06-12",
            cents=-1_234,
            flow_kind="purchase",
            key="frozen-rename",
            document_id=document,
            description=descriptor,
        )
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=account,
            processor_family="square",
            region="CA-ON",
        )
        merchant_claim = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor=descriptor,
            canonical_name="Old Café",
            scope=scope,
            operation_key="adversarial:frozen:merchant",
            actor="operator:test",
            reason="human accepted original merchant before close",
            evidence=Evidence(
                statement_line_id=statement_line,
                transaction_id=transaction,
            ),
        )
        repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=descriptor,
            category_id=category,
            scope=scope,
            operation_key="adversarial:frozen:category",
            actor="operator:test",
            reason="human accepted original category before close",
            evidence=Evidence(
                statement_line_id=statement_line,
                transaction_id=transaction,
                transaction_split_id=split,
            ),
        )
        live = build_period_statement(
            conn,
            month=MONTH,
            home_currency="CAD",
        )
        closed_row = repo_period_policy.close_period(
            conn,
            MONTH,
            snapshot=period_statement_snapshot_payload(
                live,
                close_state="clean_closed",
            ),
            exceptions=(),
            actor="operator:test",
            reason="freeze report before mutable reference-data rename",
            operation_key="adversarial:frozen:close",
        )
        snapshot_id = int(closed_row["id"])
        frozen_snapshot_json = str(closed_row["snapshot_json"])

    frozen_before = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    canonical_before = frozen_before.model_dump_json().encode("utf-8")
    csv_before = render_period_statement_csv(frozen_before)
    pdf_before = render_period_statement_pdf(frozen_before)

    with engine.write_tx(empty_db) as conn:
        conn.execute(
            "UPDATE categories SET name='Renamed category' WHERE id=?",
            (category,),
        )

    still_frozen = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    assert still_frozen.model_dump_json().encode("utf-8") == canonical_before
    assert render_period_statement_csv(still_frozen) == csv_before
    assert render_period_statement_pdf(still_frozen) == pdf_before

    with engine.write_tx(empty_db) as conn:
        repo_period_policy.reopen_period(
            conn,
            MONTH,
            actor="operator:test",
            reason="reopen before correcting transaction-bound merchant evidence",
            operation_key="adversarial:frozen:reopen",
            affected_ids={"transaction_id": transaction},
        )
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=account,
            processor_family="square",
            region="CA-ON",
        )
        repo_merchant_knowledge.correct_merchant(
            conn,
            prior_claim_id=merchant_claim,
            descriptor=descriptor,
            canonical_name="Renamed Café",
            scope=scope,
            operation_key="adversarial:frozen:merchant:rename",
            actor="operator:test",
            reason="human corrected merchant after the period was frozen",
            evidence=Evidence(
                statement_line_id=statement_line,
                transaction_id=transaction,
            ),
        )
        current_resolution = resolve_descriptor(
            conn,
            descriptor=descriptor,
            scope=scope,
        )
        history = repo_period_policy.list_snapshot_history(conn, MONTH)

    assert current_resolution.merchant.target_name == "Renamed Café"
    assert len(history) == 1
    assert int(history[0]["snapshot_id"]) == snapshot_id
    assert str(history[0]["snapshot_json"]) == frozen_snapshot_json

    reopened = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    reopened_row = next(
        row for row in reopened.rows if row.transaction_id == transaction
    )
    assert reopened_row.category_name == "Renamed category"
    assert reopened_row.canonical_merchant == "Renamed Café"


@pytest.mark.parametrize("statement_currency", ["", "CA", "EUR"])
def test_invalid_statement_currency_rejects_entire_report(
    empty_db,
    statement_currency,
):
    with engine.write_tx(empty_db) as conn:
        expense = _category(conn, "Expense", "expense")
        account = _account(conn, "CAD cash", "cash")
        document = _document(conn, f"statement-{statement_currency or 'blank'}")
        _row(
            conn,
            account_id=account,
            category_id=expense,
            posted_on="2026-06-01",
            cents=-100,
            flow_kind="purchase",
            key=f"statement-currency-{statement_currency or 'blank'}",
            document_id=document,
            statement_currency=statement_currency,
        )

    with pytest.raises(UnsupportedReportCurrency):
        build_period_statement(empty_db, month=MONTH, home_currency="CAD")


def test_path_report_reads_one_wal_snapshot_when_writer_commits_mid_load(
    empty_db,
    monkeypatch,
):
    original_accounts = repo_period_statements.accounts
    writer_committed = False

    def accounts_then_commit(conn):
        nonlocal writer_committed
        rows = original_accounts(conn)
        with engine.write_tx(empty_db) as writer:
            category = _category(writer, "Concurrent expense", "expense")
            account = _account(writer, "Concurrent account", "cash")
            _row(
                writer,
                account_id=account,
                category_id=category,
                posted_on="2026-06-10",
                cents=-999,
                flow_kind="purchase",
                key="concurrent-after-account-read",
            )
        writer_committed = True
        return rows

    monkeypatch.setattr(
        repo_period_statements,
        "accounts",
        accounts_then_commit,
    )
    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )

    assert writer_committed is True
    assert statement.accounts == ()
    assert statement.rows == ()
    with engine.read_conn(empty_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_cross_month_transfer_pair_is_neutral_with_period_account_legs(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        transfer = _category(conn, "Transfer", "transfer")
        chequing = _account(conn, "Chequing", "chequing")
        savings = _account(conn, "Savings", "savings")
        may_out = _row(
            conn,
            account_id=chequing,
            category_id=transfer,
            posted_on="2026-05-31",
            cents=-500,
            flow_kind="internal_transfer",
            key="cross-month-out",
        )[0]
        june_in = _row(
            conn,
            account_id=savings,
            category_id=transfer,
            posted_on="2026-06-01",
            cents=500,
            flow_kind="internal_transfer",
            key="cross-month-in",
        )[0]
        relationship_id = create_relationship(
            conn,
            relationship_kind="transfer_pair",
            source_transaction_id=may_out,
            target_transaction_id=june_in,
            actor="operator:test",
            reason="bank posted the paired legs on adjacent months",
        )

    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    by_name = {account.name: account for account in statement.accounts}

    assert statement.household.transfer_neutrality_control.cents == 0
    assert (
        statement.household.transfer_neutrality_control.evidence.transaction_ids
        == (may_out, june_in)
    )
    assert (
        statement.household.transfer_neutrality_control.evidence.relationship_ids
        == (relationship_id,)
    )
    assert by_name["Chequing"].transfers_out.cents == 0
    assert by_name["Savings"].transfers_in.cents == 500


def test_forward_cross_month_transfer_pair_is_neutral_without_future_account_leg(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        transfer = _category(conn, "Transfer", "transfer")
        chequing = _account(conn, "Chequing", "chequing")
        savings = _account(conn, "Savings", "savings")
        june_out = _row(
            conn,
            account_id=chequing,
            category_id=transfer,
            posted_on="2026-06-30",
            cents=-500,
            flow_kind="internal_transfer",
            key="forward-cross-month-out",
        )[0]
        july_in = _row(
            conn,
            account_id=savings,
            category_id=transfer,
            posted_on="2026-07-01",
            cents=500,
            flow_kind="internal_transfer",
            key="forward-cross-month-in",
        )[0]
        relationship_id = create_relationship(
            conn,
            relationship_kind="transfer_pair",
            source_transaction_id=june_out,
            target_transaction_id=july_in,
            actor="operator:test",
            reason="bank posted the paired legs on adjacent months",
        )

    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    by_name = {account.name: account for account in statement.accounts}

    assert statement.household.transfer_neutrality_control.cents == 0
    assert (
        statement.household.transfer_neutrality_control.evidence.transaction_ids
        == (june_out, july_in)
    )
    assert (
        statement.household.transfer_neutrality_control.evidence.relationship_ids
        == (relationship_id,)
    )
    assert by_name["Chequing"].transfers_out.cents == 500
    assert "Savings" not in by_name


@pytest.mark.parametrize("relationship_state", ["missing", "revoked"])
def test_forward_cross_month_unmatched_or_revoked_transfer_stays_unbalanced(
    empty_db,
    relationship_state,
):
    with engine.write_tx(empty_db) as conn:
        transfer = _category(conn, "Transfer", "transfer")
        chequing = _account(conn, "Chequing", "chequing")
        savings = _account(conn, "Savings", "savings")
        june_out = _row(
            conn,
            account_id=chequing,
            category_id=transfer,
            posted_on="2026-06-30",
            cents=-500,
            flow_kind="internal_transfer",
            key=f"{relationship_state}-cross-month-out",
        )[0]
        july_in = _row(
            conn,
            account_id=savings,
            category_id=transfer,
            posted_on="2026-07-01",
            cents=500,
            flow_kind="internal_transfer",
            key=f"{relationship_state}-cross-month-in",
        )[0]
        if relationship_state == "revoked":
            relationship_id = create_relationship(
                conn,
                relationship_kind="transfer_pair",
                source_transaction_id=june_out,
                target_transaction_id=july_in,
                actor="operator:test",
                reason="temporary pair",
            )
            revoke_relationship(
                conn,
                relationship_id,
                actor="operator:test",
                reason="pair was incorrect",
            )

    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    by_name = {account.name: account for account in statement.accounts}

    assert statement.household.transfer_neutrality_control.cents == -500
    assert (
        statement.household.transfer_neutrality_control.evidence.transaction_ids
        == (june_out,)
    )
    assert (
        statement.household.transfer_neutrality_control.evidence.relationship_ids
        == ()
    )
    assert by_name["Chequing"].transfers_out.cents == 500
    assert "Savings" not in by_name


def test_historical_assertion_only_account_does_not_contribute_forever(
    empty_db,
):
    with engine.write_tx(empty_db) as conn:
        euro = _account(conn, "Old euro account", "cash", currency="EUR")
        repo_assertions.record_assertion(
            conn,
            account_id=euro,
            asof_date="2025-12-31",
            asserted_cents=12_345,
            statement_period="2025-12",
        )

    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )

    assert statement.accounts == ()
    assert statement.rows == ()
    assert statement.household.closing_liquid_position.cents == 0


def test_split_evidence_excludes_sibling_category_claim_provenance(empty_db):
    descriptor = "SPLIT PURCHASE"
    with engine.write_tx(empty_db) as conn:
        first_category = _category(conn, "Meals", "expense")
        second_category = _category(conn, "Supplies", "expense")
        account = _account(conn, "Split card", "credit")
        document = _document(conn, "split-claim-evidence")
        review_id = int(
            conn.execute(
                """
                INSERT INTO statement_reviews(
                  source_document_id, account_id, period_start_on,
                  period_end_on, period_month, currency
                )
                VALUES (?, ?, '2026-06-01', '2026-06-30', ?, 'CAD')
                """,
                (document, account, MONTH),
            ).lastrowid
        )
        source_sha256 = str(
            conn.execute(
                "SELECT sha256 FROM source_documents WHERE id=?",
                (document,),
            ).fetchone()["sha256"]
        )
        line_anchor = int(
            conn.execute(
                """
                INSERT INTO statement_source_anchors(
                  statement_review_id, locator_kind, locator_json,
                  source_sha256, created_by
                )
                VALUES (?, 'raw_row', '{"row":1}', ?, 'operator:test')
                """,
                (review_id, source_sha256),
            ).lastrowid
        )
        transaction = int(
            conn.execute(
                """
                INSERT INTO transactions(
                  account_id, posted_on, description, counterparty,
                  amount_cents, source_document_id, source, statement_period,
                  external_id, recon_status, cleared_on, flow_kind
                )
                VALUES (?, '2026-06-12', ?, ?, -3000, ?, 'statement', ?,
                        'adversarial:split-claims', 'cleared', '2026-06-12',
                        'purchase')
                """,
                (account, descriptor, descriptor, document, MONTH),
            ).lastrowid
        )
        first_split = int(
            conn.execute(
                """
                INSERT INTO transaction_splits(
                  transaction_id, category_id, amount_cents
                )
                VALUES (?, ?, -1000)
                """,
                (transaction, first_category),
            ).lastrowid
        )
        second_split = int(
            conn.execute(
                """
                INSERT INTO transaction_splits(
                  transaction_id, category_id, amount_cents
                )
                VALUES (?, ?, -2000)
                """,
                (transaction, second_category),
            ).lastrowid
        )
        statement_line = int(
            conn.execute(
                """
                INSERT INTO statement_lines(
                  source_document_id, account_id, posted_on, raw_description,
                  norm_merchant, amount_cents, currency, statement_period,
                  row_hash, match_status, matched_transaction_id, match_method,
                  match_score, match_rationale, flow_kind, source_anchor_id
                )
                VALUES (?, ?, '2026-06-12', ?, ?, -3000, 'CAD', ?,
                        'row:split-claims', 'matched', ?, 'manual', 100,
                        'split transaction fixture', 'purchase', ?)
                """,
                (
                    document,
                    account,
                    descriptor,
                    descriptor.casefold(),
                    MONTH,
                    transaction,
                    line_anchor,
                ),
            ).lastrowid
        )
        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=account,
            processor_family="manual",
            region="CA-ON",
        )
        first_claim = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor=descriptor,
            category_id=first_category,
            scope=scope,
            operation_key="adversarial:split-claims:first",
            actor="operator:test",
            reason="human approved only the first split",
            evidence=Evidence(
                statement_line_id=statement_line,
                transaction_id=transaction,
                transaction_split_id=first_split,
                source_anchor_id=line_anchor,
            ),
        )

    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    by_split = {
        row.transaction_split_id: row for row in statement.rows
    }
    accepted = by_split[first_split]
    unresolved = by_split[second_split]

    assert accepted.evidence.category_claim_ids == (first_claim,)
    assert accepted.evidence.merchant_pattern_ids
    assert accepted.evidence.resolution_event_ids
    assert accepted.evidence.source_anchor_ids == (line_anchor,)
    assert unresolved.resolution_disposition == "unresolved"
    assert unresolved.evidence.category_claim_ids == ()
    assert unresolved.evidence.merchant_pattern_ids == ()
    assert unresolved.evidence.resolution_event_ids == ()
    assert unresolved.evidence.source_anchor_ids == (line_anchor,)

    synthetic_unrelated = period_statement_builder._row_evidence(
        {
            "transaction_id": transaction,
            "transaction_split_id": second_split,
            "transaction_source_document_id": None,
            "category_id": second_category,
            "account_id": account,
        },
        statements={},
        claims={
            transaction: (
                {
                    "claim_kind": "expense_category",
                    "transaction_split_id": first_split,
                    "source_anchor_id": 999,
                    "pattern_id": 888,
                    "claim_id": 777,
                    "acceptance_event_id": 666,
                },
            )
        },
        relationship_ids={},
        assertion_ids={},
        close_evidence=EvidenceSet(),
    )
    assert synthetic_unrelated.source_anchor_ids == ()
    assert synthetic_unrelated.merchant_pattern_ids == ()
    assert synthetic_unrelated.resolution_event_ids == ()


def test_csv_neutralizes_formula_leading_untrusted_text_without_model_mutation(
    empty_db,
):
    descriptions = (
        "=HYPERLINK(\"https://invalid\")",
        "+SUM(1,1)",
        "-CMD|calc!A0",
        "@EXTERNAL",
        "\t=CMD|calc!A0",
        "\r=HYPERLINK(\"https://invalid\")",
        "  =WEBSERVICE(\"https://invalid\")",
    )
    with engine.write_tx(empty_db) as conn:
        category = _category(conn, "Ordinary category", "expense")
        account = _account(conn, "Ordinary account", "credit")
        for index, description in enumerate(descriptions):
            _row(
                conn,
                account_id=account,
                category_id=category,
                posted_on=f"2026-06-{index + 1:02d}",
                cents=-(index + 1),
                flow_kind="purchase",
                key=f"csv-injection-{index}",
                description=description,
            )

    original = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    malicious_account = original.accounts[0].model_copy(
        update={"name": "=HYPERLINK(\"https://invalid/account\")"}
    )
    malicious_rows = tuple(
        row.model_copy(
            update={
                "category_name": "@EXTERNAL",
                "canonical_merchant": (
                    "+SUM(1,1)" if index == 0 else row.canonical_merchant
                ),
            }
        )
        for index, row in enumerate(original.rows)
    )
    malicious_bucket = original.expense_resolution.buckets[0].model_copy(
        update={
            "category_name": "\r@EXTERNAL",
            "canonical_merchant": "-CMD|calc!A0",
        }
    )
    statement = original.model_copy(
        update={
            "accounts": (malicious_account,),
            "rows": malicious_rows,
            "expense_resolution": original.expense_resolution.model_copy(
                update={"buckets": (malicious_bucket,)}
            ),
        }
    )
    canonical_before = statement.model_dump_json()

    rows = list(
        csv.DictReader(
            io.StringIO(
                render_period_statement_csv(statement).decode("utf-8"),
                newline="",
            )
        )
    )

    assert statement.model_dump_json() == canonical_before
    evidence = [
        row for row in rows if row["record_type"] == "evidence_row"
    ]
    assert all(row["label"].startswith("'") for row in evidence)
    assert all(row["account_name"].startswith("'=") for row in evidence)
    assert all(row["category_name"].startswith("'@") for row in evidence)
    assert evidence[0]["canonical_merchant"].startswith("'+")
    bucket = next(
        row for row in rows if row["record_type"] == "expense_bucket"
    )
    assert bucket["category_name"].startswith("'\r@")
    assert bucket["canonical_merchant"].startswith("'-")


def test_pdf_embeds_unicode_font_and_extracts_multilingual_labels(empty_db):
    with engine.write_tx(empty_db) as conn:
        category = _category(conn, "Ordinary category", "expense")
        account = _account(conn, "Ordinary account", "credit")
        _row(
            conn,
            account_id=account,
            category_id=category,
            posted_on="2026-06-12",
            cents=-1_234,
            flow_kind="purchase",
            key="unicode-pdf",
            description="ordinary description",
        )

    original = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    statement = original.model_copy(
        update={
            "accounts": (
                original.accounts[0].model_copy(
                    update={"name": "預金口座 Λογαριασμός Счёт"}
                ),
            ),
            "rows": (
                original.rows[0].model_copy(
                    update={
                        "description": "東京市場 Ελληνικά Кириллица",
                        "category_name": "食費 Δαπάνη Расходы",
                        "canonical_merchant": "東京市場 Ελληνικά Кириллица",
                    }
                ),
            ),
            "expense_resolution": original.expense_resolution.model_copy(
                update={
                    "buckets": (
                        original.expense_resolution.buckets[0].model_copy(
                            update={
                                "category_name": "食費 Δαπάνη Расходы",
                                "canonical_merchant": (
                                    "東京市場 Ελληνικά Кириллица"
                                ),
                            }
                        ),
                    )
                }
            ),
        }
    )

    pdf = render_period_statement_pdf(statement)
    with fitz.open(stream=pdf, filetype="pdf") as document:
        extracted = "\n".join(page.get_text() for page in document)
        fonts = [
            font
            for page_number in range(document.page_count)
            for font in document.get_page_fonts(page_number)
        ]

    assert "東京市場" in extracted
    assert "食費" in extracted
    assert "預金口座" in extracted
    assert "Ελληνικά" in extracted
    assert "Δαπάνη" in extracted
    assert "Λογαριασμός" in extracted
    assert "Кириллица" in extracted
    assert "Расходы" in extracted
    assert "Счёт" in extracted
    assert "????" not in extracted
    assert any(
        font[1] == "ttf"
        and font[2] == "Type0"
        and font[4] == "FNUnicode"
        and font[5] == "Identity-H"
        for font in fonts
    )


@pytest.mark.parametrize("unsupported_text", ["مرحبا", "नमस्ते", "😀"])
def test_pdf_rejects_unsupported_glyphs_without_changing_json_or_csv(
    empty_db,
    unsupported_text,
):
    with engine.write_tx(empty_db) as conn:
        category = _category(conn, "Ordinary category", "expense")
        account = _account(conn, "Ordinary account", "credit")
        _row(
            conn,
            account_id=account,
            category_id=category,
            posted_on="2026-06-12",
            cents=-1_234,
            flow_kind="purchase",
            key=f"unsupported-{ord(unsupported_text[0])}",
        )

    original = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    statement = original.model_copy(
        update={
            "rows": (
                original.rows[0].model_copy(
                    update={"description": unsupported_text}
                ),
            ),
        }
    )
    json_before = statement.model_dump_json()
    csv_before = render_period_statement_csv(statement)

    with pytest.raises(UnsupportedPdfGlyphError, match="U\\+"):
        render_period_statement_pdf(statement)

    assert statement.model_dump_json() == json_before
    assert render_period_statement_csv(statement) == csv_before
    assert unsupported_text.encode("utf-8") in csv_before
