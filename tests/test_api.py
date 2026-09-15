from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.db import engine, repo_close, repo_ledger, repo_merchant_knowledge
from app.db.repo_merchant_knowledge import Evidence
from app.ingest.schemas import ExtractedReceipt


def _client():
    # No `with` block -> lifespan (worker + warm) does not run; routes only.
    from app.web.app import create_app

    return TestClient(create_app())


@pytest.fixture
def api_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-secret-123")
    from app.config import get_settings

    get_settings.cache_clear()
    yield "test-secret-123"
    get_settings.cache_clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _api_scope(
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *(headers or [])],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }


async def _call_asgi(app, scope, receive):
    messages = []

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def _asgi_status(messages) -> int:
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


def _asgi_json(messages) -> dict:
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return json.loads(body.decode("utf-8"))


async def _drive_worker_until_transaction(app_env, doc_id: int, fake_llm, receipt: ExtractedReceipt):
    from app.workers.runner import run_worker

    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(stop, llm=fake_llm(receipt), poll_seconds=0.05))
    txn = None
    for _ in range(100):
        await asyncio.sleep(0.05)
        with engine.read_conn(app_env) as conn:
            txn = conn.execute(
                "SELECT * FROM transactions WHERE source_document_id=?",
                (doc_id,),
            ).fetchone()
        if txn is not None:
            break
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    return txn


