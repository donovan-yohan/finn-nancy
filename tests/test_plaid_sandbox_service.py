"""Generated Plaid protocol responses, never live Sandbox evidence."""
import importlib
import json

import httpx
import pytest

from app.plaid_sandbox.client import SandboxError
from app.plaid_sandbox.store import Store


class GeneratedPlaid:
    def __init__(self):
        self.calls = []
        self.item = 0
        self.pages = []
        self.fail_exchange = False
        self.completions = {}
        self.link_ordinal = 0

    def __call__(self, request):
        fields = json.loads(request.content)
        self.calls.append((request.url.path, fields))
        path = request.url.path
        if path == "/link/token/create":
            self.link_ordinal += 1
            token = f"link-sandbox-{self.link_ordinal}"
            self.completions[token] = f"public-sandbox-callback-{self.link_ordinal}"
            return httpx.Response(200, json={"link_token": token})
        if path == "/link/token/get":
            token = fields["link_token"]
            public = self.completions.get(token)
            return httpx.Response(200, json={"link_token": token, "link_sessions": [
                {"finished_at": "2026-01-02T00:00:00Z", "results": {"item_add_results": [{"public_token": public}]}}] if public else []})
        if path == "/item/public_token/exchange":
            if self.fail_exchange:
                raise httpx.ReadTimeout("generated secret body", request=request)
            self.item += 1
            return httpx.Response(200, json={"item_id": f"item-{self.item}", "access_token": f"access-sandbox-{self.item}"})
        if path == "/accounts/get":
            item = fields["access_token"].rsplit("-", 1)[-1]
            return httpx.Response(200, json={"item": {"item_id": f"item-{item}", "institution_id": "ins_generated"}, "accounts": [
                {"account_id": "same-account-id", "name": "Example Checking", "mask": "0000", "type": "depository", "subtype": "checking"},
            ]})
        if path == "/item/get":
            return httpx.Response(200, json={"item": {"item_id": "item-" + fields["access_token"].rsplit("-",1)[-1], "error": None}, "status": {"transactions": {"last_successful_update": "2026-01-02T00:00:00Z"}}})
        if path == "/transactions/sync":
            page = self.pages.pop(0)
            return httpx.Response(400 if "error_code" in page else 200, json=page)
        if path == "/item/remove":
            return httpx.Response(200, json={"request_id": "generated"})
        raise AssertionError(path)


def service(tmp_path):
    assert importlib.util.find_spec("app.plaid_sandbox.service"), "member-bound Link service missing"
    mod = importlib.import_module("app.plaid_sandbox.service")
    store = Store(tmp_path / "candidate")
    store.configure("generated-client", "generated-secret", "plaid-sandbox.v1")
    provider = GeneratedPlaid()
    return mod.Service(store, transport=httpx.MockTransport(provider)), store, provider


def page(added=(), modified=(), removed=(), cursor="end", more=False):
    return {"added": list(added), "modified": list(modified), "removed": list(removed), "next_cursor": cursor, "has_more": more}


def row(transaction_id="tx-generated", amount=12.34, pending=False, **extra):
    return {"transaction_id": transaction_id, "account_id": "same-account-id", "amount": amount,
            "iso_currency_code": "CAD", "date": "2026-01-02", "name": "Example shop", "pending": pending, **extra}


def connected(svc, store, owner="Member Alpha", suffix="one"):
    member = store.add_member(owner, "browser-a")
    link = svc.begin("browser-a", member, consent="plaid-sandbox.v1")
    provider = svc._transport.handler
    provider.completions[link["link_token"]] = "public-sandbox-" + suffix
    connection = svc.exchange("browser-a", link["session_id"], "public-sandbox-" + suffix)
    store.map_account(connection, "same-account-id", selected=True, ownership=member, actor="browser-a")
    return connection, member


