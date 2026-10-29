"""HTTP boundary tests use a generated isolated candidate only."""
import importlib
import json

from fastapi.testclient import TestClient


def factory(tmp_path):
    assert importlib.util.find_spec("app.plaid_sandbox.web"), "isolated HTTP candidate missing"
    mod = importlib.import_module("app.plaid_sandbox.web")
    return mod.create_app(tmp_path / "candidate", port=8772)


def test_normal_app_connections_uses_disabled_production_banking_without_sandbox(
    app_env, monkeypatch
):
    import httpx

    from app.banking import service as banking_service
    from app.config import get_settings
    from app.web.app import create_app

    monkeypatch.setenv('PLAID_ENABLED', 'false')
    monkeypatch.setenv('STRICT_LOCAL_MODE', 'true')
    monkeypatch.delenv('PLAID_PRIVATE_DIR', raising=False)
    monkeypatch.delenv('PLAID_CREDENTIALS_FILE', raising=False)
    origin = 'https://banking.example.invalid'
    monkeypatch.setenv('PLAID_ORIGIN', origin)
    monkeypatch.setenv('PLAID_COMPLETION_URI', origin + '/connections')
    get_settings.cache_clear()

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('disabled banking must not open a private store or egress')

    monkeypatch.setattr(banking_service, 'PrivateStore', forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    application = create_app()
    service = application.state.banking_service
    assert isinstance(service, banking_service.BankingService)
    assert service.enabled() is False
    assert service.strict_local() is True
    assert service.private_dir is None
    assert service.credentials_file is None
    assert service._store is None
    client = TestClient(application, base_url=origin)
    response = client.get('/connections')
    assert response.status_code == 200
    assert 'data-household-onboarding' in response.text
    assert 'Add household members locally' in response.text
    assert 'Enable Plaid in server settings' in response.text
    assert '/static/household-onboarding.js' in response.text
    assert 'sandbox-setup' not in response.text
    assert 'plaid-sandbox.js' not in response.text
    assert client.post('/api/sandbox/setup', json={}).status_code in (401,403,404,405,503)
    assert not any(getattr(route,'path','').startswith('/api/sandbox') for route in client.app.routes)

    overview = client.get('/connections/data/overview')
    assert overview.status_code == 200
    assert overview.json()['network_allowed'] is False
    # Reach the service through a valid browser session, not a middleware refusal.
    blocked = client.post(
        '/connections/data/link/start',
        json={'member_id': 1},
        headers={'Origin': origin, 'X-Banking-CSRF': overview.json()['csrf']},
    )
    assert blocked.status_code == 409
    assert blocked.json() == {'detail': 'egress_blocked'}
    assert service._store is None
    assert calls == []


def test_bad_http_payloads_never_echo_private_material(tmp_path):
    app = factory(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8772') as client:
        client.get('/connections')
        csrf=client.get('/api/sandbox/state').json()['csrf']
        headers={'Origin':'http://127.0.0.1:8772','X-CSRF-Token':csrf}
        for action,body in [('member',{'name':{'secret':'never-echo-generated'}}),
                            ('link',{'owner_id':['never-echo-generated'],'consent':'plaid-sandbox.v1'}),
                            ('sync',{'connection_id':{'secret':'never-echo-generated'}}),
                            ('mapping',{'connection_id':'a','account_id':'b','selected':True,'ownership':['never-echo-generated']})]:
            response=client.post('/api/sandbox/'+action,json=body,headers=headers)
            assert response.status_code in (400,409)
            assert 'never-echo-generated' not in response.text


def test_candidate_navigation_has_no_active_capture_or_model_controls(tmp_path):
    app = factory(tmp_path)
    with TestClient(app,base_url='http://127.0.0.1:8772') as client:
        page=client.get('/connections')
        assert 'data-capture-picker' not in page.text
        for route in ['/upload','/insights','/goals','/chat','/processing','/statements','/actions','/close','/recon','/review','/backlog','/categories','/manage']:
            response=client.get(route)
            assert response.status_code == 200
            assert 'unavailable in this isolated candidate' in response.text
        activity = client.get('/activity', follow_redirects=False)
        assert activity.status_code == 200
        assert 'location' not in activity.headers
        assert 'sandbox_activity' not in activity.text  # No template/debug internals.
        assert 'aria-current="page"' in activity.text


def test_runtime_private_handoff_requires_local_consent_without_echo(tmp_path):
    private=tmp_path/'private';private.mkdir(mode=0o700)
    handoff=private/'plaid-sandbox.json'
    handoff.write_text(json.dumps({'environment':'sandbox','client_id':'generated-client','secret':'generated-secret'}));handoff.chmod(0o600)
    mod=importlib.import_module('app.plaid_sandbox.web')
    app=mod.create_app(tmp_path/'candidate',port=8772,credentials_file=handoff)
    with TestClient(app,base_url='http://127.0.0.1:8772') as client:
        page=client.get('/connections')
        assert 'sandbox-file-setup' in page.text
        assert 'id="sandbox-secret"' not in page.text
        state=client.get('/api/sandbox/state').json()
        assert state['configured'] and not state['enabled']
        assert 'generated-secret' not in page.text+json.dumps(state)
        headers={'Origin':'http://127.0.0.1:8772','X-CSRF-Token':state['csrf']}
        assert client.post('/api/sandbox/enable-private-file',json={'consent':'old'},headers=headers).status_code==400
        assert client.post('/api/sandbox/enable-private-file',json={'consent':'plaid-sandbox.v1'},headers=headers).status_code==200
        assert client.get('/api/sandbox/state').json()['enabled']
        assert app.state.store.credentials()==('generated-client','generated-secret')
        assert client.post('/api/sandbox/setup',json={'client_id':'ignored-generated','secret':'ignored-generated','consent':'plaid-sandbox.v1'},headers=headers).status_code==400


def test_candidate_render_never_loads_operator_settings(tmp_path, monkeypatch):
    from app import config
    calls = []
    def forbidden():
        calls.append(True)
        raise RuntimeError("operator settings forbidden")
    monkeypatch.setattr(config, "get_settings", forbidden)
    app = factory(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        response = client.get("/connections")
        assert response.status_code == 200
        assert 'href="/connections"' in response.text
    assert calls == []


def test_http_member_link_mapping_sync_repair_and_revoke(tmp_path):
    import httpx
    from tests.test_plaid_sandbox_service import GeneratedPlaid, page, row
    provider = GeneratedPlaid()
    mod = importlib.import_module("app.plaid_sandbox.web")
    app = mod.create_app(tmp_path / "candidate", port=8772, transport=httpx.MockTransport(provider))
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        client.get("/connections")
        csrf = client.get("/api/sandbox/state").json()["csrf"]
        headers = {"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": csrf}
        def post(action, data):
            response = client.post("/api/sandbox/" + action, json=data, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()
        post("setup", {"client_id": "generated-client", "secret": "generated-secret", "consent": "plaid-sandbox.v1"})
        post("member", {"name": "Member Alpha"})
        owner = client.get("/api/sandbox/state").json()["members"][0]["id"]
        link = post("link", {"owner_id": owner, "consent": "plaid-sandbox.v1"})
        provider.completions[link["link_token"]] = "public-sandbox-one"
        connection = post("exchange", {"session_id": link["session_id"], "public_token": "public-sandbox-one", "owner_id": "forged", "institution": "forged"})["connection_id"]
        state = client.get("/api/sandbox/state").json()
        assert state["connections"][0]["owner_id"] == owner
        post("mapping", {"connection_id": connection, "account_id": "same-account-id", "selected": True, "ownership": "shared"})
        provider.pages = [page([row()])]
        post("sync", {"connection_id": connection})
        state = client.get("/api/sandbox/state?owner_id=" + owner).json()
        assert state["staged"][0]["amount_cents"] == -1234
        repair = post("link", {"owner_id": owner, "connection_id": connection, "consent": "plaid-sandbox.v1"})
        post("repair-complete", {"session_id": repair["session_id"]})
        post("disconnect", {"connection_id": connection})
        state = client.get("/api/sandbox/state").json()
        assert state["connections"][0]["status"] == "disconnected"
        assert all(value not in json.dumps(state) for value in ["generated-secret", "access-sandbox", "link-sandbox", "public-sandbox", "forged"])


def test_http_setup_requires_exact_origin_csrf_and_private_session(tmp_path):
    app = factory(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8772") as client:
        response = client.get("/connections")
        assert response.status_code == 200
        assert 'class="db-pill sandbox-badge">Sandbox · test data</span>' in response.text
        assert "link-initialize.js" not in response.text
        policy=response.headers['content-security-policy']
        assert "connect-src 'self' https://sandbox.plaid.com;" in policy
        assert 'frame-src https://cdn.plaid.com;' in policy
        assert 'production.plaid.com' not in policy
        assert "serviceWorker.register" not in response.text
        assert "HttpOnly" in response.headers["set-cookie"]
        state = client.get("/api/sandbox/state").json()
        csrf = state["csrf"]
        setup = {"client_id": "generated-client", "secret": "generated-secret", "consent": "plaid-sandbox.v1"}
        for headers in [{}, {"Origin": "http://evil.invalid", "X-CSRF-Token": csrf},
                        {"Origin": "http://127.0.0.1:8772.evil.invalid", "X-CSRF-Token": csrf},
                        {"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": "wrong"}]:
            assert client.post("/api/sandbox/setup", json=setup, headers=headers).status_code == 403
        headers = {"Origin": "http://127.0.0.1:8772", "X-CSRF-Token": csrf}
        assert client.post("/api/sandbox/setup", json=setup, headers=headers).status_code == 200
        for url in ["/connections", "/api/sandbox/state"]:
            response = client.get(url)
            assert response.headers["cache-control"] == "no-store"
            assert "generated-secret" not in response.text
            assert "generated-client" not in response.text
        assert client.post("/api/sandbox/member", json={"name": "Member Alpha"}, headers=headers).status_code == 200
        assert client.post("/api/sandbox/member", content=b"x" * 20000, headers=headers).status_code == 413
        assert client.post("/api/sandbox/setup", json={"secret": "private-never-echo"}, headers=headers).status_code == 400
        assert "private-never-echo" not in client.get("/connections").text
        assert client.get("/api/sandbox/state", headers={"Host": "evil.invalid"}).status_code == 403
        assert client.post("/api/transactions", json={}, headers=headers).status_code in (404, 405)
        assert client.get("/sw.js").status_code == 404
        assert client.post("/api/sandbox/disable", json={}, headers=headers).status_code == 200
        assert not client.get("/api/sandbox/state").json()["enabled"]
    # A session cookie is server-issued, not accepted from a caller-selected identifier.
    with TestClient(app, base_url="http://127.0.0.1:8772") as other:
        assert other.get("/api/sandbox/state").status_code == 403
        assert other.get("/connections").status_code == 200
        assert other.get("/api/sandbox/state").json()["csrf"] != csrf
