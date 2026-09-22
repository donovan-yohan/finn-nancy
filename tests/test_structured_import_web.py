from __future__ import annotations

import re
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from app.db import engine


def _client() -> TestClient:
    from app.web.app import create_app

    return TestClient(create_app())


def _preview(
    client: TestClient,
    raw: bytes,
    *,
    extra: dict[str, str] | None = None,
    filename: str = "statement.csv",
):
    data = {
        "account_id": "3",
        "adapter_kind": "mapped_csv",
        "date_column": "date",
        "description_column": "description",
        "amount_column": "amount",
        "currency_column": "currency",
        "pending_column": "pending",
        "fitid_column": "fitid",
        "date_format": "%Y-%m-%d",
        "delimiter": ",",
        "default_currency": "CAD",
        "period_start_on": "2026-06-01",
        "period_end_on": "2026-06-30",
        "statement_issued_on": "2026-07-01",
        "opening_balance": "100.00",
        "closing_balance": "90.00",
    }
    data.update(extra or {})
    return client.post(
        "/review/import/preview",
        data=data,
        files={"file": (filename, raw, "text/csv")},
        follow_redirects=False,
    )


def test_desktop_import_preview_confirm_and_structured_statement_review(app_env):
    client = _client()
    form = client.get("/review/import")
    assert form.status_code == 200
    assert "desktop month-end tool" in form.text.lower()
    assert "does not call a language model" in form.text.lower()
    assert 'accept=".csv,.ofx,.qfx' in form.text
    assert "column names are exact and never guessed" in form.text.lower()
    assert "<fieldset" in form.text
    assert "data-csv-mapping-fields" in form.text
    assert "data-structured-import-format" in form.text
    assert "csvFields.hidden = !enabled" in form.text

    raw = (
        b"date,description,amount,currency,pending,fitid\n"
        b"2026-06-10,Coffee,-10.00,CAD,posted,provider-1\n"
    )
    response = _preview(client, raw)
    assert response.status_code == 303
    location = response.headers["location"]
    assert urlparse(location).path.startswith("/review/import/")

    preview = client.get(location)
    assert preview.status_code == 200
    assert "immutable preview" in preview.text.lower()
    assert "coffee" in preview.text.lower()
    assert "confirm and stage all rows" in preview.text.lower()
    assert "data-import-confirmation-context" in preview.text
    assert "statement file" in preview.text.lower()
    assert "statement.csv" in preview.text
    assert "ledger account" in preview.text.lower()
    assert "SYNTHETIC_CARD_9001" in preview.text
    assert "provider-1" not in preview.text

    queue = client.get("/review")
    assert queue.status_code == 200
    assert "deterministic structured import" in queue.text.lower()
    assert location in queue.text

    import_id = int(urlparse(location).path.rsplit("/", 1)[-1])
    with engine.read_conn(app_env) as conn:
        imported = conn.execute(
            "SELECT * FROM structured_statement_imports WHERE id=?",
            (import_id,),
        ).fetchone()
    confirmed = client.post(
        f"/review/import/{import_id}/confirm",
        data={"expected_revision": str(imported["revision"])},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303

    detail = client.get(confirmed.headers["location"])
    assert detail.status_code == 200
    assert "open statement review" in detail.text.lower()
    with engine.read_conn(app_env) as conn:
        current = conn.execute(
            "SELECT * FROM structured_statement_imports WHERE id=?",
            (import_id,),
        ).fetchone()
        document_id = int(current["source_document_id"])
    statement = client.get(f"/review/statement/{document_id}")
    assert statement.status_code == 200
    assert "imported row evidence" in statement.text.lower()
    assert "raw row evidence" in statement.text.lower()
    assert "<iframe" not in statement.text.lower()
    assert "source anchor" in statement.text.lower()


def test_malformed_and_foreign_imports_have_actionable_fail_closed_ui(app_env):
    client = _client()
    malformed = _preview(
        client,
        (
            b"date,description,amount,currency,pending,fitid\n"
            b"not-a-date,Coffee,-10.00,CAD,posted,row-1\n"
        ),
    )
    page = client.get(malformed.headers["location"])
    assert page.status_code == 200
    assert "date invalid" in page.text.lower()
    assert "row 2" in page.text.lower()
    assert "confirm and stage all rows" not in page.text.lower()

    foreign = _preview(
        client,
        (
            b"date,description,amount,currency,pending,fitid\n"
            b"2026-06-10,Coffee,-10.00,EUR,posted,row-2\n"
        ),
    )
    page = client.get(foreign.headers["location"])
    assert "foreign currency" in page.text.lower()
    assert "record evidence for review" in page.text.lower()


def test_mobile_capture_remains_receipt_first_and_excludes_structured_formats(app_env):
    response = _client().get("/upload")
    assert response.status_code == 200
    assert "<h1>Upload files</h1>" in response.text
    assert "Receipt, invoice, or statement" in response.text
    assert 'data-intent="receipt"' in response.text
    assert 'accept="image/*,application/pdf"' in response.text
    assert ".csv" not in response.text.lower()
    assert ".ofx" not in response.text.lower()
    assert ".qfx" not in response.text.lower()


def test_numeric_overflow_is_a_review_diagnostic_not_a_server_error(app_env):
    response = _preview(
        _client(),
        (
            b"date,description,amount,currency,pending,fitid\n"
            b"2026-06-10,Coffee,92233720368547758.08,CAD,posted,row-1\n"
        ),
    )
    assert response.status_code == 303
    page = _client().get(response.headers["location"])
    assert page.status_code == 200
    assert "amount invalid" in page.text.lower()
    assert "confirm and stage all rows" not in page.text.lower()

    manual_overflow = _preview(
        _client(),
        (
            b"date,description,amount,currency,pending,fitid\n"
            b"2026-06-10,Coffee,-10.00,CAD,posted,row-2\n"
        ),
        extra={"opening_balance": "92233720368547758.08"},
    )
    assert manual_overflow.status_code == 400
    assert "text/html" in manual_overflow.headers["content-type"]
    assert "preview not created" in manual_overflow.text.lower()
    assert "reselect the original statement file" in manual_overflow.text.lower()
    assert "signed-cent storage limit" not in manual_overflow.text
    assert 'value="92233720368547758.08"' in manual_overflow.text


def test_preview_errors_stay_in_form_and_preserve_safe_mapping_values(app_env):
    client = _client()
    mapping_error = _preview(
        client,
        (
            b"posted_on,memo,amount\n"
            b"2026-06-10,provider-secret-987,-10.00\n"
        ),
        filename="private-card-4242.csv",
        extra={
            "date_column": "posted_on",
            "description_column": "memo",
            "amount_column": "signed_amount",
            "debit_column": "withdrawal",
            "default_currency": "USD",
        },
    )
    assert mapping_error.status_code == 400
    assert "text/html" in mapping_error.headers["content-type"]
    assert "preview not created" in mapping_error.text.lower()
    assert "no rows were staged" in mapping_error.text.lower()
    assert "reselect the original statement file" in mapping_error.text.lower()
    assert "private-card-4242.csv" not in mapping_error.text
    assert "provider-secret-987" not in mapping_error.text
    assert re.search(r'<option value="3"\s+selected', mapping_error.text)
    assert re.search(
        r'name="date_column"\s+value="posted_on"', mapping_error.text
    )
    assert re.search(
        r'name="description_column"\s+value="memo"', mapping_error.text
    )
    assert re.search(
        r'name="debit_column"\s+value="withdrawal"', mapping_error.text
    )
    assert 'value="USD"' in mapping_error.text

    oversized = _preview(
        client,
        b"x" * (5 * 1024 * 1024 + 1),
        filename="private-provider-account.qfx",
        extra={"adapter_kind": "ofx"},
    )
    assert oversized.status_code == 400
    assert "preview not created" in oversized.text.lower()
    assert "reselect the original statement file" in oversized.text.lower()
    assert "private-provider-account.qfx" not in oversized.text
    assert re.search(r'<option value="ofx"\s+selected', oversized.text)