def test_sync_commits_staged_observations_and_cursor_without_ledger_effects(tmp_path):
    svc, store, provider = service(tmp_path)
    connection, member = connected(svc, store)
    provider.pages = [page([row(pending=True), row("income-generated", -20.01)], cursor="first")]
    svc.sync(connection, "browser-a")
    state = store.snapshot(member)
    assert len(state["staged"]) == 2
    assert [r["amount_cents"] for r in state["staged"]] == [-1234, 2001]
    assert all(r["flow_kind"] == "unknown" and r["booked"] is False for r in state["staged"])
    assert state["staged"][0]["state"] == "pending"
    assert state["connections"][0]["last_bank_update"] == "2026-01-02T00:00:00Z"
    assert state["connections"][0]["last_local_sync"] != state["connections"][0]["last_bank_update"]
    assert store.connection(connection)["cursor"] == "first"
    provider.pages = [page([row(pending=True), row("income-generated", -20.01)], cursor="first")]
    svc.sync(connection, "browser-a")
    assert len(store.snapshot()["staged"]) == 2
    with store.tx() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not tables & {"transactions", "transaction_splits", "statement_rows", "period_closes"}
    other, _ = connected(svc, store, "Member Beta", "two")
    provider.pages = [page([row()], cursor="other")]
    svc.sync(other, "browser-a")
    assert len(store.snapshot()["staged"]) == 3
    assert len(store.snapshot(member)["staged"]) == 2


def test_sync_pending_replacement_modification_and_removal_retain_evidence(tmp_path):
    svc, store, provider = service(tmp_path)
    connection, _ = connected(svc, store)
    provider.pages = [page([row("pending-a", pending=True)], cursor="one")]
    svc.sync(connection, "browser-a")
    provider.pages = [page([row("posted-a", 14.21, pending_transaction_id="pending-a")],
        removed=[{"transaction_id": "pending-a", "account_id": "same-account-id"}], cursor="two")]
    svc.sync(connection, "browser-a")
    rows = {r["transaction_id"]: r for r in store.snapshot()["staged"]}
    assert rows["pending-a"]["state"] == "superseded"
    assert rows["posted-a"]["state"] == "posted"
    provider.pages = [page(modified=[row("posted-a", 15.22)], cursor="three")]
    svc.sync(connection, "browser-a")
    assert store.snapshot()["staged"][1]["amount_cents"] == -1522
    provider.pages = [page(removed=[{"transaction_id": "posted-a"}], cursor="four")]
    svc.sync(connection, "browser-a")
    assert store.snapshot()["staged"][1]["state"] == "removed"
    with store.tx() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 5


def test_paginated_sync_restarts_from_original_cursor_and_rolls_back_bad_windows(tmp_path):
    svc, store, provider = service(tmp_path)
    connection, _ = connected(svc, store)
    provider.pages = [page([row("discarded")], cursor="page-1", more=True),
        {"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"},
        page([row("kept-a")], cursor="page-2", more=True), page([row("kept-b")], cursor="final")]
    svc.sync(connection, "browser-a")
    assert [fields["cursor"] for path, fields in provider.calls if path == "/transactions/sync"] == ["", "page-1", "", "page-2"]
    assert {r["transaction_id"] for r in store.snapshot()["staged"]} == {"kept-a", "kept-b"}
    provider.pages = [page([row("rollback-a"), row("bad-precision", 0.001)], cursor="bad")]
    with pytest.raises((SandboxError, ValueError)):
        svc.sync(connection, "browser-a")
    assert store.connection(connection)["cursor"] == "final"
    assert {r["transaction_id"] for r in store.snapshot()["staged"]} == {"kept-a", "kept-b"}
    with store.tx() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    # A provider failure mid-window must never commit the first page/cursor.
    provider.pages = [page([row("partial")], cursor="partial", more=True), {"error_code": "INVALID_ACCESS_TOKEN"}]
    with pytest.raises(SandboxError):
        svc.sync(connection, "browser-a")
    assert store.connection(connection)["cursor"] == "final"
    assert len(store.snapshot()["staged"]) == 2
    assert store.snapshot()["connections"][0]["error"] == "INVALID_ACCESS_TOKEN"


