"""Generated timeout recovery preserves one-shot exchange evidence."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.plaid_sandbox.client import SandboxError
from app.plaid_sandbox.web import COOKIE, create_app
from tests.test_plaid_sandbox_service import GeneratedPlaid, connected, page, row

LINK_CONSENT = "plaid-sandbox.v1"
RECOVERY_CONSENT = "plaid-sandbox-recovery.v1"


@pytest.fixture
def recovery(tmp_path):
    provider = GeneratedPlaid()
    app = create_app(tmp_path / "generated-recovery", transport=httpx.MockTransport(provider))
    store, service = app.state.store, app.state.service
    store.configure("generated-client", "generated-secret", LINK_CONSENT)
    connection, owner = connected(service, store)
    provider.pages = [page([row()])]
    service.sync(connection, "generated-fixture")
    attempt = service.begin("original-browser", owner, consent=LINK_CONSENT)
    public = provider.completions[attempt["link_token"]]
    provider.fail_exchange = True
    with pytest.raises(SandboxError, match="exchange_uncertain_do_not_replay"):
        service.exchange("original-browser", attempt["session_id"], public)
    provider.fail_exchange = False
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/overview").json()["csrf"]
        client.headers.update({"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": csrf})
        yield client, store, service, provider, owner, connection, attempt["session_id"], public


def test_timeout_blocks_new_link_server_side_but_allows_known_item_repair(recovery):
    client, _, service, provider, owner, connection, _, _ = recovery
    before = len(provider.calls)
    response = client.post("/api/sandbox/link", json={"owner_id": owner, "consent": LINK_CONSENT})
    assert response.status_code == 409
    assert response.json() == {"error": "exchange_uncertain_do_not_replay"}
    with pytest.raises(SandboxError, match="exchange_uncertain_do_not_replay"):
        service.begin("another-browser", owner, consent=LINK_CONSENT)
    assert len(provider.calls) == before
    repair = service.begin("another-browser", owner, consent=LINK_CONSENT, connection_id=connection)
    assert repair["session_id"]
    assert provider.calls[-1][1]["access_token"].startswith("access-sandbox-")


def test_acknowledgement_is_content_free_idempotent_and_never_replays(recovery, monkeypatch):
    client, store, service, provider, owner, _, session, public = recovery
    def evidence():
        with store.tx() as db:
            return {table: [tuple(r) for r in db.execute("SELECT * FROM " + table)]
                    for table in ("link_sessions", "connections", "accounts", "observations", "staged")}
    before, calls = evidence(), len(provider.calls)
    with store.tx() as db:
        audit_before = [tuple(r) for r in db.execute("SELECT * FROM audit ORDER BY id")]
    def forbidden():
        raise AssertionError("acknowledgement must not load credentials")
    with monkeypatch.context() as patch:
        patch.setattr(store, "credentials", forbidden)
        for _ in range(2):
            response = client.post("/api/sandbox/acknowledge-exchange", json={
                "session_id": session, "consent": RECOVERY_CONSENT, "actor": "untrusted-actor"})
            assert response.status_code == 200
    assert len(provider.calls) == calls
    assert evidence() == before
    with store.tx() as db:
        audits = [tuple(r) for r in db.execute("SELECT * FROM audit ORDER BY id")]
    assert audits[:-1] == audit_before
    event = audits[-1]
    assert event[1:4] == ("exchange_acknowledged", client.cookies.get(COOKIE), session)
    assert event[4]
    client.get("/connections")  # Reload reads retained disposition, not transient UI state.
    for endpoint in ("state", "overview"):
        data = client.get("/api/sandbox/" + endpoint).json()
        attempt = next(a for a in data["link_attempts"] if a["id"] == session)
        assert attempt == {"id": session, "owner_id": owner, "status": "uncertain",
                           "connection_id": None, "acknowledged_at": event[4]}
    with pytest.raises(SandboxError, match="exchange_uncertain_do_not_replay"):
        service.exchange("original-browser", session, public)
    assert len(provider.calls) == calls
    for consent in (None, "plaid-sandbox.v0", RECOVERY_CONSENT):
        payload = {"owner_id": owner}
        if consent is not None:
            payload["consent"] = consent
        assert client.post("/api/sandbox/link", json=payload).status_code in {400, 409}
    assert len(provider.calls) == calls
    fresh = client.post("/api/sandbox/link", json={"owner_id": owner, "consent": LINK_CONSENT})
    assert fresh.status_code == 200
    assert fresh.json()["session_id"] != session
    assert len(provider.calls) == calls + 1
    assert store.link(session)["status"] == "uncertain"


@pytest.mark.parametrize("consent", [None, "", LINK_CONSENT, "plaid-sandbox-recovery.v0"])
def test_recovery_requires_distinct_current_consent(recovery, consent):
    client, store, _, provider, _, _, session, _ = recovery
    payload = {"session_id": session}
    if consent is not None:
        payload["consent"] = consent
    calls = len(provider.calls)
    assert client.post("/api/sandbox/acknowledge-exchange", json=payload).status_code == 400
    assert not next(a for a in store.snapshot()["link_attempts"] if a["id"] == session)["acknowledged_at"]
    assert len(provider.calls) == calls


@pytest.mark.parametrize("state", ["missing", "ready", "complete", "repair"])
def test_recovery_rejects_unknown_or_ineligible_attempt(recovery, state):
    client, store, _, provider, owner, connection, _, _ = recovery
    if state == "missing":
        session = "unknown-generated-session"
    elif state == "complete":
        session = next(a["id"] for a in store.snapshot()["link_attempts"] if a["status"] == "complete")
    else:
        session = store.new_link("original-browser", owner, "link-sandbox-ineligible",
                                 connection if state == "repair" else None)
        if state == "repair":
            store.claim_exchange(session, "generated-digest")
    calls = len(provider.calls)
    assert client.post("/api/sandbox/acknowledge-exchange", json={
        "session_id": session, "consent": RECOVERY_CONSENT}).status_code == 400
    with store.tx() as db:
        assert not db.execute("SELECT 1 FROM audit WHERE event='exchange_acknowledged'").fetchone()
    assert len(provider.calls) == calls


def test_recovery_keeps_origin_csrf_and_session_boundaries(recovery):
    client, store, _, provider, _, _, session, _ = recovery
    payload = {"session_id": session, "consent": RECOVERY_CONSENT}
    calls = len(provider.calls)
    for headers in ({"Origin": "http://generated-evil.invalid"}, {"X-CSRF-Token": "stale-generated-csrf"},
                    {"Sec-Fetch-Site": "cross-site"}):
        assert client.post("/api/sandbox/acknowledge-exchange", json=payload, headers=headers).status_code == 403
    with TestClient(client.app, base_url="http://127.0.0.1:8772") as stranger:
        assert stranger.post("/api/sandbox/acknowledge-exchange", json=payload).status_code == 403
    assert len(provider.calls) == calls
    with store.tx() as db:
        assert not db.execute("SELECT 1 FROM audit WHERE event='exchange_acknowledged'").fetchone()


def test_concurrent_acknowledgements_append_only_once_and_apply_to_exact_attempt(recovery):
    from concurrent.futures import ThreadPoolExecutor
    client, store, service, provider, owner, _, session, _ = recovery
    other = store.new_link("original-browser", owner, "link-sandbox-other")
    store.claim_exchange(other, "generated-other-digest")
    calls = len(provider.calls)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: store.acknowledge_exchange(session, RECOVERY_CONSENT,
                                                          client.cookies.get(COOKIE)), range(4)))
    with store.tx() as db:
        events = db.execute("SELECT target FROM audit WHERE event='exchange_acknowledged'").fetchall()
    assert [r[0] for r in events] == [session]
    with pytest.raises(SandboxError, match="exchange_uncertain_do_not_replay"):
        service.begin("another-browser", owner, consent=LINK_CONSENT)
    assert len(provider.calls) == calls
