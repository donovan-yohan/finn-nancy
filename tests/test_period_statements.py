from __future__ import annotations

import csv
import io

import fitz
import pytest
from fastapi.testclient import TestClient

from app.db import (
    engine,
    repo_assertions,
    repo_merchant_knowledge,
    repo_period_policy,
)
from app.db.repo_merchant_knowledge import Evidence
from app.reporting.exports import render_period_statement_csv, render_period_statement_pdf
from app.reporting.period_statements import (
    FrozenPeriodStatementIntegrityError,
    FrozenPeriodStatementUnavailable,
    UnsupportedReportCurrency,
    build_period_statement,
    period_statement_snapshot_payload,
)

MONTH = "2026-06"
AS_OF = "2026-06-30"


def _category(conn, name: str, kind: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO categories(name, kind, brand_owner)
            VALUES (?, ?, 'shared')
            """,
            (name, kind),
        ).lastrowid
    )


def _account(conn, name: str, kind: str, *, currency: str = "CAD") -> int:
    return int(
        conn.execute(
            """
            INSERT INTO accounts(name, institution, kind, currency)
            VALUES (?, 'Fixture Bank', ?, ?)
            """,
            (name, kind, currency),
        ).lastrowid
    )


def _document(conn, name: str) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO source_documents(
              kind, original_name, storage_ref, sha256, mime_type, status
            )
            VALUES ('statement', ?, ?, ?, 'application/pdf', 'matched')
            """,
            (name, f"fixture/{name}", name.encode().hex().ljust(64, "0")[:64]),
        ).lastrowid
    )


def _opening(conn, account_id: int, category_id: int, cents: int, key: str) -> None:
    transaction_id = int(
        conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, amount_cents, source,
              external_id, recon_status, cleared_on, flow_kind
            )
            VALUES (?, '2026-05-31', 'Opening balance', ?, 'opening', ?,
                    'cleared', '2026-05-31', 'opening')
            """,
            (account_id, cents, f"fixture:opening:{key}"),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
        VALUES (?, ?, ?)
        """,
        (transaction_id, category_id, cents),
    )