def test_update_mode_reuses_item_and_disconnect_retains_local_audit(tmp_path):
    svc, store, provider = service(tmp_path)
    connection, member = connected(svc, store)
    repair = svc.begin("browser-a", member, consent="plaid-sandbox.v1", connection_id=connection)
    request = provider.calls[-1][1]
    assert request["access_token"] == "access-sandbox-1"
    assert "products" not in request
    with pytest.raises(SandboxError):
        svc.finish_repair("browser-other", repair["session_id"])
    svc.finish_repair("browser-a", repair["session_id"])
    assert provider.item == 1
    assert len(store.snapshot()["connections"]) == 1
    assert store.connection(connection)["status"] == "connected"
    store.revocation_state(connection,'browser-a')
    with pytest.raises(SandboxError):
        svc.finish_repair('browser-a',repair['session_id'])
    svc.disconnect(connection, "browser-a")
    assert store.connection(connection)["access_token"] is None
    assert store.snapshot()["connections"][0]["status"] == "disconnected"
    calls = len(provider.calls)
    with pytest.raises(SandboxError):
        svc.sync(connection, "browser-a")
    assert len(provider.calls) == calls
    with store.tx() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='item_disconnected'").fetchone()[0] == 1


def test_sync_rejects_unknown_accounts_and_survives_cursor_commit_crash(tmp_path):
    import sqlite3
    svc, store, provider = service(tmp_path)
    connection, _ = connected(svc, store)
    provider.pages = [page([row(account_id="not-an-authenticated-account")], cursor="unknown")]
    with pytest.raises(SandboxError):
        svc.sync(connection, "browser-a")
    assert store.connection(connection)["cursor"] == ""
    with store.tx() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        db.execute("CREATE TRIGGER simulated_crash BEFORE UPDATE OF cursor ON connections BEGIN SELECT RAISE(ABORT,'generated crash'); END")
    provider.pages = [page([row()], cursor="crash")]
    with pytest.raises(SandboxError):
        svc.sync(connection, "browser-a")
    assert store.connection(connection)["cursor"] == ""
    assert not store.snapshot()["staged"]
    with store.tx() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        db.execute("DROP TRIGGER simulated_crash")
    provider.pages = [page([row()], cursor="recovered")]
    svc.sync(connection, "browser-a")
    # A process restart observes one complete committed window, never half a cursor.
    recovered = Store(store.root)
    assert recovered.connection(connection)["cursor"] == "recovered"
    assert len(recovered.snapshot()["staged"]) == 1


def test_exchange_requires_provider_verified_completion_of_exact_link(tmp_path):
    svc, store, provider = service(tmp_path)
    alpha = store.add_member('Member Alpha','browser-a')
    beta = store.add_member('Member Beta','browser-a')
    a = svc.begin('browser-a',alpha,consent='plaid-sandbox.v1')
    b = svc.begin('browser-a',beta,consent='plaid-sandbox.v1')
    provider.completions[a['link_token']] = 'public-sandbox-alpha'
    provider.completions[b['link_token']] = 'public-sandbox-beta'
    for token in ['public-sandbox-beta','public-sandbox-unrelated']:
        with pytest.raises(SandboxError,match='link_completion_unverified'):
            svc.exchange('browser-a',a['session_id'],token)
    assert provider.item == 0
    assert store.link(a['session_id'])['status'] == 'ready'
    assert store.snapshot()['connections'] == []
    provider.completions[a['link_token']] = None
    with pytest.raises(SandboxError,match='link_completion_unverified'):
        svc.exchange('browser-a',a['session_id'],'public-sandbox-alpha')
    assert provider.item == 0
    provider.completions[a['link_token']] = 'public-sandbox-alpha'
    connection = svc.exchange('browser-a',a['session_id'],'public-sandbox-alpha')
    assert store.connection(connection)['owner_id'] == alpha


