"""Generated household acceptance for the approval-only machine interface."""
from __future__ import annotations

import pytest

from app.api import service
from app.db import engine


@pytest.fixture
def expense(app_env):
    created = service.record_transaction(
        amount_cents=-1200, posted_on="2026-06-15", description="Synthetic mixed shop",
        category="Uncategorized", flow_kind="purchase", external_id="namako-synthetic",
    )
    transaction_id = created["transaction_id"]
    with engine.write_tx(app_env) as conn:
        split = conn.execute("SELECT * FROM transaction_splits WHERE transaction_id=?", (transaction_id,)).fetchone()
        conn.execute("UPDATE transaction_splits SET amount_cents=-800 WHERE id=?", (split["id"],))
        sibling = conn.execute(
            "INSERT INTO transaction_splits(transaction_id,category_id,amount_cents,memo) VALUES (?,?,?,?)",
            (transaction_id, split["category_id"], -400, "untouched sibling"),
        ).lastrowid
        target = conn.execute("SELECT id FROM categories WHERE name='Restaurants'").fetchone()[0]
    return transaction_id, int(split["id"]), int(sibling), int(target)


def test_inspect_propose_retry_then_human_apply_and_exact_undo(app_env, expense):
    from app.api import finance
    from app.actions.recategorization import RecategorizationHandler
    from app.db import repo_actions

    transaction_id, split_id, sibling_id, target = expense
    before = finance.transaction_detail(transaction_id)
    assert before["currency"] == "CAD"
    assert len(before["splits"]) == 2
    request = dict(
        operation_key="synthetic:category:1", transaction_id=transaction_id, split_id=split_id,
        to_category_id=target, expected_state_digest=before["state_digest"],
        rationale="The selected split is a meal; the sibling is not.", confidence=0.91,
    )
    proposal = finance.propose_category(**request)
    proposal_id = proposal["id"]
    assert proposal["actor"] == "agent:namako"
    assert proposal["status"] == "proposed"
    assert finance.transaction_detail(transaction_id) == before
    assert finance.propose_category(**request)["id"] == proposal_id
    with pytest.raises(finance.OperationConflict, match="operation_key"):
        finance.propose_category(**{**request, "rationale": "Different request"})
    with engine.write_tx(app_env) as conn:
        stored = repo_actions.get_proposal(conn, proposal_id)
        result = RecategorizationHandler().apply(conn, {
            **stored["payload"], "_proposed_action_id": proposal_id, "_actor": "operator:actions",
        })
        repo_actions.mark_applied(conn, proposal_id, status="approved", revert=result["revert"], detail=result["detail"], actor="user")
    after = finance.transaction_detail(transaction_id)
    assert after["splits"][0]["category_id"] == target
    assert after["splits"][1] == before["splits"][1]
    assert sum(s["amount_cents"] for s in after["splits"]) == -1200
    assert finance.propose_category(**request)["id"] == proposal_id
    with engine.write_tx(app_env) as conn:
        stored = repo_actions.get_proposal(conn, proposal_id)
        detail = RecategorizationHandler().revert(conn, stored["revert"])
        repo_actions.mark_reverted(conn, proposal_id, detail=detail, actor="user")
    restored = finance.transaction_detail(transaction_id)
    assert restored["splits"] == before["splits"]
    assert restored["amount_cents"] == before["amount_cents"]
    assert [r["to_status"] for r in finance.proposal_detail(proposal_id)["audit"]] == ["proposed", "approved", "reverted"]


def test_search_bounded_filters_and_cursors_preserve_unknown(app_env, expense):
    from app.api import finance

    txn_id, split_id, sibling_id, target = expense
    second = service.record_transaction(amount_cents=-100, posted_on="2026-06-16",
                                        description="Synthetic unknown", external_id="namako-unknown")
    rows = finance.search_transactions(q="Synthetic", date_from="2026-06-01", date_to="2026-06-30", limit=1)
    assert len(rows["rows"]) == 1 and rows["next_cursor"]
    next_page = finance.search_transactions(q="Synthetic", date_from="2026-06-01", date_to="2026-06-30", limit=1, cursor=rows["next_cursor"])
    assert {r["id"] for r in rows["rows"] + next_page["rows"]} == {txn_id, second["transaction_id"]}
    assert next_page["next_cursor"] is None
    assert all(r["currency"] == "CAD" for r in rows["rows"])
    assert finance.search_transactions(q="Synthetic", review="needs_review")["rows"][0]["flow_kind"] == "unknown"
    assert finance.search_transactions(q="Synthetic", flow_kind="purchase")["rows"][0]["id"] == txn_id
    assert finance.search_transactions(category_id=target, q="Synthetic")["rows"] == []
    assert finance.categories()["rows"]
    with pytest.raises(ValueError):
        finance.search_transactions(limit=501)
    with pytest.raises(ValueError):
        finance.search_transactions(cursor=rows["next_cursor"], q="different")
    with pytest.raises(ValueError):
        finance.search_transactions(date_from="2026-02-30")


