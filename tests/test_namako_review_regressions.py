"""Generated regressions for the three demonstrated Namako boundary defects."""
import base64
import json

import pytest

from app.api import finance
from app.config import get_settings
from app.db import engine, repo_documents
from tests.test_namako_finance import expense, finance_client  # noqa: F401

MAX_ID = 9223372036854775807


def request_for(expense):
    tid, split, sibling, category = expense
    return dict(operation_key="review:generated", transaction_id=tid, split_id=split,
                to_category_id=category, expected_state_digest=finance.transaction_detail(tid)["state_digest"],
                rationale="Generated boundary regression")


def authenticated_client(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "generated-review-only")
    get_settings.cache_clear()
    return finance_client(), {"Authorization": "Bearer generated-review-only"}


def attach_extraction(app_env, tid, raw, *, kind="receipt", status="approved"):
    with engine.write_tx(app_env) as conn:
        doc = repo_documents.insert_source_document(conn, kind=kind, original_name="generated.jpg",
            storage_ref="generated.jpg", sha256="b" * 64, mime_type="image/jpeg", status="processed")
        extraction = repo_documents.insert_extraction(conn, source_document_id=doc, doc_kind=kind,
            extracted_json=raw, confidence=1, external_id="generated-currency-review",
            proposed_account_id=None, proposed_category_id=None, review_status=status)
        repo_documents.link_extraction_txn(conn, extraction, tid, review_status=status)
        conn.execute("UPDATE transactions SET source_document_id=? WHERE id=?", (doc, tid))


def test_configured_usd_home_receipt_can_be_proposed_approved_and_undone(app_env, expense, monkeypatch):
    tid, split, sibling, category = expense
    monkeypatch.setenv("HOME_CURRENCY", "USD")
    get_settings.cache_clear()
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE accounts SET currency='USD' WHERE id=(SELECT account_id FROM transactions WHERE id=?)", (tid,))
    attach_extraction(app_env, tid, json.dumps({"currency": "USD", "total_cents": 1200}))
    before = finance.transaction_detail(tid)
    client, headers = authenticated_client(monkeypatch)
    response = client.post("/api/finance/category-proposals", headers=headers, json=request_for(expense))
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    assert client.post(f"/actions/{pid}/approve", follow_redirects=False).status_code == 303
    after = finance.transaction_detail(tid)
    assert after["splits"][0]["category_id"] == category
    assert after["splits"][1] == before["splits"][1]
    assert after["amount_cents"] == before["amount_cents"]
    assert after["evidence"]["extractions"] == before["evidence"]["extractions"]
    assert client.post(f"/actions/{pid}/revert", follow_redirects=False).status_code == 303
    assert finance.transaction_detail(tid)["splits"] == before["splits"]


@pytest.mark.parametrize("raw", [
    '{"currency":"USD"}', '{}', '{"currency":null}', '{"currency":""}',
    '{"currency":"not-a-code"}', '{"currency":12}', '[]',
])
def test_receipt_currency_fails_closed_at_proposal_and_human_boundary(app_env, expense, monkeypatch, raw):
    tid = expense[0]
    # A genuine proposal made before the receipt was attached remains stale.
    old = finance.propose_category(**request_for(expense))
    attach_extraction(app_env, tid, raw)
    before = finance.transaction_detail(tid)
    client, headers = authenticated_client(monkeypatch)
    response = client.post("/api/finance/category-proposals", headers=headers,
                           json={**request_for(expense), "operation_key": "review:fresh-receipt"})
    assert response.status_code == 422, response.text
    assert "currency" in response.text
    assert client.post(f"/actions/{old['id']}/approve", follow_redirects=False).status_code == 400
    # Exercise the human handler with a fresh digest too: stale-state protection
    # must not be the only defense against inconsistent approved evidence.
    from app.actions.recategorization import RecategorizationHandler
    with pytest.raises(ValueError, match="currency"):
        with engine.write_tx(app_env) as conn:
            RecategorizationHandler().apply(conn, {
                **old["payload"], "expected_state_digest": before["state_digest"],
                "_proposed_action_id": old["id"], "_actor": "operator:actions",
            })
    assert finance.transaction_detail(tid) == before
    assert finance.proposal_detail(old["id"])["status"] == "proposed"
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM category_operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM v_active_merchant_resolution_claims WHERE transaction_id=?", (tid,)).fetchone()[0] == 0