def test_api_disabled_when_token_unset(app_env, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    r = _client().get("/api/reconcile")
    assert r.status_code == 503
    assert "API disabled" in r.json()["detail"]


def test_api_auth_rejects_missing_and_wrong_token(app_env, api_token):
    client = _client()
    missing = client.get("/api/reconcile")
    wrong = client.get("/api/reconcile", headers=_auth("wrong"))
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_api_auth_rejects_missing_token_before_body_receive(app_env, api_token):
    from app.web.app import create_app

    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        return {"type": "http.request", "body": b"x" * 10_000_000, "more_body": False}

    messages = asyncio.run(
        _call_asgi(
            create_app(),
            _api_scope(
                method="POST",
                path="/api/ingest",
                headers=[(b"content-length", b"10000000")],
            ),
            receive,
        )
    )
    assert calls == 0
    assert _asgi_status(messages) == 401
    assert _asgi_json(messages) == {"detail": "invalid or missing bearer token"}


def test_api_disabled_rejects_before_body_receive(app_env, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    from app.config import get_settings
    from app.web.app import create_app

    get_settings.cache_clear()
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        return {"type": "http.request", "body": b"x" * 10_000_000, "more_body": False}

    messages = asyncio.run(
        _call_asgi(
            create_app(),
            _api_scope(
                method="POST",
                path="/api/ingest",
                headers=[(b"content-length", b"10000000")],
            ),
            receive,
        )
    )
    assert calls == 0
    assert _asgi_status(messages) == 503
    assert _asgi_json(messages) == {
        "detail": "API disabled: set API_TOKEN to enable programmatic access"
    }


def test_api_rejects_oversized_authorized_upload_and_accepts_small(
    app_env, api_token, make_jpeg, monkeypatch
):
    monkeypatch.setenv("API_MAX_BODY_BYTES", "1500")
    from app.config import get_settings

    get_settings.cache_clear()
    client = _client()

    big = client.post(
        "/api/ingest",
        files={"file": ("too-big.bin", b"x" * 2_000, "application/octet-stream")},
        headers=_auth(api_token),
    )
    assert big.status_code == 413
    assert big.json() == {"detail": "request body too large (max 1500 bytes)"}

    small = client.post(
        "/api/ingest",
        files={"file": ("receipt.jpg", make_jpeg(size=(10, 10)), "image/jpeg")},
        headers=_auth(api_token),
    )
    assert small.status_code == 200
    assert small.json()["status"] == "staged"


def test_api_counts_streamed_body_without_content_length(app_env, api_token, monkeypatch):
    monkeypatch.setenv("API_MAX_BODY_BYTES", "20")
    from app.config import get_settings
    from app.web.app import create_app

    get_settings.cache_clear()
    chunks = [
        {"type": "http.request", "body": b'{"amount_', "more_body": True},
        {
            "type": "http.request",
            "body": b'cents": 1234, "posted_on": "2026-06-12"}',
            "more_body": False,
        },
    ]

    async def receive():
        return chunks.pop(0)

    messages = asyncio.run(
        _call_asgi(
            create_app(),
            _api_scope(
                method="POST",
                path="/api/transactions",
                headers=[
                    (b"authorization", f"Bearer {api_token}".encode("ascii")),
                    (b"content-type", b"application/json"),
                ],
            ),
            receive,
        )
    )
    assert _asgi_status(messages) == 413
    assert _asgi_json(messages) == {"detail": "request body too large (max 20 bytes)"}


def test_api_ingest_stages_document(app_env, api_token, make_jpeg):
    client = _client()
    files = {"file": ("receipt.jpg", make_jpeg(), "image/jpeg")}
    r = client.post("/api/ingest", files=files, headers=_auth(api_token))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "staged"
    assert body["duplicate"] is False
    assert body["doc_id"]
    assert body["job_id"]
    assert body["status_url"].endswith(f"/api/documents/{body['doc_id']}")
    assert body["sha256"]

    with engine.read_conn(app_env) as conn:
        doc = conn.execute(
            "SELECT * FROM source_documents WHERE id=?",
            (body["doc_id"],),
        ).fetchone()
        job = conn.execute(
            "SELECT * FROM jobs WHERE id=?",
            (body["job_id"],),
        ).fetchone()
    assert doc is not None and doc["status"] == "staged" and doc["kind"] == "receipt"
    assert job is not None and job["status"] == "pending" and job["type"] == "ingest_document"


def test_api_ingest_duplicate_returns_existing_doc(app_env, api_token, make_jpeg):
    client = _client()
    raw = make_jpeg()
    files = {"file": ("receipt.jpg", raw, "image/jpeg")}
    first = client.post("/api/ingest", files=files, headers=_auth(api_token)).json()
    files = {"file": ("receipt-again.jpg", raw, "image/jpeg")}
    second = client.post("/api/ingest", files=files, headers=_auth(api_token))
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["duplicate"] is True
    assert body["doc_id"] == first["doc_id"]
    assert body["job_id"] is None


def test_api_transactions_create_split_and_dedupe(app_env, api_token):
    client = _client()
    payload = {
        "amount_cents": -1299,
        "posted_on": "2026-06-12",
        "description": "API Coffee",
        "counterparty": "Night Owl Cafe",
        "category": "Restaurants",
        "external_id": "api-test-dedupe-1",
    }
    first = client.post("/api/transactions", json=payload, headers=_auth(api_token))
    assert first.status_code == 200
    body = first.json()
    assert body["created"] is True
    assert body["status"] == "created"
    assert body["external_id"] == "api-test-dedupe-1"
    assert body["account_id"] == 1
    assert body["category_id"] == 5
    assert body["category_name"] == "Restaurants"
    assert body["category_fallback"] is False

    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id=?",
            (body["transaction_id"],),
        ).fetchone()
    assert split is not None
    assert split["amount_cents"] == -1299

    second = client.post("/api/transactions", json=payload, headers=_auth(api_token))
    assert second.status_code == 200
    again = second.json()
    assert again["created"] is False
    assert again["status"] == "existing"
    assert again["transaction_id"] == body["transaction_id"]
    assert again["category_id"] == 5
    assert again["category_name"] == "Restaurants"

    changed_category = client.post(
        "/api/transactions",
        json={**payload, "category": "Groceries"},
        headers=_auth(api_token),
    )
    assert changed_category.status_code == 422
    assert "different transaction semantics" in changed_category.json()["detail"]
    with engine.read_conn(app_env) as conn:
        persisted = conn.execute(
            """SELECT category.id, category.name
               FROM transaction_splits split
               JOIN categories category ON category.id=split.category_id
               WHERE split.transaction_id=?""",
            (body["transaction_id"],),
        ).fetchone()
    assert dict(persisted) == {"id": 5, "name": "Restaurants"}


def test_api_transaction_insert_is_blocked_in_closed_month(app_env, api_token):
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, "2026-06", reason="reviewer repro")

    response = _client().post(
        "/api/transactions",
        json={
            "amount_cents": -1299,
            "posted_on": "2026-06-22",
            "description": "Closed Month Coffee",
            "category": "Restaurants",
            "external_id": "api-closed-month-repro",
            "flow_kind": "purchase",
        },
        headers=_auth(api_token),
    )
    assert response.status_code == 422
    assert "2026-06 is closed" in response.json()["detail"]
    with engine.read_conn(app_env) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE external_id='api-closed-month-repro'"
        ).fetchone()[0]
    assert count == 0


def test_api_transactions_without_external_id_create_distinct_rows(app_env, api_token):
    client = _client()
    payload = {
        "amount_cents": -725,
        "posted_on": "2026-06-12",
        "description": "API Coffee",
        "counterparty": "Night Owl Cafe",
        "category": "Restaurants",
    }
    first = client.post("/api/transactions", json=payload, headers=_auth(api_token))
    second = client.post("/api/transactions", json=payload, headers=_auth(api_token))
    assert first.status_code == 200
    assert second.status_code == 200
    one = first.json()
    two = second.json()
    assert one["created"] is True
    assert two["created"] is True
    assert one["transaction_id"] != two["transaction_id"]
    assert one["external_id"].startswith("api:")
    assert two["external_id"].startswith("api:")
    assert one["external_id"] != two["external_id"]

    ids = {one["transaction_id"], two["transaction_id"]}
    with engine.read_conn(app_env) as conn:
        rows = conn.execute(
            "SELECT id FROM transactions WHERE id IN (?, ?)",
            (one["transaction_id"], two["transaction_id"]),
        ).fetchall()
    assert {int(row["id"]) for row in rows} == ids


