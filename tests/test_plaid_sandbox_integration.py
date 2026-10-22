"""Integration verification of the sandbox candidate against safety boundaries.

Exercises two-member isolation, repeat-sync idempotency, repair/failure/retry,
and all required safety invariants through the HTTP API with generated data.
No real credentials, tokens, or live API calls.
"""
import importlib
import json

import httpx
from fastapi.testclient import TestClient

from tests.test_plaid_sandbox_service import GeneratedPlaid, page, row


def factory(tmp_path, *, transport=None, credentials_file=None):
    mod = importlib.import_module("app.plaid_sandbox.web")
    return mod.create_app(tmp_path / "candidate", port=8772,
                          transport=transport, credentials_file=credentials_file)


def setup_and_member(client, headers, name="Member Alpha"):
    """Setup sandbox with dummy keys and add a household member."""
    client.post("/api/sandbox/setup", json={
        "client_id": "generated-client", "secret": "generated-secret",
        "consent": "plaid-sandbox.v1"}, headers=headers)
    client.post("/api/sandbox/member", json={"name": name}, headers=headers)
    state = client.get("/api/sandbox/state").json()
    return state["members"][-1]["id"]


def link_and_exchange(client, headers, provider, owner_id, suffix="one",
                      connection_id=None):
    """Begin Link, verify completion, exchange for a connection."""
    link = client.post("/api/sandbox/link", json={
        "owner_id": owner_id, "consent": "plaid-sandbox.v1",
        "connection_id": connection_id}, headers=headers).json()
    provider.completions[link["link_token"]] = "public-sandbox-" + suffix
    resp = client.post("/api/sandbox/exchange", json={
        "session_id": link["session_id"],
        "public_token": "public-sandbox-" + suffix}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["connection_id"]


# ── Two-member account/transaction isolation ──────────────────────────────

def test_two_member_isolation_at_http_level(tmp_path):
    """Two members get separate connections; owner-filter excludes the other."""
    provider = GeneratedPlaid()
    app = factory(tmp_path, transport=httpx.MockTransport(provider))
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        headers = {"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": csrf}

        alpha = setup_and_member(client, headers, "Member Alpha")
        beta = setup_and_member(client, headers, "Member Beta")

        conn_a = link_and_exchange(client, headers, provider, alpha, "alpha")
        conn_b = link_and_exchange(client, headers, provider, beta, "beta")

        # Map accounts for both connections
        for conn in (conn_a, conn_b):
            client.post("/api/sandbox/mapping", json={
                "connection_id": conn, "account_id": "same-account-id",
                "selected": True, "ownership": "shared"}, headers=headers)

        # Sync both with distinct synthetic data
        provider.pages = [page([row("tx-alpha", 10.00)], cursor="alpha-c")]
        client.post("/api/sandbox/sync", json={"connection_id": conn_a}, headers=headers)
        provider.pages = [page([row("tx-beta", 20.00)], cursor="beta-c")]
        client.post("/api/sandbox/sync", json={"connection_id": conn_b}, headers=headers)

        # No-owner filter: sees both connections and both staged rows
        all_state = client.get("/api/sandbox/state").json()
        assert len(all_state["connections"]) == 2
        assert len(all_state["staged"]) == 2
        assert {c["owner_id"] for c in all_state["connections"]} == {alpha, beta}

        # Alpha-only filter: sees only Alpha's connection and staged row
        alpha_state = client.get(f"/api/sandbox/state?owner_id={alpha}").json()
        assert len(alpha_state["connections"]) == 1
        assert alpha_state["connections"][0]["owner_id"] == alpha
        assert len(alpha_state["staged"]) == 1
        assert alpha_state["staged"][0]["transaction_id"] == "tx-alpha"

        # Beta-only filter: sees only Beta's connection and staged row
        beta_state = client.get(f"/api/sandbox/state?owner_id={beta}").json()
        assert len(beta_state["connections"]) == 1
        assert beta_state["connections"][0]["owner_id"] == beta
        assert len(beta_state["staged"]) == 1
        assert beta_state["staged"][0]["transaction_id"] == "tx-beta"

        # No credential or token leakage in any state response
        for s in (all_state, alpha_state, beta_state):
            dumped = json.dumps(s)
            for secret in ("generated-secret", "access-sandbox", "link-sandbox",
                           "public-sandbox"):
                assert secret not in dumped


# ── Repeat sync without duplication ───────────────────────────────────────

def test_repeat_sync_without_duplication_at_http_level(tmp_path):
    """Syncing the same cursor twice does not duplicate staged observations."""
    provider = GeneratedPlaid()
    app = factory(tmp_path, transport=httpx.MockTransport(provider))
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        headers = {"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": csrf}

        owner = setup_and_member(client, headers)
        conn = link_and_exchange(client, headers, provider, owner, "sync")
        client.post("/api/sandbox/mapping", json={
            "connection_id": conn, "account_id": "same-account-id",
            "selected": True, "ownership": "shared"}, headers=headers)

        # First sync: one transaction
        provider.pages = [page([row("tx-dedup", 12.34)], cursor="first")]
        client.post("/api/sandbox/sync", json={"connection_id": conn}, headers=headers)
        state1 = client.get("/api/sandbox/state").json()
        assert len(state1["staged"]) == 1
        assert state1["staged"][0]["transaction_id"] == "tx-dedup"

        # Second sync with same data: observations are deduplicated by digest
        provider.pages = [page([row("tx-dedup", 12.34)], cursor="first")]
        client.post("/api/sandbox/sync", json={"connection_id": conn}, headers=headers)
        state2 = client.get("/api/sandbox/state").json()
        assert len(state2["staged"]) == 1  # not 2 — no duplication
        assert state2["staged"][0]["transaction_id"] == "tx-dedup"

        # Third sync with new cursor and new transaction: adds one
        provider.pages = [page([row("tx-new", 56.78)], cursor="second")]
        client.post("/api/sandbox/sync", json={"connection_id": conn}, headers=headers)
        state3 = client.get("/api/sandbox/state").json()
        assert len(state3["staged"]) == 2
        tx_ids = {r["transaction_id"] for r in state3["staged"]}
        assert tx_ids == {"tx-dedup", "tx-new"}


# ── Repair behavior and failure/retry states ───────────────────────────────

def test_repair_and_failure_retry_states_at_http_level(tmp_path):
    """Repair reuses existing Item; exchange failure is durable and not replayed."""
    provider = GeneratedPlaid()
    app = factory(tmp_path, transport=httpx.MockTransport(provider))
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        headers = {"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": csrf}

        owner = setup_and_member(client, headers)
        conn = link_and_exchange(client, headers, provider, owner, "repair")

        # Start in the failure state repair must actually clear, not already connected.
        app.state.store.connection_error(conn, "ITEM_LOGIN_REQUIRED", "generated-bank-refresh")
        state = client.get("/api/sandbox/state").json()
        original_connection = state["connections"][0]
        assert original_connection["status"] == "needs_repair"
        assert original_connection["error"] == "ITEM_LOGIN_REQUIRED"

        # Begin repair Link for the existing connection
        repair = client.post("/api/sandbox/link", json={
            "owner_id": owner, "consent": "plaid-sandbox.v1",
            "connection_id": conn}, headers=headers).json()
        assert repair["session_id"]
        repair_request = provider.calls[-1]
        assert repair_request[0] == "/link/token/create"
        assert repair_request[1]["access_token"] == "access-sandbox-1"
        assert "products" not in repair_request[1]

        # Complete repair — reuses existing item, no new item created
        before_items = provider.item
        before_repair_calls = len(provider.calls)
        repaired = client.post("/api/sandbox/repair-complete", json={
            "session_id": repair["session_id"]}, headers=headers)
        assert repaired.status_code == 200, repaired.text
        after_state = client.get("/api/sandbox/state").json()
        assert after_state["connections"][0]["status"] == "connected"
        assert after_state["connections"][0]["error"] is None
        assert after_state["connections"][0]["id"] == original_connection["id"]
        assert after_state["connections"][0]["item_id"] == original_connection["item_id"]
        repair_attempt = next(a for a in after_state["link_attempts"] if a["id"] == repair["session_id"])
        assert repair_attempt["status"] == "complete"
        assert [path for path, _ in provider.calls[before_repair_calls:]] == ["/item/get", "/accounts/get"]
        with app.state.store.tx() as db:
            assert db.execute("SELECT count(*) FROM audit WHERE event='item_repaired' AND target=?", (conn,)).fetchone()[0] == 1
        # Provider item count unchanged (no new item created in repair)
        assert provider.item == before_items

        # Failure/retry: simulate uncertain exchange with a second member
        beta = setup_and_member(client, headers, "Member Beta")
        fail_link = client.post("/api/sandbox/link", json={
            "owner_id": beta, "consent": "plaid-sandbox.v1"}, headers=headers).json()
        provider.completions[fail_link["link_token"]] = "public-sandbox-fail"
        provider.fail_exchange = True

        # First attempt fails with exchange_uncertain
        exchange_calls_initial = sum(
            1 for path, _ in provider.calls
            if path == "/item/public_token/exchange")
        resp = client.post("/api/sandbox/exchange", json={
            "session_id": fail_link["session_id"],
            "public_token": "public-sandbox-fail"}, headers=headers)
        assert resp.status_code == 409
        assert "exchange_uncertain" in resp.text

        # Second attempt also fails — exchange is not replayed
        exchange_calls_before = sum(
            1 for path, _ in provider.calls
            if path == "/item/public_token/exchange")
        assert exchange_calls_before == exchange_calls_initial + 1
        resp2 = client.post("/api/sandbox/exchange", json={
            "session_id": fail_link["session_id"],
            "public_token": "public-sandbox-fail"}, headers=headers)
        assert resp2.status_code == 409
        exchange_calls_after = sum(
            1 for path, _ in provider.calls
            if path == "/item/public_token/exchange")
        # An uncertain exchange is never dispatched again, even once.
        assert exchange_calls_after == exchange_calls_before

        # The uncertain attempt is durable in state
        state = client.get("/api/sandbox/state").json()
        uncertain = [a for a in state["link_attempts"] if a["status"] == "uncertain"]
        assert len(uncertain) >= 1
        # No tokens leaked in state
        assert "public-sandbox" not in json.dumps(state)


# ── Safety boundary: missing-config rejection with zero calls ──────────────

def test_missing_config_rejects_with_zero_provider_calls(tmp_path):
    """When sandbox is not configured, no provider call is made."""
    seen = []
    def transport(request):
        seen.append(request)
        return httpx.Response(200, json={})
    app = factory(tmp_path, transport=httpx.MockTransport(transport))
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        headers = {"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": csrf}

        # Add only a local member; never configure credentials or enable networking.
        added = client.post("/api/sandbox/member", json={"name": "Member Alpha"}, headers=headers)
        assert added.status_code == 200
        state = client.get("/api/sandbox/state").json()
        assert state["configured"] is False
        assert state["enabled"] is False
        member = state["members"][0]["id"]
        assert seen == []

        resp = client.post("/api/sandbox/link", json={
            "owner_id": member, "consent": "plaid-sandbox.v1"}, headers=headers)
        assert resp.status_code == 409
        assert len(seen) == 0  # zero provider calls on refusal


# ── Safety boundary: host rejection ─────────────────────────────────────────

def test_host_rejection_at_http_level(tmp_path):
    """Non-loopback host header is rejected with 403."""
    app = factory(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        # Normal request works
        assert client.get("/connections").status_code == 200
        # Forged host header is rejected
        resp = client.get("/api/sandbox/state", headers={"Host": "evil.invalid"})
        assert resp.status_code == 403
        resp2 = client.get("/api/sandbox/state",
                          headers={"Host": "127.0.0.1:9999"})
        assert resp2.status_code == 403


# ── Safety boundary: non-sandbox token rejection with zero calls ────────────

def test_non_sandbox_token_rejected_with_zero_calls(tmp_path):
    """Production or development tokens are rejected before any network call."""
    from app.plaid_sandbox.client import SandboxClient, SandboxError
    seen = []
    transport = httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, json={}))
    client = SandboxClient(enabled=True, credentials=("generated-client", "generated-secret"),
                           transport=transport)
    for token_kind, value in [
        ("access", "access-production-generated"),
        ("public", "public-development-generated"),
        ("link", "link-production-generated"),
    ]:
        with pytest.raises(SandboxError):
            if token_kind == "access":
                client.call("/accounts/get", access_token=value)
            elif token_kind == "public":
                client.call("/item/public_token/exchange", public_token=value)
            else:
                client.call("/link/token/get", link_token=value)
    assert len(seen) == 0  # zero calls on refusal

import pytest  # noqa: E402
