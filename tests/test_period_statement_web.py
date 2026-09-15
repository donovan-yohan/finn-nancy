from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import engine
from app.reporting.exports import UnsupportedPdfGlyphError
from app.reporting.models import CloseEvidence
from app.reporting.period_statements import (
    FrozenPeriodStatementIntegrityError,
    FrozenPeriodStatementUnavailable,
    build_period_statement,
)
from tests.test_period_statements import MONTH, _seed_golden_month


def _client(db_path: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("HOME_CURRENCY", "CAD")
    get_settings.cache_clear()
    from app.web.app import create_app

    return TestClient(create_app())


def _total_targets(body: str) -> tuple[list[str], set[str]]:
    targets = re.findall(
        r'class="[^"]*\bperiod-total-link\b[^"]*"[^>]*href="#([^"]+)"',
        body,
    )
    ids = set(re.findall(r'\bid="([^"]+)"', body))
    return targets, ids


def test_household_and_account_statements_render_canonical_evidence(
    empty_db,
    monkeypatch,
):
    seeded = _seed_golden_month(empty_db)
    client = _client(empty_db, monkeypatch)

    household = client.get(f"/statements?month={MONTH}")

    assert household.status_code == 200
    assert 'data-report-origin="live"' in household.text
    assert "live working report" in household.text.lower()
    assert "$5,018.40" in household.text
    assert "$2,308.10" in household.text
    assert "SQ *ACME MARKET 042" in household.text
    assert "CARD PURCHASE 0092" in household.text
    assert "Acme Market" in household.text
    assert "human approved" in household.text
    assert "unresolved" in household.text
    assert f"/txn/{seeded['resolved_purchase']}/edit" in household.text
    assert "Account-kind-v1 boundary" in household.text
    assert "credit is a" in household.text.lower()
    assert "Export CSV" in household.text
    assert "Export PDF" in household.text

    targets, ids = _total_targets(household.text)
    assert targets
    assert all(target in ids for target in targets)

    with engine.read_conn(empty_db, read_only=True) as conn:
        chequing_id = int(
            conn.execute("SELECT id FROM accounts WHERE name='Chequing'").fetchone()[
                "id"
            ]
        )
    canonical = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    expected_account_rows = sum(
        row.account_id == chequing_id for row in canonical.rows
    )

    account = client.get(
        f"/statements?month={MONTH}&account_id={chequing_id}"
    )

    assert account.status_code == 200
    assert "Chequing" in account.text
    assert "account statement" in account.text.lower()
    assert "Household-wide transfer neutrality" in account.text
    assert "Export CSV" not in account.text
    assert len(re.findall(r'\bid="report-row-', account.text)) == expected_account_rows
    targets, ids = _total_targets(account.text)
    assert targets
    assert all(target in ids for target in targets)


def test_frozen_statement_discloses_origin_currentness_digests_and_close_evidence(
    empty_db,
    monkeypatch,
):
    _seed_golden_month(empty_db)
    live = build_period_statement(
        empty_db,
        month=MONTH,
        home_currency="CAD",
    )
    frozen = live.model_copy(
        update={
            "close": CloseEvidence(
                state="closed_with_exceptions",
                cycle_id=9,
                snapshot_id=17,
                snapshot_digest="snapshot-digest-17",
                snapshot_is_current=True,
                exception_ids=(41,),
                exception_types=("unresolved_line",),
                acknowledgement_ids=(52,),
            )
        }
    )
    from app.web.routes import statements

    monkeypatch.setattr(statements, "build_period_statement", lambda *a, **k: frozen)
    response = _client(empty_db, monkeypatch).get(
        f"/statements?month={MONTH}"
    )

    assert response.status_code == 200
    assert 'data-report-origin="frozen_snapshot"' in response.text
    assert 'data-snapshot-current="true"' in response.text
    assert "frozen canonical report" in response.text.lower()
    assert "Snapshot #17" in response.text
    assert live.report_digest in response.text
    assert "snapshot-digest-17" in response.text
    assert f"/close?month={MONTH}#close-exception-41" in response.text
    assert f"/close?month={MONTH}#close-acknowledgement-52" in response.text
    assert "Acknowledgement proves review" in response.text


def test_period_statement_fail_closed_error_states_have_no_totals_or_exports(
    empty_db,
    monkeypatch,
):
    _seed_golden_month(empty_db)
    from app.web.routes import statements

    cases = (
        (
            FrozenPeriodStatementUnavailable(
                "closed period snapshot predates FN-148 report freezing"
            ),
            "legacy_snapshot_unavailable",
            "no frozen canonical report",
        ),
        (
            FrozenPeriodStatementIntegrityError(
                "frozen period statement digest mismatch"
            ),
            "snapshot_integrity_error",
            "failed its digest check",
        ),
    )
    for error, error_kind, expected in cases:
        def fail(*args, _error=error, **kwargs):
            raise _error

        monkeypatch.setattr(statements, "build_period_statement", fail)
        response = _client(empty_db, monkeypatch).get(
            f"/statements?month={MONTH}"
        )
        assert response.status_code == 409
        assert f'data-report-error-kind="{error_kind}"' in response.text
        assert expected in response.text
        assert "period-total-link" not in response.text
        assert "Export CSV" not in response.text


def test_period_statement_foreign_currency_and_invalid_inputs_fail_closed(
    empty_db,
    monkeypatch,
):
    _seed_golden_month(empty_db)
    client = _client(empty_db, monkeypatch)
    assert client.get(
        f"/statements?month={MONTH}&account_id=not-an-id"
    ).status_code == 400
    assert client.get(
        f"/statements?month={MONTH}&account_id=999999"
    ).status_code == 404

    with engine.write_tx(empty_db) as conn:
        category_id = int(
            conn.execute(
                "SELECT id FROM categories WHERE name='Uncategorized'"
            ).fetchone()["id"]
        )
        account_id = int(
            conn.execute(
                """
                INSERT INTO accounts(name, institution, kind, currency)
                VALUES ('Euro cash', 'Fixture Bank', 'cash', 'EUR')
                """
            ).lastrowid
        )
        transaction_id = int(
            conn.execute(
                """
                INSERT INTO transactions(
                  account_id, posted_on, description, amount_cents, source,
                  external_id, recon_status, cleared_on, flow_kind
                )
                VALUES (?, '2026-06-22', 'EUR purchase', -1000, 'manual',
                        'web:eur', 'cleared', '2026-06-22', 'purchase')
                """,
                (account_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO transaction_splits(transaction_id, category_id, amount_cents)
            VALUES (?, ?, -1000)
            """,
            (transaction_id, category_id),
        )

    foreign = client.get(f"/statements?month={MONTH}")
    assert foreign.status_code == 409
    assert 'data-report-error-kind="unsupported_currency"' in foreign.text
    assert "no totals or exports have been produced" in foreign.text.lower()
    assert "period-total-link" not in foreign.text
    assert client.get("/statements?month=June-2026").status_code == 400
    assert client.get("/statements/export?month=&format=csv").status_code == 400
    assert client.get(
        f"/statements/export?month={MONTH}&format=xlsx"
    ).status_code == 400


def test_period_statement_exports_delegate_canonical_model(
    empty_db,
    monkeypatch,
):
    _seed_golden_month(empty_db)
    client = _client(empty_db, monkeypatch)

    csv_response = client.get(
        f"/statements/export?month={MONTH}&format=csv"
    )
    pdf_response = client.get(
        f"/statements/export?month={MONTH}&format=pdf"
    )

    assert csv_response.status_code == 200
    assert csv_response.content.startswith(b"record_type,")
    assert csv_response.headers["content-disposition"].endswith(
        f'{MONTH}.csv"'
    )
    assert pdf_response.status_code == 200
    assert pdf_response.content.startswith(b"%PDF-")
    assert pdf_response.headers["content-disposition"].endswith(
        f'{MONTH}.pdf"'
    )


def test_unsupported_pdf_glyph_is_an_explicit_422_for_web_and_api(
    empty_db,
    monkeypatch,
):
    _seed_golden_month(empty_db)
    monkeypatch.setenv("DB_PATH", empty_db)
    monkeypatch.setenv("HOME_CURRENCY", "CAD")
    monkeypatch.setenv("API_TOKEN", "period-statement-test-token")
    get_settings.cache_clear()

    from app.api import service as api_service
    from app.web.app import create_app
    from app.web.routes import statements as web_statements

    def reject_pdf(_statement):
        raise UnsupportedPdfGlyphError((0x1F600,))

    monkeypatch.setattr(api_service, "render_period_statement_pdf", reject_pdf)
    monkeypatch.setattr(
        web_statements,
        "render_period_statement_pdf",
        reject_pdf,
    )
    client = TestClient(create_app())

    web_response = client.get(
        f"/statements/export?month={MONTH}&format=pdf"
    )
    api_response = client.get(
        f"/api/period-statements/{MONTH}/export?format=pdf",
        headers={"Authorization": "Bearer period-statement-test-token"},
    )

    assert web_response.status_code == 422
    assert api_response.status_code == 422
    assert "U+1F600" in web_response.json()["detail"]
    assert "U+1F600" in api_response.json()["detail"]
    assert client.get(
        f"/statements/export?month={MONTH}&format=csv"
    ).status_code == 200