@pytest.mark.parametrize("change", ["description", "source", "sibling", "shape", "amount"])
def test_human_apply_refuses_stale_state_before_changes(app_env, expense, change):
    from app.api import finance
    from app.actions.recategorization import RecategorizationHandler
    from app.db import repo_actions

    txn_id, split_id, sibling_id, target = expense
    detail = finance.transaction_detail(txn_id)
    proposed = finance.propose_category(operation_key="stale:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=detail["state_digest"], rationale="Meal")
    with engine.write_tx(app_env) as conn:
        if change in {"description", "source"}:
            conn.execute(f"UPDATE transactions SET {change}=? WHERE id=?", ("intervening edit", txn_id))
        elif change == "sibling":
            conn.execute("UPDATE transaction_splits SET category_id=? WHERE id=?", (target, sibling_id))
        elif change == "shape":
            conn.execute("INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,?,0)", (txn_id, target))
        else:
            conn.execute("UPDATE transactions SET amount_cents=-1300 WHERE id=?", (txn_id,))
    before = finance.transaction_detail(txn_id)
    with pytest.raises(ValueError, match="stale"):
        with engine.write_tx(app_env) as conn:
            RecategorizationHandler().apply(conn, proposed["payload"])
    assert finance.transaction_detail(txn_id) == before
    assert finance.proposal_detail(proposed["id"])["status"] == "proposed"


def test_reject_unsafe_proposals_and_immutable_audit(app_env, expense):
    from app.api import finance

    txn_id, split_id, sibling_id, target = expense
    base = dict(operation_key="guard:1", transaction_id=txn_id, split_id=split_id,
                to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    with engine.read_conn(app_env) as conn:
        income = conn.execute("SELECT id FROM categories WHERE kind='income' LIMIT 1").fetchone()[0]
    for change in ({"to_category_id": income}, {"split_id": 999999}, {"evidence_refs": ["document:999999"]}, {"confidence": float("nan")}, {"operation_key": "x' SQL"}):
        with pytest.raises(ValueError):
            finance.propose_category(**{**base, **change})
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE transactions SET flow_kind='unknown' WHERE id=?", (txn_id,))
    with pytest.raises(ValueError, match="flow"):
        finance.propose_category(**{**base, "expected_state_digest": finance.transaction_detail(txn_id)["state_digest"]})
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE transactions SET flow_kind='purchase' WHERE id=?", (txn_id,))
    p = finance.propose_category(**{**base, "expected_state_digest": finance.transaction_detail(txn_id)["state_digest"]})
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with engine.write_tx(app_env) as conn:
            conn.execute("UPDATE proposed_action_audit SET actor='fake' WHERE proposed_action_id=?", (p["id"],))


def finance_client():
    from fastapi.testclient import TestClient
    from app.web.app import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


def test_authenticated_json_proposal_human_html_approve_report_and_undo(app_env, expense, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("API_TOKEN", "synthetic-only")
    get_settings.cache_clear()
    client = finance_client()
    headers = {"Authorization": "Bearer synthetic-only"}
    txn_id, split_id, sibling_id, target = expense
    for path in ("transactions", f"transactions/{txn_id}", "categories", "proposals/1"):
        assert client.get(f"/api/finance/{path}").status_code == 401
    detail = client.get(f"/api/finance/transactions/{txn_id}", headers=headers).json()
    body = dict(operation_key="http:1", transaction_id=txn_id, split_id=split_id, to_category_id=target,
                expected_state_digest=detail["state_digest"], rationale="Synthetic meal split")
    assert client.post("/api/finance/category-proposals", json=body).status_code == 401
    response = client.post("/api/finance/category-proposals", json=body, headers=headers)
    assert response.status_code == 200, response.text
    proposal_id = response.json()["id"]
    assert client.get("/actions").status_code == 200
    assert client.post(f"/actions/{proposal_id}/approve", follow_redirects=False).status_code == 303
    changed = client.get(f"/api/finance/transactions/{txn_id}", headers=headers).json()
    assert changed["splits"][0]["category_id"] == target
    assert changed["splits"][1] == detail["splits"][1]
    assert client.get("/api/period-statements/2026-06", headers=headers).status_code == 200
    assert client.get(f"/api/finance/proposals/{proposal_id}", headers=headers).json()["audit"][-1]["actor"] == "user"
    assert client.post("/api/finance/category-proposals", json=body, headers=headers).json()["id"] == proposal_id
    assert client.post("/api/finance/category-proposals", json={**body, "rationale": "conflict"}, headers=headers).status_code == 409
    for operation in ("approve", "revert", "sql"):
        assert client.post(f"/api/finance/proposals/{proposal_id}/{operation}", headers=headers).status_code == 404
    assert client.post(f"/actions/{proposal_id}/revert", follow_redirects=False).status_code == 303
    restored = client.get(f"/api/finance/transactions/{txn_id}", headers=headers).json()
    assert restored["splits"] == detail["splits"]
    assert client.get("/api/finance/transactions?limit=501", headers=headers).status_code == 422
    assert client.post("/api/finance/category-proposals", json={**body, "actor": "user"}, headers=headers).status_code == 422


def test_cli_json_and_proposal_only_mcp_surface(app_env, expense):
    import asyncio
    import json
    from typer.testing import CliRunner
    from app.cli import app

    runner = CliRunner()
    help_result = runner.invoke(app, ["finance", "--help"])
    assert help_result.exit_code == 0, help_result.output
    txn_id, split_id, sibling_id, target = expense
    query = runner.invoke(app, ["finance", "transactions", "--db", app_env, "--q", "Synthetic", "--limit", "1"])
    assert query.exit_code == 0, query.output
    assert json.loads(query.stdout)["rows"][0]["id"] == txn_id
    inspect = runner.invoke(app, ["finance", "transaction", str(txn_id), "--db", app_env])
    assert inspect.exit_code == 0, inspect.output
    digest = json.loads(inspect.stdout)["state_digest"]
    propose = runner.invoke(app, ["finance", "propose-category", "--db", app_env,
        "--operation-key", "cli:1", "--transaction-id", str(txn_id), "--split-id", str(split_id),
        "--to-category-id", str(target), "--expected-state-digest", digest, "--rationale", "Synthetic meal"])
    assert propose.exit_code == 0, propose.output
    assert json.loads(propose.stdout)["status"] == "proposed"
    bad = runner.invoke(app, ["finance", "transactions", "--limit", "999"])
    assert bad.exit_code != 0

def test_mcp_proposal_only_surface_and_execution(app_env, expense):
    import asyncio
    pytest.importorskip("mcp")
    from app.mcp.server import build_server
    from app.api import finance

    readonly = build_server(read_only=True)
    names = {t.name for t in asyncio.run(readonly.list_tools())}
    reads = {"search_transactions", "inspect_transaction", "list_categories", "inspect_proposal", "period_statement"}
    assert reads <= names
    assert not {"propose_category", "add_transaction", "ingest_document"} & names
    proposal_server = build_server(proposals=True)
    assert {t.name for t in asyncio.run(proposal_server.list_tools())} == names | {"propose_category"}
    with pytest.raises(ValueError, match="incompatible"):
        build_server(read_only=True, proposals=True)
    raw = asyncio.run(proposal_server.call_tool("search_transactions", {"q": "Synthetic", "limit": 1}))
    assert raw
    txn_id, split_id, sibling_id, target = expense
    request = dict(operation_key="mcp:1", transaction_id=txn_id, split_id=split_id, to_category_id=target,
        expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Synthetic meal")
    asyncio.run(proposal_server.call_tool("propose_category", request))
    with engine.read_conn(app_env) as conn:
        proposal_id = conn.execute("SELECT proposed_action_id FROM category_operations WHERE operation_key='mcp:1'").fetchone()[0]
    assert finance.proposal_detail(proposal_id)["status"] == "proposed"
    assert finance.transaction_detail(txn_id)["state_digest"] == request["expected_state_digest"]


@pytest.mark.parametrize("when", ["before_proposal", "before_apply", "before_undo"])
def test_closed_period_guards_all_stages(app_env, expense, when):
    from app.api import finance
    from app.db import repo_close
    from app.actions.recategorization import RecategorizationHandler

    txn_id, split_id, sibling_id, target = expense
    body = dict(operation_key="closed:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    handler = RecategorizationHandler()
    if when != "before_proposal":
        p = finance.propose_category(**body)
    if when == "before_undo":
        with engine.write_tx(app_env) as conn:
            applied = handler.apply(conn, p["payload"])
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, "2026-06", reason="Synthetic close")
    before = finance.transaction_detail(txn_id)
    with pytest.raises(ValueError, match="closed|locked"):
        if when == "before_proposal":
            finance.propose_category(**body)
        else:
            with engine.write_tx(app_env) as conn:
                if when == "before_apply":
                    handler.apply(conn, p["payload"])
                else:
                    handler.revert(conn, applied["revert"])
    assert finance.transaction_detail(txn_id) == before


def test_claim_failure_rolls_back_category_and_audit(app_env, expense, monkeypatch):
    from app.api import finance
    from app.db import repo_merchant_knowledge

    txn_id, split_id, sibling_id, target = expense
    p = finance.propose_category(operation_key="rollback:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    before = finance.transaction_detail(txn_id)
    def fail(*args, **kwargs):
        raise ValueError("synthetic claim failure")
    monkeypatch.setattr(repo_merchant_knowledge, "confirm_category", fail)
    response = finance_client().post(f"/actions/{p['id']}/approve", follow_redirects=False)
    assert response.status_code == 400
    assert "synthetic claim failure" in response.text
    assert finance.transaction_detail(txn_id) == before
    assert len(finance.proposal_detail(p["id"])["audit"]) == 1


def test_exact_undo_refuses_sibling_change(app_env, expense):
    from app.api import finance
    from app.actions.recategorization import RecategorizationHandler

    txn_id, split_id, sibling_id, target = expense
    p = finance.propose_category(operation_key="undo:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    with engine.write_tx(app_env) as conn:
        result = RecategorizationHandler().apply(conn, p["payload"])
        conn.execute("UPDATE transaction_splits SET category_id=? WHERE id=?", (target, sibling_id))
    before = finance.transaction_detail(txn_id)
    with pytest.raises(ValueError, match="changed"):
        with engine.write_tx(app_env) as conn:
            RecategorizationHandler().revert(conn, result["revert"])
    assert finance.transaction_detail(txn_id) == before


def test_source_review_and_field_evidence_are_in_digest(app_env, expense):
    from app.api import finance

    txn_id, split_id, sibling_id, target = expense
    with engine.write_tx(app_env) as conn:
        doc_id = conn.execute("INSERT INTO source_documents(kind,storage_ref,status) VALUES ('statement','synthetic-namako','processed')").lastrowid
        conn.execute("UPDATE transactions SET source_document_id=? WHERE id=?", (doc_id, txn_id))
        review_id = conn.execute("INSERT INTO statement_reviews(source_document_id,account_id) VALUES (?,1)", (doc_id,)).lastrowid
    before = finance.transaction_detail(txn_id)
    with engine.write_tx(app_env) as conn:
        conn.execute("""INSERT INTO statement_field_evidence(evidence_key,statement_review_id,field_name,
            original_value_json,confidence,origin) VALUES ('namako:field',?,'currency','\"CAD\"',1,'manual')""", (review_id,))
    after = finance.transaction_detail(txn_id)
    assert before["state_digest"] != after["state_digest"]
    assert after["evidence"]["statement_reviews"][0]["id"] == review_id


@pytest.mark.parametrize("unsafe", ["pending", "currency", "review", "document"])
def test_source_ambiguity_requires_review(app_env, expense, unsafe):
    from app.api import finance

    txn_id, split_id, sibling_id, target = expense
    with engine.write_tx(app_env) as conn:
        doc_id = conn.execute("INSERT INTO source_documents(kind,storage_ref,status) VALUES ('statement','synthetic-pending','processed')").lastrowid
        if unsafe == "document":
            conn.execute("UPDATE source_documents SET status='staged' WHERE id=?", (doc_id,))
        account_id = conn.execute("SELECT account_id FROM transactions WHERE id=?", (txn_id,)).fetchone()[0]
        conn.execute("""INSERT INTO statement_lines(source_document_id,account_id,posted_on,raw_description,
            amount_cents,currency,is_pending,row_hash,matched_transaction_id,match_status)
            VALUES (?,?,'2026-06-15','Synthetic bank row',-1200,?,?,?,?, 'matched')""",
            (doc_id, account_id, "USD" if unsafe == "currency" else "CAD", int(unsafe == "pending"), "synthetic-hash", txn_id))
        if unsafe == "review":
            conn.execute("INSERT INTO statement_reviews(source_document_id,account_id) VALUES (?,?)", (doc_id, account_id))
    with pytest.raises(ValueError, match="review|currency"):
        finance.propose_category(operation_key="source:1", transaction_id=txn_id, split_id=split_id,
            to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")


def test_claim_evidence_changes_invalidate_exact_undo(app_env, expense):
    from app.api import finance
    from app.db import repo_merchant_knowledge
    from app.db.repo_merchant_knowledge import Evidence

    txn_id, split_id, sibling_id, target = expense
    p = finance.propose_category(operation_key="knowledge:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    client = finance_client()
    assert client.post(f"/actions/{p['id']}/approve", follow_redirects=False).status_code == 303
    with engine.write_tx(app_env) as conn:
        repo_merchant_knowledge.confirm_category(conn, descriptor="Synthetic mixed shop", category_id=target,
            scope=repo_merchant_knowledge.scope_for_transaction(conn, txn_id),
            operation_key="human:intervening", actor="operator:actions", reason="Intervening confirmation",
            evidence=Evidence(transaction_id=txn_id, transaction_split_id=split_id))
    response = client.post(f"/actions/{p['id']}/revert", follow_redirects=False)
    assert response.status_code == 400
    assert "changed" in response.text


def test_read_only_and_concurrent_idempotency(app_env, expense, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from app.api import finance
    from app.config import get_settings

    txn_id, split_id, sibling_id, target = expense
    body = dict(operation_key="concurrent:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: finance.propose_category(**body)["id"], range(2)))
    assert ids[0] == ids[1]
    monkeypatch.setenv("READ_ONLY", "true")
    get_settings.cache_clear()
    assert finance.search_transactions(q="Synthetic")["rows"]
    with pytest.raises(ValueError, match="read-only"):
        finance.propose_category(**{**body, "operation_key": "readonly:1"})


@pytest.mark.parametrize("bad_cursor", ["not base64", "W10=", "e30=", "bnVsbA==", "MQ=="])
def test_malformed_cursor_is_validation_error(app_env, bad_cursor):
    from app.api import finance
    with pytest.raises(ValueError, match="cursor"):
        finance.search_transactions(cursor=bad_cursor)


def test_proposal_evidence_and_operation_receipts_are_immutable(app_env, expense):
    import sqlite3
    from app.api import finance

    txn_id, split_id, sibling_id, target = expense
    p = finance.propose_category(operation_key="immutable:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    for sql in ("DELETE FROM category_operations", "UPDATE category_operations SET actor='user'",
                "DELETE FROM proposed_action_audit", "UPDATE proposed_action_audit SET feedback='fake'"):
        with pytest.raises(sqlite3.IntegrityError):
            with engine.write_tx(app_env) as conn:
                conn.execute(sql)
    assert finance.proposal_detail(p["id"])["actor"] == "agent:namako"


def test_admin_delete_preserves_proposal_receipt_and_audit(app_env, expense):
    from app.api import finance
    from app.db import repo_admin

    txn_id, split_id, sibling_id, target = expense
    with engine.write_tx(app_env) as conn:
        doc_id = conn.execute("INSERT INTO source_documents(kind,storage_ref,status) VALUES ('receipt','generated-admin-receipt','processed')").lastrowid
        conn.execute("UPDATE transactions SET source_document_id=? WHERE id=?", (doc_id, txn_id))
    body = dict(operation_key="admin:1", transaction_id=txn_id, split_id=split_id,
        to_category_id=target, expected_state_digest=finance.transaction_detail(txn_id)["state_digest"], rationale="Meal")
    p = finance.propose_category(**body)
    with engine.write_tx(app_env) as conn:
        result = repo_admin.delete_document(conn, doc_id)
    assert result["transactions_deleted"] == 1
    assert finance.proposal_detail(p["id"])["audit"] == p["audit"]
    assert finance.propose_category(**body)["id"] == p["id"]
    assert finance_client().post(f"/actions/{p['id']}/approve", follow_redirects=False).status_code == 400
