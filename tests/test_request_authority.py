"""One trusted household, two request sources; no roles or caller-supplied identity."""
from __future__ import annotations

import logging
import re
from uuid import UUID

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings, get_settings
from app.db import engine
from app.web.app import create_app


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    monkeypatch.setenv('API_TOKEN', 'generated-authority-test-key')
    monkeypatch.setenv('PLAID_ENABLED', 'false')
    monkeypatch.setenv('STRICT_LOCAL_MODE', 'true')
    monkeypatch.setenv('PLAID_ORIGIN', 'https://banking.example.invalid')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def client():
    app = create_app()

    @app.get('/__authority/{item_id}')
    def inspect(request: Request, item_id: int):
        return {'source': request.state.authority.source,
                'request_id': request.state.request_id}

    return TestClient(app, base_url='https://banking.example.invalid')


def api_headers():
    return {'Authorization': 'Bearer generated-authority-test-key'}


@pytest.mark.parametrize('keyed', [False, True])
def test_unhandled_500_retains_correlation_and_sanitized_log(app_env, caplog, keyed):
    app = create_app()

    @app.get('/__failure/{item_id}')
    def fail(item_id: int):
        raise RuntimeError('generated-private-exception-detail')

    headers = {'X-Request-ID': 'forged-request', 'X-FN-Caller': 'forged-caller'}
    if keyed:
        headers.update(api_headers())
    with caplog.at_level(logging.INFO, logger='uvicorn.error.requests'):
        response = TestClient(app, raise_server_exceptions=False).get(
            '/__failure/78654?secret=generated-private-query', headers=headers)
    assert response.status_code == 500
    assert str(UUID(response.headers['x-request-id'])) != 'forged-request'
    assert response.headers['x-fn-caller'] == ('api' if keyed else 'web_ui')
    assert response.text == 'Internal Server Error'
    records = [r for r in caplog.records if r.name == 'uvicorn.error.requests']
    assert len(records) == 1
    assert records[0].request_id == response.headers['x-request-id']
    assert records[0].caller == response.headers['x-fn-caller']
    assert records[0].status_code == 500
    assert records[0].route == '/__failure/{item_id}'
    for sentinel in ['78654', 'generated-private-query', 'generated-private-exception-detail']:
        assert sentinel not in response.text
        assert sentinel not in str(records[0].__dict__)


def test_sources_are_derived_by_server_and_have_distinct_request_ids(app_env):
    browser = client()
    web = browser.get('/__authority/1', headers={
        'X-FN-Caller': 'api', 'X-Request-ID': 'caller-supplied'})
    direct = browser.get('/__authority/1', headers=api_headers())
    assert web.json()['source'] == 'web_ui'
    assert direct.json()['source'] == 'api'
    assert web.headers['x-fn-caller'] == 'web_ui'
    assert direct.headers['x-fn-caller'] == 'api'
    for response in (web, direct):
        assert str(UUID(response.json()['request_id'])) == response.headers['x-request-id']
    assert web.json()['request_id'] != direct.json()['request_id']


@pytest.mark.parametrize('route', ['/', '/__authority/1', '/api/finance/categories', '/connections/data/feed'])
def test_invalid_supplied_key_never_falls_back_to_web_authority(app_env, route):
    response = client().get(route, headers={'Authorization': 'Bearer generated-wrong-key'})
    assert response.status_code == 401
    assert response.json() == {'detail': 'invalid or missing bearer token'}


def test_browser_frontend_and_bearer_use_same_banking_write_endpoint(app_env):
    browser = client()
    route = '/connections/data/member'
    assert browser.post(route, json={'name': 'Generated refused'}).status_code == 403
    csrf = browser.get('/connections/data/overview').json()['csrf']
    web = browser.post(route, json={'name': 'Generated web member'}, headers={
        'Origin': 'https://banking.example.invalid', 'X-Banking-CSRF': csrf})
    direct = browser.post(route, json={'name': 'Generated API member'}, headers=api_headers())
    assert web.status_code == direct.status_code == 200
    assert web.headers['x-fn-caller'] == 'web_ui'
    assert direct.headers['x-fn-caller'] == 'api'
    with engine.read_conn(app_env) as conn:
        names = {r['name'] for r in conn.execute('SELECT name FROM household_members')}
    assert {'Generated web member', 'Generated API member'} <= names
    assert browser.post(route, json={'name': 'Generated cross-origin'}, headers={
        'Origin': 'https://other.example.invalid', 'X-Banking-CSRF': csrf}).status_code == 403