def test_api_transactions_unknown_account_is_422(app_env, api_token):
    payload = {
        "amount_cents": -1299,
        "posted_on": "2026-06-12",
        "description": "Wrong Account",
        "counterparty": "Night Owl Cafe",
        "category": "Restaurants",
        "account_id": 5003,
        "external_id": "api-test-unknown-account",
    }
    r = _client().post("/api/transactions", json=payload, headers=_auth(api_token))
    assert r.status_code == 422
    assert r.json()["detail"] == "unknown account_id: 5003"


def test_api_transactions_category_fallback_is_reported(app_env, api_token):
    payload = {
        "amount_cents": -1299,
        "posted_on": "2026-06-12",
        "description": "Unknown Category",
        "counterparty": "Night Owl Cafe",
        "category": "Not A Real Category",
        "external_id": "api-test-category-fallback",
    }
    r = _client().post("/api/transactions", json=payload, headers=_auth(api_token))
    assert r.status_code == 200
    body = r.json()
    assert body["category_name"] == "Uncategorized"
    assert body["category_fallback"] is True

    with engine.read_conn(app_env) as conn:
        split = conn.execute(
            """SELECT c.name
               FROM transaction_splits s
               JOIN categories c ON c.id=s.category_id
               WHERE s.transaction_id=?""",
            (body["transaction_id"],),
        ).fetchone()
    assert split["name"] == "Uncategorized"