def test_statement_extraction_is_not_treated_as_receipt(app_env, expense):
    attach_extraction(app_env, expense[0], '{"rows":[]}', kind="statement")
    assert finance.propose_category(**request_for(expense))["status"] == "proposed"


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_receipt_pending_or_rejected_remains_blocked(app_env, expense, status):
    attach_extraction(app_env, expense[0], '{"currency":"CAD"}', status=status)
    before = finance.transaction_detail(expense[0])
    with pytest.raises(ValueError, match="review"):
        finance.propose_category(**request_for(expense))
    assert finance.transaction_detail(expense[0]) == before


@pytest.mark.parametrize("bad_id", [MAX_ID + 1, 0, -1, True, 1.0, "1"])
def test_shared_services_reject_non_sqlite_positive_integer_ids(app_env, expense, bad_id):
    body = request_for(expense)
    for call in (
        lambda: finance.transaction_detail(bad_id),
        lambda: finance.proposal_detail(bad_id),
        lambda: finance.search_transactions(account_id=bad_id),
        lambda: finance.search_transactions(category_id=bad_id),
        *(lambda field=field: finance.propose_category(**{**body, field: bad_id})
          for field in ("transaction_id", "split_id", "to_category_id")),
    ):
        with pytest.raises(ValueError, match="ID|id"):
            call()


def cursor_for(after_id):
    filters = dict(q=None, account_id=None, category_id=None, date_from=None,
                   date_to=None, flow_kind=None, review=None)
    return base64.urlsafe_b64encode(json.dumps({"filters": finance.state_digest(filters), "after_id": after_id}).encode()).decode()


def test_cursor_ids_are_bounded_without_changing_pagination_limit(app_env):
    for value in (MAX_ID + 1, True, 1.0, 0, -1):
        with pytest.raises(ValueError, match="cursor"):
            finance.search_transactions(cursor=cursor_for(value))
    assert finance.search_transactions(cursor=cursor_for(MAX_ID), limit=500)["next_cursor"] is None


def test_public_api_ids_reject_overflow_and_keep_sqlite_max_valid(app_env, expense, monkeypatch):
    client, headers = authenticated_client(monkeypatch)
    for raw_id, status in ((str(MAX_ID + 1), 422), (str(MAX_ID), 404), ("0", 422), ("-1", 422), ("true", 422), ("1.0", 422)):
        for resource in ("transactions", "proposals"):
            response = client.get(f"/api/finance/{resource}/{raw_id}", headers=headers)
            assert response.status_code == status, (response.url, response.text)
    for field in ("account_id", "category_id"):
        for raw_id in (str(MAX_ID + 1), "0", "-1", "true", "1.0"):
            response = client.get("/api/finance/transactions", params={field: raw_id}, headers=headers)
            assert response.status_code == 422, (response.url, response.text)
        response = client.get("/api/finance/transactions", params={field: MAX_ID}, headers=headers)
        assert response.status_code == 200 and response.json()["rows"] == []
    response = client.get("/api/finance/transactions", params={"cursor": cursor_for(MAX_ID + 1)}, headers=headers)
    assert response.status_code == 422, response.text
    body = request_for(expense)
    for field in ("transaction_id", "split_id", "to_category_id"):
        for value in (MAX_ID + 1, True, 1.0):
            response = client.post("/api/finance/category-proposals", headers=headers, json={**body, field: value})
            assert response.status_code == 422, response.text
    assert client.get("/api/finance/transactions", params={"unexpected": "field"}, headers=headers).status_code == 422
    assert client.post("/api/finance/category-proposals", headers=headers, json={**body, "actor": "user"}).status_code == 422


def test_cli_overflow_is_structured_validation_error(app_env):
    from typer.testing import CliRunner
    from app.cli import app
    runner = CliRunner()
    for args in (["transaction", str(MAX_ID + 1)], ["proposal", str(MAX_ID + 1)],
                 ["transactions", "--account-id", str(MAX_ID + 1)],
                 ["transactions", "--category-id", str(MAX_ID + 1)],
                 ["transactions", "--cursor", cursor_for(MAX_ID + 1)]):
        result = runner.invoke(app, ["finance", *args, "--db", app_env])
        assert result.exit_code == 1, (args, result.output, result.exception)
        assert "error" in json.loads(result.stdout)


def test_mcp_overflow_uses_shared_validation(app_env):
    pytest.importorskip("mcp")
    from app.mcp import server
    for call in (lambda: server.inspect_transaction(MAX_ID + 1),
                 lambda: server.inspect_proposal(MAX_ID + 1),
                 lambda: server.search_transactions(account_id=MAX_ID + 1),
                 lambda: server.search_transactions(category_id=MAX_ID + 1),
                 lambda: server.search_transactions(cursor=cursor_for(MAX_ID + 1))):
        with pytest.raises(ValueError):
            call()