def test_exchange_accepts_current_provider_item_add_results_without_legacy_callback(tmp_path):
    svc, store, provider = service(tmp_path)
    member = store.add_member('Member Alpha', 'browser-a')
    link = svc.begin('browser-a', member, consent='plaid-sandbox.v1')
    def transport(request):
        if request.url.path == '/link/token/get':
            return httpx.Response(200, json={
                'link_token': link['link_token'], 'link_sessions': [{
                    'finished_at': '2026-01-02T00:00:00Z',
                    'results': {'item_add_results': [{'public_token': 'public-sandbox-current'}]},
                }]})
        return provider(request)
    svc._transport = httpx.MockTransport(transport)
    with pytest.raises(SandboxError, match='link_completion_unverified'):
        svc.exchange('browser-a', link['session_id'], 'public-sandbox-unrelated')
    assert provider.item == 0
    connection = svc.exchange('browser-a', link['session_id'], 'public-sandbox-current')
    assert store.connection(connection)['owner_id'] == member
    assert provider.item == 1


@pytest.mark.parametrize('completion', [
    {},
    {'link_token': 'link-sandbox-unrelated', 'link_sessions': []},
    {'link_sessions': None},
    {'link_sessions': [None, {'finished_at': None, 'results': {'item_add_results': [{'public_token': 'public-sandbox-current'}]}}]},
    {'link_sessions': [{'finished_at': '', 'results': {'item_add_results': [{'public_token': 'public-sandbox-current'}]}}]},
    {'link_sessions': [{'finished_at': '2026-01-02T00:00:00Z', 'results': None, 'on_success': {'public_token': 'public-sandbox-current'}}]},
    {'link_sessions': [{'finished_at': '2026-01-02T00:00:00Z', 'results': {'item_add_results': 'malformed'}}]},
    {'link_sessions': [{'finished_at': '2026-01-02T00:00:00Z', 'results': {'item_add_results': [None, {}]}}]},
])
def test_missing_or_malformed_completion_never_claims_or_exchanges(tmp_path, completion):
    svc, store, provider = service(tmp_path)
    owner = store.add_member('Member Alpha', 'browser-a')
    link = svc.begin('browser-a', owner, consent='plaid-sandbox.v1')
    def transport(request):
        if request.url.path == '/link/token/get':
            return httpx.Response(200, json={'link_token': link['link_token'], **completion})
        return provider(request)
    svc._transport = httpx.MockTransport(transport)
    with pytest.raises(SandboxError, match='link_completion_unverified'):
        svc.exchange('browser-a', link['session_id'], 'public-sandbox-current')
    assert provider.item == 0
    assert store.link(link['session_id'])['status'] == 'ready'
    assert not store.snapshot()['connections']


def test_documented_legacy_completion_fallback_remains_supported(tmp_path):
    svc, store, provider = service(tmp_path)
    owner = store.add_member('Member Alpha', 'browser-a')
    link = svc.begin('browser-a', owner, consent='plaid-sandbox.v1')
    def transport(request):
        if request.url.path == '/link/token/get':
            return httpx.Response(200, json={'link_token': link['link_token'], 'link_sessions': [{
                'finished_at': '2026-01-02T00:00:00Z', 'on_success': {'public_token': 'public-sandbox-legacy'}}]})
        return provider(request)
    svc._transport = httpx.MockTransport(transport)
    connection = svc.exchange('browser-a', link['session_id'], 'public-sandbox-legacy')
    assert store.connection(connection)['owner_id'] == owner


@pytest.mark.parametrize('failure',['lost_ack','local_commit'])
def test_revocation_uncertainty_survives_remote_success_and_reconciles_safely(tmp_path,failure):
    svc,store,provider=service(tmp_path)
    connection,_=connected(svc,store)
    provider.pages=[page([row()])]
    svc.sync(connection,'browser-a')
    removed=set()
    def transport(request):
        fields=json.loads(request.content)
        if request.url.path=='/item/get' and fields['access_token'] in removed:
            return httpx.Response(400,json={'error_code':'INVALID_ACCESS_TOKEN'})
        if request.url.path=='/item/remove':
            removed.add(fields['access_token'])
            if failure=='lost_ack':
                raise httpx.ReadTimeout('generated lost acknowledgement',request=request)
            return httpx.Response(200,json={})
        return provider(request)
    svc._transport=httpx.MockTransport(transport)
    if failure=='local_commit':
        with store.tx() as db:
            db.execute("CREATE TRIGGER generated_commit_failure BEFORE UPDATE OF access_token ON connections WHEN NEW.access_token IS NULL BEGIN SELECT RAISE(ABORT,'generated'); END")
    with pytest.raises(SandboxError,match='revocation_uncertain'):
        svc.disconnect(connection,'browser-a')
    assert store.connection(connection)['status']=='revocation_uncertain'
    with pytest.raises(SandboxError):
        svc.begin('browser-a',store.connection(connection)['owner_id'],consent='plaid-sandbox.v1',connection_id=connection)
    assert len(store.snapshot()['staged'])==1
    assert len(store.snapshot()['accounts'])==1
    with store.tx() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE event='revocation_started'").fetchone()[0]==1
        if failure=='local_commit':db.execute('DROP TRIGGER generated_commit_failure')
    result=svc.disconnect(connection,'browser-a')
    assert result=='unconfirmed'
    assert store.connection(connection)['status']=='revocation_unconfirmed'
    assert store.connection(connection)['access_token'] is None
    assert len(store.snapshot()['staged'])==1
    assert svc.disconnect(connection,'browser-a')=='unconfirmed'