def _period_row(
    conn,
    *,
    account_id: int,
    document_id: int,
    category_id: int,
    posted_on: str,
    descriptor: str,
    cents: int,
    flow_kind: str,
    key: str,
    source_anchor_id: int | None = None,
) -> tuple[int, int, int]:
    transaction_id = int(
        conn.execute(
            """
            INSERT INTO transactions(
              account_id, posted_on, description, counterparty, amount_cents,
              source_document_id, source, statement_period, external_id,
              recon_status, cleared_on, flow_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, 'statement', ?, ?, 'cleared', ?, ?)
            """,
            (
                account_id,
                posted_on,
                descriptor,
                descriptor,
                cents,
                document_id,
                MONTH,
                f"fixture:{key}",
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
    statement_line_id = int(
        conn.execute(
            """
            INSERT INTO statement_lines(
              source_document_id, account_id, posted_on, raw_description,
              norm_merchant, amount_cents, currency, statement_period,
              row_hash, match_status, matched_transaction_id, match_method,
              match_score, match_rationale, flow_kind, source_anchor_id
            )
            VALUES (?, ?, ?, ?, ?, ?, 'CAD', ?, ?, 'matched', ?, 'manual',
                    100, 'golden fixture exact row', ?, ?)
            """,
            (
                document_id,
                account_id,
                posted_on,
                descriptor,
                descriptor.casefold(),
                cents,
                MONTH,
                f"row:{key}",
                transaction_id,
                flow_kind,
                source_anchor_id,
            ),
        ).lastrowid
    )
    return transaction_id, split_id, statement_line_id


def _seed_golden_month(db: str) -> dict[str, int]:
    with engine.write_tx(db) as conn:
        opening = _category(conn, "Opening", "transfer")
        salary = _category(conn, "Salary", "income")
        groceries = _category(conn, "Groceries", "expense")
        uncategorized = _category(conn, "Uncategorized", "expense")
        transfer = _category(conn, "Transfer", "transfer")

        chequing = _account(conn, "Chequing", "chequing")
        savings = _account(conn, "Savings", "savings")
        credit = _account(conn, "Credit card", "credit")

        chequing_doc = _document(conn, "chequing-june.pdf")
        savings_doc = _document(conn, "savings-june.pdf")
        credit_doc = _document(conn, "credit-june.pdf")
        review_id = int(
            conn.execute(
                """
                INSERT INTO statement_reviews(
                  source_document_id, account_id, period_start_on,
                  period_end_on, period_month, currency
                )
                VALUES (?, ?, '2026-06-01', '2026-06-30', ?, 'CAD')
                """,
                (credit_doc, credit, MONTH),
            ).lastrowid
        )
        source_sha256 = str(
            conn.execute(
                "SELECT sha256 FROM source_documents WHERE id=?",
                (credit_doc,),
            ).fetchone()["sha256"]
        )
        resolved_anchor = int(
            conn.execute(
                """
                INSERT INTO statement_source_anchors(
                  statement_review_id, locator_kind, locator_json,
                  source_sha256, created_by
                )
                VALUES (?, 'raw_row', '{"row":1}', ?, 'fixture')
                """,
                (review_id, source_sha256),
            ).lastrowid
        )

        _opening(conn, chequing, opening, 1_000_000, "chequing")
        _opening(conn, savings, opening, 240_000, "savings")

        income, _, _ = _period_row(
            conn,
            account_id=chequing,
            document_id=chequing_doc,
            category_id=salary,
            posted_on="2026-06-01",
            descriptor="Payroll",
            cents=501_840,
            flow_kind="income",
            key="income",
        )
        resolved_purchase, resolved_split, resolved_line = _period_row(
            conn,
            account_id=credit,
            document_id=credit_doc,
            category_id=groceries,
            posted_on="2026-06-03",
            descriptor="SQ *ACME MARKET 042",
            cents=-226_735,
            flow_kind="purchase",
            key="resolved-purchase",
            source_anchor_id=resolved_anchor,
        )
        unresolved_purchase, _, _ = _period_row(
            conn,
            account_id=chequing,
            document_id=chequing_doc,
            category_id=uncategorized,
            posted_on="2026-06-05",
            descriptor="CARD PURCHASE 0092",
            cents=-6_420,
            flow_kind="purchase",
            key="unresolved-purchase",
        )
        refund, _, _ = _period_row(
            conn,
            account_id=credit,
            document_id=credit_doc,
            category_id=groceries,
            posted_on="2026-06-08",
            descriptor="ACME MARKET REFUND",
            cents=2_345,
            flow_kind="refund",
            key="refund",
        )
        transfer_out, _, _ = _period_row(
            conn,
            account_id=chequing,
            document_id=chequing_doc,
            category_id=transfer,
            posted_on="2026-06-10",
            descriptor="Transfer to savings",
            cents=-50_000,
            flow_kind="internal_transfer",
            key="transfer-out",
        )
        transfer_in, _, _ = _period_row(
            conn,
            account_id=savings,
            document_id=savings_doc,
            category_id=transfer,
            posted_on="2026-06-10",
            descriptor="Transfer from chequing",
            cents=50_000,
            flow_kind="internal_transfer",
            key="transfer-in",
        )
        payment_out, _, _ = _period_row(
            conn,
            account_id=chequing,
            document_id=chequing_doc,
            category_id=transfer,
            posted_on="2026-06-20",
            descriptor="Credit card payment",
            cents=-100_000,
            flow_kind="card_payment",
            key="payment-out",
        )
        payment_in, _, _ = _period_row(
            conn,
            account_id=credit,
            document_id=credit_doc,
            category_id=transfer,
            posted_on="2026-06-20",
            descriptor="Payment received",
            cents=100_000,
            flow_kind="card_payment",
            key="payment-in",
        )
        conn.execute(
            """
            INSERT INTO transaction_relationships(
              relationship_kind, source_transaction_id, target_transaction_id,
              created_by, reason
            )
            VALUES ('refund_of', ?, ?, 'fixture', 'exact partial refund')
            """,
            (refund, resolved_purchase),
        )
        conn.execute(
            """
            INSERT INTO transaction_relationships(
              relationship_kind, source_transaction_id, target_transaction_id,
              created_by, reason
            )
            VALUES ('transfer_pair', ?, ?, 'fixture', 'internal transfer pair')
            """,
            (transfer_out, transfer_in),
        )
        conn.execute(
            """
            INSERT INTO transaction_relationships(
              relationship_kind, source_transaction_id, target_transaction_id,
              created_by, reason
            )
            VALUES ('transfer_pair', ?, ?, 'fixture', 'card payment pair')
            """,
            (payment_out, payment_in),
        )

        scope = repo_merchant_knowledge.scope_for(
            conn,
            account_id=credit,
            processor_family="square",
            region="CA-ON",
        )
        merchant_claim = repo_merchant_knowledge.confirm_merchant(
            conn,
            descriptor="SQ *ACME MARKET 042",
            canonical_name="Acme Market",
            scope=scope,
            operation_key="fixture:merchant:acme",
            actor="operator:fixture",
            reason="fixture human-approved canonical merchant",
            evidence=Evidence(
                statement_line_id=resolved_line,
                transaction_id=resolved_purchase,
                source_anchor_id=resolved_anchor,
            ),
        )
        category_claim = repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="SQ *ACME MARKET 042",
            category_id=groceries,
            scope=scope,
            operation_key="fixture:category:acme",
            actor="operator:fixture",
            reason="fixture human-approved expense category",
            evidence=Evidence(
                statement_line_id=resolved_line,
                transaction_id=resolved_purchase,
                transaction_split_id=resolved_split,
                source_anchor_id=resolved_anchor,
            ),
        )

        repo_assertions.record_assertion(
            conn,
            account_id=chequing,
            asof_date=AS_OF,
            asserted_cents=1_345_420,
            source_document_id=chequing_doc,
            statement_period=MONTH,
        )
        repo_assertions.record_assertion(
            conn,
            account_id=savings,
            asof_date=AS_OF,
            asserted_cents=290_000,
            source_document_id=savings_doc,
            statement_period=MONTH,
        )
        repo_assertions.record_assertion(
            conn,
            account_id=credit,
            asof_date=AS_OF,
            asserted_cents=-124_390,
            source_document_id=credit_doc,
            statement_period=MONTH,
        )

    return {
        "income": income,
        "resolved_purchase": resolved_purchase,
        "unresolved_purchase": unresolved_purchase,
        "merchant_claim": merchant_claim,
        "category_claim": category_claim,
        "resolved_anchor": resolved_anchor,
    }


def test_period_statement_golden_totals_and_export_parity(empty_db):
    seeded = _seed_golden_month(empty_db)
    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )

    assert statement.household.income.cents == 501_840
    assert statement.household.gross_money_out.cents == 233_155
    assert statement.household.refunds.cents == 2_345
    assert statement.household.net_money_out.cents == 230_810
    assert statement.household.external_cash_movement.cents == 271_030
    assert statement.household.opening_liquid_position.cents == 1_240_000
    assert statement.household.closing_liquid_position.cents == 1_511_030
    assert statement.expense_resolution.resolved.cents == 224_390
    assert statement.expense_resolution.unresolved.cents == 6_420
    assert statement.expense_resolution.confirmed.cents == 0
    assert statement.expense_resolution.human_approved.cents == 224_390
    assert statement.automation_authority["expense_category"] is False
    assert statement.household.transfer_neutrality_control.cents == 0
    assert [account.name for account in statement.accounts] == [
        "Chequing",
        "Savings",
        "Credit card",
    ]
    assert seeded["resolved_purchase"] in statement.household.gross_money_out.evidence.transaction_ids
    assert seeded["unresolved_purchase"] in statement.expense_resolution.unresolved.evidence.transaction_ids
    unresolved_row = next(
        row
        for row in statement.rows
        if row.transaction_id == seeded["unresolved_purchase"]
    )
    resolved_row = next(
        row
        for row in statement.rows
        if row.transaction_id == seeded["resolved_purchase"]
    )
    assert unresolved_row.category_id > 0
    assert unresolved_row.resolution_disposition == "unresolved"
    assert resolved_row.evidence.source_anchor_ids == (
        seeded["resolved_anchor"],
    )
    assert resolved_row.evidence.merchant_entity_ids
    assert resolved_row.evidence.merchant_pattern_ids
    assert resolved_row.evidence.canonical_merchant_claim_ids == (
        seeded["merchant_claim"],
    )
    assert resolved_row.evidence.category_claim_ids == (
        seeded["category_claim"],
    )
    assert len(resolved_row.evidence.resolution_event_ids) == 2
    assert statement.household.external_cash_movement.evidence.relationship_ids
    assert statement.close.state == "open"

    csv_bytes = render_period_statement_csv(statement)
    pdf_bytes = render_period_statement_pdf(statement)
    assert render_period_statement_csv(statement) == csv_bytes
    assert render_period_statement_pdf(statement) == pdf_bytes
    csv_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    summary = {
        row["key"]: int(row["cents"])
        for row in csv_rows
        if row["record_type"] == "household_total"
    }
    assert summary["income"] == 501_840
    assert summary["net_money_out"] == 230_810
    exported_resolved = next(
        row
        for row in csv_rows
        if row["record_type"] == "evidence_row"
        and row["transaction_ids"] == str(seeded["resolved_purchase"])
    )
    assert exported_resolved["transaction_split_ids"]
    assert exported_resolved["statement_line_ids"]
    assert exported_resolved["source_document_ids"]
    assert exported_resolved["source_anchor_ids"] == str(
        seeded["resolved_anchor"]
    )
    assert exported_resolved["merchant_entity_ids"]
    assert exported_resolved["merchant_pattern_ids"]
    assert exported_resolved["merchant_claim_ids"] == str(
        seeded["merchant_claim"]
    )
    assert exported_resolved["category_claim_ids"] == str(
        seeded["category_claim"]
    )
    assert exported_resolved["resolution_event_ids"]
    assert exported_resolved["category_ids"] == str(resolved_row.category_id)
    assert exported_resolved["relationship_ids"]
    assert exported_resolved["assertion_ids"]
    assert exported_resolved["canonical_merchant"] == "Acme Market"
    assert exported_resolved["category_name"] == "Groceries"
    assert exported_resolved["source"] == "statement"
    assert exported_resolved["reconciliation_state"] == "cleared"
    assert exported_resolved["resolution_disposition"] == "human_approved"
    assert pdf_bytes.startswith(b"%PDF-")
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        pdf_text = "\n".join(page.get_text() for page in document)
    assert "Income" in pdf_text
    assert "$5,018.40" in pdf_text
    assert "$2,308.10" in pdf_text
    assert f"source-anchors={seeded['resolved_anchor']}" in pdf_text
    assert f"merchant-claims={seeded['merchant_claim']}" in pdf_text
    assert f"category-claims={seeded['category_claim']}" in pdf_text
    assert "merchant=Acme Market" in pdf_text
    assert "reconciliation=cleared" in pdf_text


def test_period_statement_rejects_foreign_currency_contributors(empty_db):
    _seed_golden_month(empty_db)
    with engine.write_tx(empty_db) as conn:
        euro = _account(conn, "Euro cash", "cash", currency="EUR")
        expense = conn.execute(
            "SELECT id FROM categories WHERE name='Uncategorized'"
        ).fetchone()["id"]
        transaction_id = int(
            conn.execute(
                """
                INSERT INTO transactions(
                  account_id, posted_on, description, amount_cents, source,
                  external_id, recon_status, cleared_on, flow_kind
                )
                VALUES (?, '2026-06-22', 'EUR purchase', -1000, 'manual',
                        'fixture:eur', 'cleared', '2026-06-22', 'purchase')
                """,
                (euro,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (?, ?, -1000)
            """,
            (transaction_id, expense),
        )

    with pytest.raises(UnsupportedReportCurrency, match="EUR"):
        build_period_statement(empty_db, month=MONTH, home_currency="CAD")


@pytest.mark.parametrize(
    ("table", "column", "where"),
    (
        ("accounts", "currency", "name='Chequing'"),
        ("statement_lines", "currency", "raw_description='Payroll'"),
    ),
)
def test_period_statement_rejects_blank_contributing_currency(
    empty_db,
    table,
    column,
    where,
):
    _seed_golden_month(empty_db)
    with engine.write_tx(empty_db) as conn:
        conn.execute(f"UPDATE {table} SET {column}='' WHERE {where}")

    with pytest.raises(UnsupportedReportCurrency, match="blank"):
        build_period_statement(empty_db, month=MONTH, home_currency="CAD")


def test_period_statement_excludes_investments_from_liquid_position(empty_db):
    _seed_golden_month(empty_db)
    with engine.write_tx(empty_db) as conn:
        opening = int(
            conn.execute(
                "SELECT id FROM categories WHERE name='Opening'"
            ).fetchone()["id"]
        )
        investments = _account(conn, "Brokerage", "investment")
        document = _document(conn, "brokerage-june.pdf")
        _opening(conn, investments, opening, 80_000, "brokerage")
        repo_assertions.record_assertion(
            conn,
            account_id=investments,
            asof_date=AS_OF,
            asserted_cents=80_000,
            source_document_id=document,
            statement_period=MONTH,
        )

    statement = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )

    assert statement.household.opening_liquid_position.cents == 1_240_000
    assert statement.household.closing_liquid_position.cents == 1_511_030
    assert [
        (item.account_name, item.reason)
        for item in statement.liquid_position_exclusions
    ] == [("Brokerage", "unsupported_account_kind_v1")]


def _known_exception() -> dict[str, object]:
    return {
        "exception_type": "unconfirmed_merchant_category",
        "subject_kind": "transaction",
        "subject_id": "fixture:unresolved-purchase",
        "affected_ids": {"month": MONTH},
        "evidence": {"resolution": "unresolved"},
        "amount_cents": 6_420,
        "reason": "fixture expense still needs human review",
        "resolution_href": f"/statements/{MONTH}",
    }


def test_closed_report_is_frozen_and_carries_exact_close_evidence(empty_db):
    seeded = _seed_golden_month(empty_db)
    exception = _known_exception()
    with engine.write_tx(empty_db) as conn:
        repo_period_policy.acknowledge_preclose_exception(
            conn,
            MONTH,
            exception,
            actor="human:fixture",
            reason="reviewed before exception close",
            operation_key="fixture:fn148:preclose-ack",
            evidence={"surface": "period-statement-test"},
        )
        live = build_period_statement(
            conn,
            month=MONTH,
            home_currency="CAD",
        )
        snapshot = {"fixture_close_summary": True}
        snapshot.update(
            period_statement_snapshot_payload(
                live,
                close_state="closed_with_exceptions",
                exception_types=("unconfirmed_merchant_category",),
            )
        )
        closed = repo_period_policy.close_period(
            conn,
            MONTH,
            snapshot=snapshot,
            exceptions=(exception,),
            actor="human:fixture",
            reason="freeze golden report",
            operation_key="fixture:fn148:close",
        )
        frozen_json = str(closed["snapshot_json"])
        snapshot_id = int(closed["id"])
        cycle_id = int(closed["cycle_id"])
        exception_id = int(
            conn.execute(
                "SELECT id FROM period_close_exceptions WHERE cycle_id=?",
                (cycle_id,),
            ).fetchone()["id"]
        )
        acknowledgement_id = int(
            conn.execute(
                """
                SELECT acknowledgement.id
                FROM period_close_acknowledgements acknowledgement
                JOIN period_close_exceptions exception
                  ON exception.id=acknowledgement.exception_id
                WHERE exception.cycle_id=?
                """,
                (cycle_id,),
            ).fetchone()["id"]
        )

    # These labels are intentionally mutable reference data. A closed report
    # must continue to render the exact labels and claims frozen at sign-off.
    with engine.write_tx(empty_db) as conn:
        conn.execute(
            "UPDATE categories SET name='Renamed after close' WHERE name='Groceries'"
        )

    frozen = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    assert frozen.close.state == "closed_with_exceptions"
    assert frozen.close.cycle_id == cycle_id
    assert frozen.close.snapshot_id == snapshot_id
    assert frozen.close.snapshot_is_current is True
    assert frozen.close.exception_ids == (exception_id,)
    assert frozen.close.acknowledgement_ids == (acknowledgement_id,)
    assert frozen.close.exception_types == (
        "unconfirmed_merchant_category",
    )
    assert frozen.household.net_money_out.evidence.exception_ids == (
        exception_id,
    )
    assert frozen.household.net_money_out.evidence.acknowledgement_ids == (
        acknowledgement_id,
    )
    assert frozen.household.net_money_out.evidence.close_snapshot_ids == (
        snapshot_id,
    )
    resolved = next(
        row
        for row in frozen.rows
        if row.transaction_id == seeded["resolved_purchase"]
    )
    assert resolved.category_name == "Groceries"
    with engine.read_conn(empty_db) as conn:
        history = repo_period_policy.list_snapshot_history(conn, MONTH)
        assert len(history) == 1
        assert int(history[0]["is_current"]) == 1
        assert str(history[0]["snapshot_json"]) == frozen_json

    with engine.write_tx(empty_db) as conn:
        repo_period_policy.reopen_period(
            conn,
            MONTH,
            actor="human:fixture",
            reason="prove historical snapshot state",
            operation_key="fixture:fn148:reopen",
            affected_ids={"month": MONTH},
        )
    with engine.read_conn(empty_db) as conn:
        history = repo_period_policy.list_snapshot_history(conn, MONTH)
        assert int(history[0]["is_current"]) == 0
        assert str(history[0]["snapshot_json"]) == frozen_json
    reopened = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    assert reopened.close.state == "reopened"
    assert any(
        row.category_name == "Renamed after close" for row in reopened.rows
    )


def test_closed_legacy_snapshot_is_explicitly_unavailable(empty_db):
    _seed_golden_month(empty_db)
    with engine.write_tx(empty_db) as conn:
        repo_period_policy.close_period(
            conn,
            MONTH,
            snapshot={"legacy": True},
            exceptions=(),
            actor="human:fixture",
            reason="legacy close without FN148 report",
            operation_key="fixture:fn148:legacy",
        )

    with pytest.raises(FrozenPeriodStatementUnavailable, match="predates"):
        build_period_statement(empty_db, month=MONTH, home_currency="CAD")


def test_closed_report_rejects_tampered_frozen_content(empty_db):
    _seed_golden_month(empty_db)
    with engine.write_tx(empty_db) as conn:
        live = build_period_statement(
            conn,
            month=MONTH,
            home_currency="CAD",
        )
        snapshot = period_statement_snapshot_payload(
            live,
            close_state="clean_closed",
        )
        snapshot["fn148_period_statement"]["household"]["income"]["cents"] += 1
        repo_period_policy.close_period(
            conn,
            MONTH,
            snapshot=snapshot,
            exceptions=(),
            actor="human:fixture",
            reason="store deliberately inconsistent fixture",
            operation_key="fixture:fn148:tampered",
        )

    with pytest.raises(FrozenPeriodStatementIntegrityError, match="digest"):
        build_period_statement(empty_db, month=MONTH, home_currency="CAD")


def test_period_statement_api_csv_pdf_share_digest_and_cents(
    empty_db,
    monkeypatch,
):
    _seed_golden_month(empty_db)
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("API_TOKEN", "period-statement-test-token")
    monkeypatch.setenv("HOME_CURRENCY", "CAD")
    from app.config import get_settings
    from app.web.app import create_app

    get_settings.cache_clear()
    client = TestClient(create_app())
    auth = {"Authorization": "Bearer period-statement-test-token"}

    response = client.get(f"/api/period-statements/{MONTH}", headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert payload["household"]["income"]["cents"] == 501_840
    assert payload["household"]["net_money_out"]["cents"] == 230_810

    csv_response = client.get(
        f"/api/period-statements/{MONTH}/export?format=csv",
        headers=auth,
    )
    assert csv_response.status_code == 200
    assert f"finn-nancy-{MONTH}.csv" in csv_response.headers[
        "content-disposition"
    ]
    csv_rows = list(
        csv.DictReader(io.StringIO(csv_response.content.decode("utf-8")))
    )
    assert {
        row["report_digest"] for row in csv_rows
    } == {payload["report_digest"]}
    assert next(
        int(row["cents"])
        for row in csv_rows
        if row["record_type"] == "household_total"
        and row["key"] == "net_money_out"
    ) == payload["household"]["net_money_out"]["cents"]

    pdf_response = client.get(
        f"/api/period-statements/{MONTH}/export?format=pdf",
        headers=auth,
    )
    assert pdf_response.status_code == 200
    assert pdf_response.content.startswith(b"%PDF-")
    assert f"finn-nancy-{MONTH}.pdf" in pdf_response.headers[
        "content-disposition"
    ]
    with fitz.open(
        stream=pdf_response.content,
        filetype="pdf",
    ) as document:
        pdf_text = "\n".join(page.get_text() for page in document)
    assert payload["report_digest"] in pdf_text
    assert "$2,308.10" in pdf_text
    get_settings.cache_clear()
