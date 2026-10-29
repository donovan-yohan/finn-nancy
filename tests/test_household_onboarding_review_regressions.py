"""Generated review regressions through real application/browser/service authority."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import engine


class ListenerScope:
    """Supply listener metadata TestClient otherwise invents from the URL/Host.

    A production ASGI server supplies this tuple from its actual local socket.
    Deliberately keep request headers untouched for spoofing controls.
    """

    def __init__(self, app, server=('127.0.0.1', 8080), scheme='http'):
        self.app, self.server, self.scheme = app, server, scheme

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            scope = dict(scope, server=self.server, scheme=self.scheme)
        await self.app(scope, receive, send)


@pytest.fixture
def local_app(empty_db, tmp_path, monkeypatch):
    from app import config
    from app.api import auth, authority, middleware
    from app.banking import secrets as private
    from app.banking.client import PlaidClient
    import httpx

    # Construct actual field defaults without consulting dotenv or inherited
    # operator settings. Only generated storage paths differ from first use.
    settings = config.Settings.model_construct(db_path=Path(empty_db), data_dir=tmp_path / 'data')
    assert settings.plaid_origin == settings.plaid_completion_uri == ''
    assert settings.plaid_enabled is False and settings.strict_local_mode is True
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    monkeypatch.setattr(auth, 'get_settings', lambda: settings)
    monkeypatch.setattr(authority, 'get_settings', lambda: settings)
    monkeypatch.setattr(middleware, 'get_settings', lambda: settings)

    def forbidden(*args, **kwargs):
        pytest.fail('local onboarding must not read private stores or dispatch provider traffic')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(PlaidClient, 'post', forbidden)
    monkeypatch.setattr(private, 'private_read', forbidden)
    monkeypatch.setattr(private.PrivateStore, '__init__', forbidden)
    from app.web import app as web_app
    monkeypatch.setattr(web_app, 'get_settings', lambda: settings)
    # TestClient is used without lifespan: no background jobs, polling or warmup.
    app = web_app.create_app()
    return app, settings, app.state.banking_service


def seed_account(db_path, member_id):
    """Existing generated provider observation; binding itself is strictly local."""
    with engine.write_tx(db_path) as conn:
        connection_id = conn.execute("""INSERT INTO bank_connections
            (member_id,item_id,token_ref,consent_version,state)
            VALUES (?,'generated-item','generated-ref','plaid-transactions-v1','active')""",
            (member_id,)).lastrowid
        conn.execute("""INSERT INTO bank_accounts
            (connection_id,provider_account_id,name,currency,kind)
            VALUES (?,'generated-account','Generated savings','CAD','savings')""", (connection_id,))
    return dict(connection_id=connection_id, provider_account_id='generated-account',
                account_id=None, included=True, ownership='personal', owner_member_id=member_id,
                role='primary', expected_revision=0, new_name='Generated local savings', new_kind='savings')


@pytest.mark.parametrize('server,origin', [
    (('127.0.0.1', 8080), 'http://127.0.0.1:8080'),
    (('::1', 8080), 'http://[::1]:8080'),
])
def test_default_create_app_local_add_rename_and_mapping_without_plaid(local_app, server, origin):
    app, settings, service = local_app
    # Installed TestClient misparses bracketed IPv6 URL authorities; keep its
    # cookie transport on IPv4 while supplying the independent listener tuple.
    client = TestClient(ListenerScope(app, server), base_url='http://127.0.0.1:8080')
    try:
        assert 'Add yourself' in client.get('/connections').text
        overview = client.get('/connections/data/overview').json()
        assert overview['members'] == [] and overview['setup']['state'] == 'disabled'
        assert overview['setup']['ready'] is overview['network_allowed'] is False
        headers = {'Origin': origin, 'X-Banking-CSRF': overview['csrf']}
        created = client.post('/connections/data/member', json={'name': 'Generated Avery'}, headers=headers)
        assert created.status_code == 200, created.json()
        member_id = created.json()['member_id']
        renamed = client.post(f'/connections/data/member/{member_id}',
                              json={'name': 'Generated Éloïse'}, headers=headers)
        assert renamed.status_code == 200, renamed.json()
        binding = seed_account(service.db_path, member_id)
        mapped = client.post('/connections/data/bind', json=binding, headers=headers)
        assert mapped.status_code == 200, mapped.json()
        overview = client.get('/connections/data/overview').json()
        assert overview['members'][0]['id'] == member_id
        assert overview['members'][0]['name'] == 'Generated Éloïse'
        assert overview['members'][0]['consented'] is False
        account = overview['connections'][0]['accounts'][0]
        assert account['account_id'] == mapped.json()['account_id']
        assert account['owner_member_id'] == member_id and account['revision'] == 1
        with engine.read_conn(service.db_path) as conn:
            assert conn.execute('SELECT count(*) FROM transactions').fetchone()[0] == 0
            assert conn.execute('SELECT count(*) FROM bank_consents').fetchone()[0] == 0
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert service.origin == '' and service._store is None
        assert settings.strict_local_mode is True and settings.plaid_enabled is False
    finally:
        client.close()


@pytest.mark.parametrize('operation', ['add', 'rename', 'bind'])
@pytest.mark.parametrize('case', [
    'wrong_origin', 'wrong_port', 'host_spoof', 'forwarded_spoof', 'nonloopback',
    'wildcard', 'hostname', 'missing_server', 'bad_port', 'bool_port', 'zero_port',
    'https_scope', 'missing_origin', 'invalid_csrf', 'missing_session',
])
def test_local_write_authority_fails_closed(local_app, operation, case):
    app, _, service = local_app
    member_id = service.add_member('Generated original')['member_id']
    payload = seed_account(service.db_path, member_id) if operation == 'bind' else {'name': 'Generated changed'}
    endpoint = {'add': '/connections/data/member', 'rename': f'/connections/data/member/{member_id}',
                'bind': '/connections/data/bind'}[operation]
    listener = ListenerScope(app)
    client = TestClient(listener, base_url='http://127.0.0.1:8080')
    try:
        csrf = client.get('/connections/data/overview').json()['csrf']
        headers = {'Origin': 'http://127.0.0.1:8080', 'X-Banking-CSRF': csrf}
        if case == 'wrong_origin':
            headers['Origin'] = 'https://evil.example.invalid'
        elif case == 'wrong_port':
            headers['Origin'] = 'http://127.0.0.1:8081'
        elif case == 'host_spoof':
            headers.update(Origin='http://evil.example.invalid:8080', Host='evil.example.invalid:8080')
        elif case == 'forwarded_spoof':
            headers.update(Origin='https://evil.example.invalid',
                           Forwarded='host=evil.example.invalid;proto=https',
                           **{'X-Forwarded-Host': 'evil.example.invalid', 'X-Forwarded-Proto': 'https'})
        elif case in {'nonloopback', 'wildcard', 'hostname'}:
            listener.server = ({'nonloopback': '192.0.2.1', 'wildcard': '0.0.0.0', 'hostname': 'localhost'}[case], 8080)
        elif case == 'missing_server':
            listener.server = None
        elif case in {'bad_port', 'bool_port', 'zero_port'}:
            listener.server = ('127.0.0.1', {'bad_port': 65536, 'bool_port': True, 'zero_port': 0}[case])
        elif case == 'https_scope':
            listener.scheme = 'https'
        elif case == 'missing_origin':
            del headers['Origin']
        elif case == 'invalid_csrf':
            headers['X-Banking-CSRF'] = 'invalid'
        elif case == 'missing_session':
            client.cookies.clear()
        response = client.post(endpoint, json=payload, headers=headers)
        assert response.status_code == 403, response.json()
        assert response.json()['detail'] == ('browser_session_required' if case in {
            'invalid_csrf', 'missing_session'} else 'trusted_origin_required')
        with engine.read_conn(service.db_path) as conn:
            assert conn.execute('SELECT count(*) FROM bank_bindings').fetchone()[0] == 0
            assert conn.execute('SELECT name FROM household_members WHERE id=?', (member_id,)).fetchone()[0] == 'Generated original'
            assert conn.execute('SELECT count(*) FROM household_members WHERE is_active=1').fetchone()[0] == 1
        assert service._store is None
    finally:
        client.close()


@pytest.mark.parametrize('operation', ['add', 'rename', 'bind'])
def test_default_local_writes_remain_read_only(local_app, operation):
    app, settings, service = local_app
    member_id = service.add_member('Generated original')['member_id']
    binding = seed_account(service.db_path, member_id)
    settings.read_only = True
    endpoint = {'add': '/connections/data/member', 'rename': f'/connections/data/member/{member_id}',
                'bind': '/connections/data/bind'}[operation]
    client = TestClient(ListenerScope(app), base_url='http://127.0.0.1:8080')
    try:
        csrf = client.get('/connections/data/overview').json()['csrf']
        response = client.post(endpoint, json=binding if operation == 'bind' else {'name': 'Generated changed'},
                               headers={'Origin': 'http://127.0.0.1:8080', 'X-Banking-CSRF': csrf})
        assert response.status_code == 409 and response.json()['detail'] == 'read_only'
        with engine.read_conn(service.db_path) as conn:
            assert conn.execute('SELECT count(*) FROM bank_bindings').fetchone()[0] == 0
            assert conn.execute('SELECT count(*) FROM accounts').fetchone()[0] == 0
            assert conn.execute('SELECT name FROM household_members WHERE id=?', (member_id,)).fetchone()[0] == 'Generated original'
    finally:
        client.close()


def test_configured_remote_origin_stays_exact_and_host_independent(local_app):
    app, _, service = local_app
    service.origin = 'https://banking.example.invalid'
    client = TestClient(ListenerScope(app), base_url=service.origin)
    try:
        csrf = client.get('/connections/data/overview').json()['csrf']
        headers = {'Origin': service.origin, 'X-Banking-CSRF': csrf}
        # A loopback fallback never expands an explicitly configured policy.
        refused = client.post('/connections/data/member', json={'name': 'Generated refused'},
                              headers=headers | {'Origin': 'http://127.0.0.1:8080'})
        assert refused.status_code == 403
        accepted = client.post('/connections/data/member', json={'name': 'Generated remote'},
                               headers=headers | {'Host': 'untrusted.example.invalid',
                               'Forwarded': 'host=untrusted.example.invalid;proto=http'})
        assert accepted.status_code == 200, accepted.json()
        assert service.overview()['members'][0]['name'] == 'Generated remote'
    finally:
        client.close()


@pytest.mark.parametrize('endpoint,payload', [
    ('consent', {'member_id': 1, 'granted': True, 'version': 'plaid-transactions-v1'}),
    ('link/start', {'member_id': 1}), ('link/finish', {'session_id': 'generated-session'}),
    ('accounts', {'connection_id': 1}), ('sync', {'connection_id': 1}),
])
def test_local_fallback_never_authorizes_provider_routes(local_app, endpoint, payload):
    app, _, service = local_app
    client = TestClient(ListenerScope(app), base_url='http://127.0.0.1:8080')
    try:
        csrf = client.get('/connections/data/overview').json()['csrf']
        response = client.post('/connections/data/' + endpoint, json=payload,
            headers={'Origin': 'http://127.0.0.1:8080', 'X-Banking-CSRF': csrf})
        assert response.status_code == 403 and response.json()['detail'] == 'trusted_origin_required'
        with engine.read_conn(service.db_path) as conn:
            assert conn.execute('SELECT count(*) FROM bank_consents').fetchone()[0] == 0
            assert conn.execute('SELECT count(*) FROM bank_link_sessions').fetchone()[0] == 0
    finally:
        client.close()


def test_default_local_api_bearer_authority_is_unchanged(local_app):
    app, settings, service = local_app
    settings.api_token = 'synthetic-review-key'
    client = TestClient(ListenerScope(app, server=None), base_url='http://127.0.0.1:8080')
    try:
        headers = {'Authorization': 'Bearer synthetic-review-key'}
        created = client.post('/connections/data/member', json={'name': 'Generated keyed'}, headers=headers)
        assert created.status_code == 200 and created.headers['x-fn-caller'] == 'api'
        member_id = created.json()['member_id']
        renamed = client.post(f'/connections/data/member/{member_id}', json={'name': 'Generated renamed'}, headers=headers)
        assert renamed.status_code == 200
        mapped = client.post('/connections/data/bind', json=seed_account(service.db_path, member_id), headers=headers)
        assert mapped.status_code == 200
        invalid = client.post('/connections/data/member', json={'name': 'Generated refused'},
                              headers={'Authorization': 'Bearer invalid'})
        assert invalid.status_code == 401
        blocked = client.post('/connections/data/link/start', json={'member_id': member_id}, headers=headers)
        assert blocked.status_code == 409 and blocked.json()['detail'] == 'egress_blocked'
        assert service._store is None
    finally:
        client.close()


@pytest.fixture
def link_service(household_db, tmp_path, monkeypatch):
    import json
    import httpx
    from app.banking.service import BankingService, CONSENT_VERSION

    def forbidden(*args, **kwargs):
        pytest.fail('regressions may only dispatch through generated MockTransport')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    credential = tmp_path / 'synthetic-credentials.json'
    credential.write_text(json.dumps({'client_id': 'synthetic-client', 'secret': 'synthetic-secret'}))
    credential.chmod(0o600)
    calls = []

    def provider(request):
        calls.append(request.url.path)
        assert request.url.path == '/link/token/create'
        return httpx.Response(200, json={'link_token': 'generated-link',
            'hosted_link_url': 'https://secure.plaid.com/hl/generated', 'expiration': '2099-01-01T00:00:00Z'})

    service = BankingService(db_path=household_db, private_dir=tmp_path / 'private', credentials_file=credential,
        origin='https://banking.example.invalid', completion_uri='https://banking.example.invalid/connections',
        enabled=lambda: True, strict_local=lambda: False, read_only=lambda: False,
        transport=httpx.MockTransport(provider))
    service.consent(1, granted=True, version=CONSENT_VERSION)
    service.consent(2, granted=True, version=CONSENT_VERSION)
    ref = service.store.put('generated-access')
    with engine.write_tx(household_db) as conn:
        connection_id = conn.execute("""INSERT INTO bank_connections
            (member_id,item_id,token_ref,consent_version,state)
            VALUES (1,'generated-repair-item',?,'plaid-transactions-v1','needs_repair')""", (ref,)).lastrowid
    return service, calls, connection_id


def seed_link(service, *, state, expired=False, browser='generated-original-browser', connection_id=None):
    import hashlib
    import time
    ref = service.store.put('generated-existing-link')
    with engine.write_tx(service.db_path) as conn:
        consent_id = conn.execute('SELECT max(id) FROM bank_consents WHERE member_id=1').fetchone()[0]
        conn.execute("""INSERT INTO bank_link_sessions
            (id,browser_hash,member_id,token_ref,consent_id,connection_id,state,expires_at)
            VALUES ('generated-existing-session',?,1,?,?,?,?,?)""",
            (hashlib.sha256(browser.encode()).hexdigest(), ref, consent_id, connection_id, state,
             int(time.time()) + (-60 if expired else 600)))


@pytest.mark.parametrize('repair', [False, True])
@pytest.mark.parametrize('state,expired', [('pending', False), ('uncertain', False),
    ('exchanging', False), ('uncertain', True), ('exchanging', True)])
def test_unresolved_member_link_blocks_add_and_repair_across_browsers(link_service, repair, state, expired):
    from app.banking.client import BankingError
    service, calls, connection_id = link_service
    seed_link(service, state=state, expired=expired, connection_id=connection_id)
    with pytest.raises(BankingError, match='^member_link_unresolved_check_existing_session$'):
        service.start_link(member_id=1, browser='generated-other-browser',
                           connection_id=connection_id if repair else None)
    assert calls == []
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM bank_link_sessions').fetchone()[0] == 1
        assert conn.execute('SELECT state FROM bank_connections WHERE id=?', (connection_id,)).fetchone()[0] == 'needs_repair'


@pytest.mark.parametrize('operation', ['rename', 'bind'])
def test_default_create_app_existing_local_identity_is_editable(local_app, operation):
    app, _, service = local_app
    member_id = service.add_member('Generated existing')['member_id']
    binding = seed_account(service.db_path, member_id)
    client = TestClient(ListenerScope(app), base_url='http://127.0.0.1:8080')
    try:
        csrf = client.get('/connections/data/overview').json()['csrf']
        endpoint = '/connections/data/bind' if operation == 'bind' else f'/connections/data/member/{member_id}'
        response = client.post(endpoint, json=binding if operation == 'bind' else {'name': 'Generated corrected'},
            headers={'Origin': 'http://127.0.0.1:8080', 'X-Banking-CSRF': csrf})
        assert response.status_code == 200, response.json()
        overview = client.get('/connections/data/overview').json()
        assert overview['members'][0]['id'] == member_id
        if operation == 'rename':
            assert overview['members'][0]['name'] == 'Generated corrected'
        else:
            assert overview['connections'][0]['accounts'][0]['account_id'] == response.json()['account_id']
    finally:
        client.close()


@pytest.mark.parametrize('repair', [False, True])
@pytest.mark.parametrize('state,expired', [('pending', True), ('complete', False), ('exited', False)])
def test_terminal_or_expired_pending_link_allows_eligible_start(link_service, repair, state, expired):
    service, calls, connection_id = link_service
    seed_link(service, state=state, expired=expired, connection_id=connection_id)
    result = service.start_link(member_id=1, browser='generated-other-browser',
                                connection_id=connection_id if repair else None)
    assert result['session_id'] != 'generated-existing-session'
    assert calls == ['/link/token/create']
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT state FROM bank_link_sessions WHERE id=?', (result['session_id'],)).fetchone()[0] == 'pending'


def test_unresolved_link_does_not_block_a_different_member(link_service):
    service, calls, _ = link_service
    seed_link(service, state='uncertain', expired=True)
    result = service.start_link(member_id=2, browser='generated-other-browser')
    assert calls == ['/link/token/create']
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT member_id FROM bank_link_sessions WHERE id=?', (result['session_id'],)).fetchone()[0] == 2


@pytest.mark.parametrize('state,expired,visible', [('pending', False, True), ('pending', True, False),
    ('uncertain', True, True), ('exchanging', True, True)])
def test_browser_recovery_keeps_uncertainty_after_clock_expiry(link_service, state, expired, visible):
    service, calls, _ = link_service
    seed_link(service, state=state, expired=expired)
    links = service.browser_links('generated-original-browser')
    assert bool(links) is visible
    if visible:
        assert links[0]['state'] == state and links[0]['session_id'] == 'generated-existing-session'
    assert service.browser_links('generated-other-browser') == []
    assert calls == []


def test_unresolved_start_http_refusal_is_actionable_and_never_dispatches(link_service):
    from fastapi import FastAPI
    from app.banking.web import build_router
    service, calls, connection_id = link_service
    seed_link(service, state='pending', connection_id=connection_id)
    app = FastAPI()
    app.include_router(build_router(service))
    with TestClient(app, base_url=service.origin) as client:
        csrf = client.get('/connections/data/overview').json()['csrf']
        response = client.post('/connections/data/link/start',
            json={'member_id': 1, 'connection_id': connection_id},
            headers={'Origin': service.origin, 'X-Banking-CSRF': csrf})
        assert response.status_code == 409
        assert response.json()['detail'] == 'member_link_unresolved_check_existing_session'
    assert calls == []


def test_link_start_revalidates_unresolved_state_in_commit_transaction(link_service):
    import httpx
    from app.banking.client import BankingError
    service, calls, _ = link_service

    def provider(request):
        calls.append(request.url.path)
        assert request.url.path == '/link/token/create'
        # This request was admitted before another unresolved session appeared.
        # Prove publication revalidation, not single-dispatch concurrency.
        seed_link(service, state='uncertain')
        return httpx.Response(200, json={'link_token': 'generated-new-link',
            'hosted_link_url': 'https://secure.plaid.com/hl/generated', 'expiration': '2099-01-01T00:00:00Z'})

    service.transport = httpx.MockTransport(provider)
    with pytest.raises(BankingError, match='^member_link_unresolved_check_existing_session$'):
        service.start_link(member_id=1, browser='generated-other-browser')
    assert calls == ['/link/token/create']
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT id,state FROM bank_link_sessions').fetchall()[0][:] == ('generated-existing-session', 'uncertain')
        assert conn.execute('SELECT count(*) FROM bank_link_sessions').fetchone()[0] == 1