def test_api_category_reports_exclude_unconfirmed_assignments(
    app_env,
    api_token,
):
    with engine.write_tx(app_env) as conn:
        account_id = int(
            conn.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()[0]
        )
        category_id = int(
            conn.execute(
                "SELECT id FROM categories WHERE name='Restaurants'"
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO budgets(category_id, period_month, amount_cents)
            VALUES (?, '2099-01', 5000)
            """,
            (category_id,),
        )
        resolved_transaction_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=account_id,
                posted_on="2099-01-10",
                description="CONFIRMED RESTAURANT",
                counterparty="Confirmed Restaurant",
                amount_cents=-2000,
                source="manual",
                external_id="api-report:resolved-category",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
        )
        resolved_split_id = repo_ledger.insert_split(
            conn,
            transaction_id=resolved_transaction_id,
            category_id=category_id,
            amount_cents=-2000,
        )
        repo_merchant_knowledge.confirm_category(
            conn,
            descriptor="CONFIRMED RESTAURANT",
            category_id=category_id,
            scope=repo_merchant_knowledge.scope_for(
                conn,
                account_id=account_id,
            ),
            operation_key="test:api-report:resolved-category",
            actor="test:operator",
            reason="operator confirmed the synthetic report category",
            evidence=Evidence(
                transaction_id=resolved_transaction_id,
                transaction_split_id=resolved_split_id,
            ),
        )
        unresolved_transaction_id = int(
            repo_ledger.insert_transaction(
                conn,
                account_id=account_id,
                posted_on="2099-01-11",
                description="UNCONFIRMED RESTAURANT",
                counterparty="Unconfirmed Restaurant",
                amount_cents=-1299,
                source="manual",
                external_id="api-report:unresolved-category",
                source_document_id=None,
                source_confidence=1.0,
                flow_kind="purchase",
            )
        )
        repo_ledger.insert_split(
            conn,
            transaction_id=unresolved_transaction_id,
            category_id=category_id,
            amount_cents=-1299,
        )

    response = _client().get(
        "/api/reports/monthly_spend_by_category",
        params={"month": "2099-01"},
        headers=_auth(api_token),
    )

    assert response.status_code == 200
    report = response.json()
    assert len(report["rows"]) == 1
    assert {
        key: report["rows"][0][key]
        for key in (
            "month",
            "category_id",
            "category_name",
            "category_kind",
            "amount_cents",
            "magnitude_cents",
        )
    } == {
        "month": "2099-01",
        "category_id": category_id,
        "category_name": "Restaurants",
        "category_kind": "expense",
        "amount_cents": -2000,
        "magnitude_cents": 2000,
    }
    completeness = report["semantic_completeness"]
    assert completeness["complete"] is False
    assert completeness["excluded_transaction_count"] == 0
    assert completeness["transaction_count"] == 2
    assert completeness["category_resolution"] == {
        "money_out_cents": 3299,
        "resolved_expense_cents": 2000,
        "excluded_expense_cents": 1299,
        "resolved_split_count": 1,
        "unresolved_split_count": 1,
    }
    assert "without accepted category evidence" in completeness["notice"]

    budget_response = _client().get(
        "/api/reports/budget_vs_actual",
        headers=_auth(api_token),
    )

    assert budget_response.status_code == 200
    budget_report = budget_response.json()
    assert budget_report["month"] == "2099-01"
    budget_row = next(
        row
        for row in budget_report["rows"]
        if row["category_id"] == category_id
    )
    assert {
        key: budget_row[key]
        for key in (
            "budget_cents",
            "actual_cents",
            "remaining_cents",
            "pct_used",
        )
    } == {
        "budget_cents": 5000,
        "actual_cents": 2000,
        "remaining_cents": 3000,
        "pct_used": 40.0,
    }
    budget_resolution = budget_report["semantic_completeness"][
        "category_resolution"
    ]
    assert budget_resolution == completeness["category_resolution"]
    assert (
        budget_row["actual_cents"]
        == budget_resolution["resolved_expense_cents"]
    )
    assert (
        budget_resolution["resolved_expense_cents"]
        + budget_resolution["excluded_expense_cents"]
        == budget_resolution["money_out_cents"]
    )


def test_api_transactions_sign_validation_by_category_kind(app_env, api_token):
    client = _client()

    positive_expense = client.post(
        "/api/transactions",
        json={
            "amount_cents": 1299,
            "posted_on": "2026-06-12",
            "description": "Wrong Sign Expense",
            "category": "Restaurants",
            "external_id": "api-test-positive-expense",
        },
        headers=_auth(api_token),
    )
    assert positive_expense.status_code == 422
    assert "must be negative for expense category 'Restaurants'" in positive_expense.json()["detail"]

    negative_income = client.post(
        "/api/transactions",
        json={
            "amount_cents": -50000,
            "posted_on": "2026-06-12",
            "description": "Wrong Sign Income",
            "category": "Salary",
            "external_id": "api-test-negative-income",
        },
        headers=_auth(api_token),
    )
    assert negative_income.status_code == 422
    assert "must be positive for income category 'Salary'" in negative_income.json()["detail"]

    positive_no_category = client.post(
        "/api/transactions",
        json={
            "amount_cents": 50000,
            "posted_on": "2026-06-12",
            "description": "Missing Income Category",
            "external_id": "api-test-positive-no-category",
        },
        headers=_auth(api_token),
    )
    assert positive_no_category.status_code == 422
    assert "supply an income category explicitly" in positive_no_category.json()["detail"]

    positive_income = client.post(
        "/api/transactions",
        json={
            "amount_cents": 50000,
            "posted_on": "2026-06-12",
            "description": "Right Sign Income",
            "category": "Salary",
            "external_id": "api-test-positive-income",
        },
        headers=_auth(api_token),
    )
    assert positive_income.status_code == 200
    income_body = positive_income.json()
    assert income_body["category_name"] == "Salary"
    with engine.read_conn(app_env) as conn:
        income_split = conn.execute(
            "SELECT amount_cents FROM transaction_splits WHERE transaction_id=?",
            (income_body["transaction_id"],),
        ).fetchone()
    assert income_split["amount_cents"] == 50000

    for amount, external_id in ((2500, "api-test-transfer-positive"), (-2500, "api-test-transfer-negative")):
        transfer = client.post(
            "/api/transactions",
            json={
                "amount_cents": amount,
                "posted_on": "2026-06-12",
                "description": "Transfer Sign Allowed",
                "category": "Savings transfer",
                "external_id": external_id,
            },
            headers=_auth(api_token),
        )
        assert transfer.status_code == 200
        assert transfer.json()["category_name"] == "Savings transfer"


def test_api_document_status_after_worker_processing(app_env, api_token, make_jpeg, fake_llm):
    client = _client()
    files = {"file": ("receipt.jpg", make_jpeg(), "image/jpeg")}
    cap = client.post("/api/ingest", files=files, headers=_auth(api_token)).json()

    receipt = ExtractedReceipt(
        merchant="Night Owl Cafe",
        purchased_on="2026-06-12",
        currency="CAD",
        subtotal_cents=4850,
        total_cents=4850,
        category_guess="Restaurants",
        confidence=0.9,
    )
    txn = asyncio.run(_drive_worker_until_transaction(app_env, cap["doc_id"], fake_llm, receipt))
    assert txn is not None

    r = client.get(f"/api/documents/{cap['doc_id']}", headers=_auth(api_token))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "processed"
    assert body["transaction_ids"]
    assert body["extractions"][0]["review_status"] == "auto"


def test_api_document_status_missing_is_404(app_env, api_token):
    r = _client().get("/api/documents/999999", headers=_auth(api_token))
    assert r.status_code == 404
