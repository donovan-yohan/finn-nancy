"""Hosted Link completion uses Plaid's current results shape, not only legacy on_success.

Plaid returns `on_success` only for legacy integrations; current integrations
receive `link_sessions[].results.item_add_results[].public_token` instead.
"""
import json
import secrets

import httpx
import pytest

from app.db import engine


def make_service(household_db, tmp_path, completion):
    from app.banking.service import BankingService, CONSENT_VERSION
    credential = tmp_path / 'credentials.json'
    credential.write_text(json.dumps({'client_id': secrets.token_hex(12), 'secret': secrets.token_hex(24)}))
    credential.chmod(0o600)
    link = 'generated-link-' + secrets.token_hex(12)
    calls = []

    def provider(req):
        body = json.loads(req.content)
        calls.append(req.url.path)
        if req.url.path == '/link/token/create':
            return httpx.Response(200, json={'link_token': link, 'hosted_link_url': 'https://secure.plaid.com/hl/generated',
                                             'expiration': '2099-01-01T00:00:00Z'})
        if req.url.path == '/link/token/get':
            assert body['link_token'] == link
            return httpx.Response(200, json={'link_token': link, 'link_sessions': completion})
        if req.url.path == '/item/public_token/exchange':
            assert body['public_token'] == 'generated-public-current'
            return httpx.Response(200, json={'access_token': 'generated-access', 'item_id': 'generated-item'})
        if req.url.path == '/accounts/get':
            return httpx.Response(200, json={'accounts': [
                {'account_id': f'generated-account-{n}', 'name': f'Account {n}', 'type': 'depository',
                 'subtype': 'checking', 'balances': {'iso_currency_code': 'CAD'}} for n in range(3)]})
        raise AssertionError(req.url.path)

    service = BankingService(db_path=household_db, private_dir=tmp_path / 'private', credentials_file=credential,
                             origin='http://banking.example.invalid',
                             completion_uri='http://banking.example.invalid/connections', enabled=lambda: True,
                             strict_local=lambda: False, read_only=lambda: False,
                             transport=httpx.MockTransport(provider))
    service.consent(1, granted=True, version=CONSENT_VERSION)
    return service, calls


CURRENT = [{'link_session_id': 'generated-session', 'finished_at': '2026-11-10T00:00:00Z',
            'results': {'item_add_results': [{'public_token': 'generated-public-current'}]}}]


def test_current_item_add_results_complete_the_connection(household_db, tmp_path):
    service, calls = make_service(household_db, tmp_path, CURRENT)
    started = service.start_link(member_id=1, browser='generated-browser')
    result = service.finish_link(started['session_id'], browser='generated-browser')
    assert result['status'] == 'connected'
    assert calls.count('/item/public_token/exchange') == 1
    assert service.finish_link(started['session_id'], browser='generated-browser') == result
    assert calls.count('/item/public_token/exchange') == 1
    overview = service.overview()
    assert len(overview['connections']) == 1
    assert len(overview['connections'][0]['accounts']) == 3


def test_unfinished_session_with_current_shape_stays_pending(household_db, tmp_path):
    pending = [{'link_session_id': 'generated-session', 'finished_at': None,
                'results': {'item_add_results': [{'public_token': 'generated-public-current'}]}}]
    service, calls = make_service(household_db, tmp_path, pending)
    started = service.start_link(member_id=1, browser='generated-browser')
    assert service.finish_link(started['session_id'], browser='generated-browser') == {'status': 'pending'}
    assert '/item/public_token/exchange' not in calls


@pytest.mark.parametrize('completion', [
    [None, {'finished_at': '', 'results': {'item_add_results': [{'public_token': 'generated-public-current'}]}}],
    [{'finished_at': '2026-11-10T00:00:00Z', 'results': {'item_add_results': 'malformed'}}],
    [{'finished_at': '2026-11-10T00:00:00Z', 'results': {'item_add_results': [None, {}]}}],
    [{'finished_at': '2026-11-10T00:00:00Z', 'results': None}],
])
def test_malformed_or_tokenless_completion_never_exchanges(household_db, tmp_path, completion):
    service, calls = make_service(household_db, tmp_path, completion)
    started = service.start_link(member_id=1, browser='generated-browser')
    assert service.finish_link(started['session_id'], browser='generated-browser') == {'status': 'pending'}
    assert '/item/public_token/exchange' not in calls
    with engine.read_conn(household_db) as conn:
        assert conn.execute('SELECT state FROM bank_link_sessions').fetchone()[0] == 'pending'


def test_two_distinct_current_tokens_are_ambiguous(household_db, tmp_path):
    two = [{'finished_at': '2026-11-10T00:00:00Z', 'results': {'item_add_results': [
        {'public_token': 'generated-public-current'}, {'public_token': 'generated-public-other'}]}}]
    service, calls = make_service(household_db, tmp_path, two)
    started = service.start_link(member_id=1, browser='generated-browser')
    with pytest.raises(ValueError, match='server_link_verification_unavailable'):
        service.finish_link(started['session_id'], browser='generated-browser')
    assert '/item/public_token/exchange' not in calls


def test_legacy_on_success_only_remains_supported(household_db, tmp_path):
    legacy = [{'finished_at': '2026-11-10T00:00:00Z', 'on_success': {'public_token': 'generated-public-current'}}]
    service, calls = make_service(household_db, tmp_path, legacy)
    started = service.start_link(member_id=1, browser='generated-browser')
    assert service.finish_link(started['session_id'], browser='generated-browser')['status'] == 'connected'
    assert calls.count('/item/public_token/exchange') == 1