def test_uncertain_exchange_is_durable_and_never_replayed(tmp_path):
    svc, store, provider = service(tmp_path)
    member = store.add_member("Member Alpha", "browser-a")
    link = svc.begin("browser-a", member, consent="plaid-sandbox.v1")
    provider.completions[link["link_token"]] = "public-sandbox-uncertain"
    provider.fail_exchange = True
    with pytest.raises(SandboxError, match="exchange_uncertain"):
        svc.exchange("browser-a", link["session_id"], "public-sandbox-uncertain")
    with pytest.raises(SandboxError, match="exchange_uncertain"):
        svc.exchange("browser-a", link["session_id"], "public-sandbox-uncertain")
    assert sum(path == "/item/public_token/exchange" for path, _ in provider.calls) == 1
    assert store.snapshot()["connections"] == []
    assert store.snapshot()["link_attempts"][0]["status"] == "uncertain"
    assert "public-sandbox" not in json.dumps(store.snapshot())


def test_link_sessions_bind_owner_server_side_and_deduplicate_success(tmp_path):
    svc, store, provider = service(tmp_path)
    first = store.add_member("Member Alpha", "browser-a")
    second = store.add_member("Member Beta", "browser-a")
    link = svc.begin("browser-a", first, consent="plaid-sandbox.v1")
    assert link["link_token"].startswith("link-sandbox-")
    provider.completions[link["link_token"]] = "public-sandbox-one"
    with pytest.raises(SandboxError, match="session_mismatch"):
        svc.exchange("browser-b", link["session_id"], "public-sandbox-one")
    assert provider.item == 0
    connection = svc.exchange("browser-a", link["session_id"], "public-sandbox-one")
    assert svc.exchange("browser-a", link["session_id"], "public-sandbox-one") == connection
    assert provider.item == 1
    with pytest.raises(SandboxError):
        svc.exchange("browser-a", link["session_id"], "public-sandbox-different")
    link2 = svc.begin("browser-a", second, consent="plaid-sandbox.v1")
    provider.completions[link2["link_token"]] = "public-sandbox-two"
    connection2 = svc.exchange("browser-a", link2["session_id"], "public-sandbox-two")
    snapshot = store.snapshot()
    assert len(snapshot["connections"]) == 2
    assert [c["owner_id"] for c in snapshot["connections"]] == [first, second]
    accounts = snapshot["accounts"]
    assert len(accounts) == 2
    assert accounts[0]["account_id"] == accounts[1]["account_id"]
    assert accounts[0]["connection_id"] != accounts[1]["connection_id"]
    assert all(a["ownership"] == "unassigned" and not a["selected"] for a in accounts)
    assert "access-sandbox" not in json.dumps(snapshot)
    store.map_account(connection, "same-account-id", selected=True, ownership="shared", actor="browser-a")
    assert store.snapshot()["accounts"][0]["ownership"] == "shared"
    assert [c["id"] for c in store.snapshot(first)["connections"]] == [connection]
    assert [c["id"] for c in store.snapshot(second)["connections"]] == [connection2]