def test_bearer_can_drive_existing_frontend_forms_without_new_permissions(app_env):
    browser = client()
    with engine.read_conn(app_env) as conn:
        account = conn.execute('SELECT id FROM accounts LIMIT 1').fetchone()[0]
        category = conn.execute("SELECT id FROM categories WHERE kind='expense' LIMIT 1").fetchone()[0]
    response = browser.post('/txn/new', headers=api_headers(), data={
        'posted_on': '2026-06-12', 'description': 'Generated direct workflow',
        'amount': '12.34', 'account_id': account, 'category_id': category,
        'flow_kind': 'purchase'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['x-fn-caller'] == 'api'
    with engine.read_conn(app_env) as conn:
        row = conn.execute("SELECT id,amount_cents FROM transactions WHERE description='Generated direct workflow'").fetchone()
        assert row['amount_cents'] == -1234
        assert conn.execute('SELECT SUM(amount_cents) FROM transaction_splits WHERE transaction_id=?', (row['id'],)).fetchone()[0] == -1234


def test_api_remains_disabled_without_key_but_web_still_works(app_env, monkeypatch):
    monkeypatch.delenv('API_TOKEN')
    get_settings.cache_clear()
    browser = client()
    assert browser.get('/').status_code == 200
    assert browser.get('/api/finance/categories').status_code == 503
    assert browser.get('/', headers=api_headers()).status_code == 503


def test_keyed_frontend_banking_writes_still_enforce_egress_policy(app_env):
    response = client().post('/connections/data/link/start',
        headers=api_headers(), json={'member_id': 1})
    assert response.status_code == 409
    assert response.json() == {'detail': 'egress_blocked'}


def test_request_telemetry_is_content_free_and_route_templated(app_env, caplog):
    with caplog.at_level(logging.INFO, logger='uvicorn.error.requests'):
        response = client().get('/__authority/78654?secret=generated-query-secret', headers={
            **api_headers(), 'Cookie': 'generated-cookie-secret', 'X-FN-Caller': 'forged'})
    assert response.status_code == 200
    records = [r for r in caplog.records if r.name == 'uvicorn.error.requests']
    assert len(records) == 1
    event = records[0]
    assert event.caller == 'api'
    assert event.route == '/__authority/{item_id}'
    assert event.method == 'GET' and event.status_code == 200
    assert event.request_id == response.headers['x-request-id']
    assert event.duration_ms >= 0
    assert not re.search('generated-authority-test-key|generated-query-secret|generated-cookie-secret|78654', str(event.__dict__))


def test_browser_chat_keeps_origin_guard_but_keyed_client_can_use_same_workflow(app_env):
    browser = client()
    denied = browser.post('/chat/new', headers={'Origin': 'https://other.example.invalid'}, follow_redirects=False)
    assert denied.status_code == 403
    accepted = browser.post('/chat/new', headers={**api_headers(), 'Origin': 'https://other.example.invalid'}, follow_redirects=False)
    assert accepted.status_code == 303
    assert accepted.headers['x-fn-caller'] == 'api'


def test_invalid_websocket_key_cannot_be_treated_as_browser(app_env):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client().websocket_connect('/chat/socket', headers={
            'Authorization': 'Bearer generated-wrong-key', 'Origin': 'https://banking.example.invalid'}):
            pass
    assert exc.value.code == 4401


def test_keyed_link_session_is_stable_without_browser_cookie_and_rotates_with_key(app_env, monkeypatch):
    from app.banking.client import BankingError
    browser = client()
    service = browser.app.state.banking_service
    bindings = {}

    def start(*, member_id, browser, connection_id=None):
        bindings['generated-session'] = browser
        return {'session_id': 'generated-session', 'hosted_link_url': 'https://secure.plaid.com/hl/generated'}

    def finish(session_id, *, browser):
        if bindings[session_id] != browser:
            raise BankingError('browser_session_mismatch')
        return {'status': 'pending'}

    monkeypatch.setattr(service, 'start_link', start)
    monkeypatch.setattr(service, 'finish_link', finish)
    assert browser.post('/connections/data/link/start', json={'member_id': 1}, headers=api_headers()).status_code == 200
    assert not browser.cookies
    response = browser.post('/connections/data/link/finish', json={'session_id': 'generated-session'}, headers=api_headers())
    assert response.json() == {'status': 'pending'}
    assert 'generated-authority-test-key' not in bindings['generated-session']
    csrf = browser.get('/connections/data/overview').json()['csrf']
    refused = browser.post('/connections/data/link/finish', json={'session_id': 'generated-session'}, headers={
        'Origin': 'https://banking.example.invalid', 'X-Banking-CSRF': csrf})
    assert refused.status_code == 409 and refused.json()['detail'] == 'browser_session_mismatch'
    monkeypatch.setenv('API_TOKEN', 'generated-rotated-key')
    get_settings.cache_clear()
    rotated = browser.post('/connections/data/link/finish', json={'session_id': 'generated-session'}, headers={
        'Authorization': 'Bearer generated-rotated-key'})
    assert rotated.status_code == 409 and rotated.json()['detail'] == 'browser_session_mismatch'


def test_keyed_websocket_keeps_thread_binding_without_browser_origin(app_env, monkeypatch):
    from app.web.routes import chat
    from app.hermes_chat.threads import ThreadStore
    from uuid import uuid4
    monkeypatch.setenv('HERMES_CHAT_URL', 'ws://127.0.0.1:19119/api/ws')
    get_settings.cache_clear()
    thread = uuid4().hex
    ThreadStore(get_settings(), thread).create()

    async def serve(socket, settings, thread_id):
        await socket.accept()
        await socket.send_json({'source': socket.state.authority.source, 'thread': thread_id})
        await socket.close()

    monkeypatch.setattr(chat, 'serve', serve)
    browser = client()
    browser.cookies.set('fn_chat_thread', thread)
    with browser.websocket_connect('/chat/socket', headers=api_headers()) as socket:
        assert socket.receive_json() == {'source': 'api', 'thread': thread}
    browser.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as exc:
        with browser.websocket_connect('/chat/socket', headers=api_headers()):
            pass
    assert exc.value.code == 4403


def test_duplicate_authorization_is_rejected_before_mutation(app_env):
    response = client().post('/connections/data/member', json={'name': 'Generated refused'}, headers=[
        ('Authorization', 'Bearer generated-authority-test-key'),
        ('Authorization', 'Bearer generated-wrong-key')])
    assert response.status_code == 401
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM household_members WHERE name='Generated refused'").fetchone()[0] == 0


def test_api_body_limit_and_error_correlation_remain_effective(app_env, monkeypatch, caplog):
    monkeypatch.setenv('API_MAX_BODY_BYTES', '64')
    get_settings.cache_clear()
    with caplog.at_level(logging.INFO, logger='uvicorn.error.requests'):
        response = client().post('/api/transactions', headers=api_headers(),
            content='generated-body-secret' * 10)
    assert response.status_code == 413
    assert response.headers['x-fn-caller'] == 'api'
    records = [r for r in caplog.records if r.name == 'uvicorn.error.requests']
    assert len(records) == 1
    assert records[0].request_id == response.headers['x-request-id']
    assert records[0].status_code == 413
    assert records[0].route == '<unmatched>'
    assert 'generated-body-secret' not in str(records[0].__dict__)


def test_keyed_client_can_apply_and_undo_category_via_frontend_workflow(app_env):
    from app.api import finance, service
    created = service.record_transaction(amount_cents=-1234, posted_on='2026-06-12',
        description='Generated workflow purchase', flow_kind='purchase',
        external_id='generated-authority-workflow')
    tid = created['transaction_id']
    before = finance.transaction_detail(tid)
    with engine.read_conn(app_env) as conn:
        target = conn.execute("SELECT id FROM categories WHERE name='Restaurants'").fetchone()[0]
    proposal = finance.propose_category(operation_key='authority:category', transaction_id=tid,
        split_id=before['splits'][0]['id'], to_category_id=target,
        expected_state_digest=before['state_digest'], rationale='Generated explicit category decision')
    browser = client()
    approved = browser.post(f'/actions/{proposal["id"]}/approve', headers=api_headers(), follow_redirects=False)
    assert approved.status_code == 303 and approved.headers['x-fn-caller'] == 'api'
    assert finance.transaction_detail(tid)['splits'][0]['category_id'] == target
    reverted = browser.post(f'/actions/{proposal["id"]}/revert', headers=api_headers(), follow_redirects=False)
    assert reverted.status_code == 303 and reverted.headers['x-fn-caller'] == 'api'
    after = finance.transaction_detail(tid)
    assert after['splits'] == before['splits'] and after['amount_cents'] == before['amount_cents']
