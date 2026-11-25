"""Hosted Link completes without a browser click.

Provider fixtures copy the exact key layout Plaid Production returned for a real
completed Hosted Link session on 2026-11-14 (no on_success; results carries the
public token). All values are generated.
"""
import asyncio
import json
import secrets
import threading
import time

import httpx
import pytest

from app.banking.client import BankingError
from app.banking.completion import run_link_completion
from app.banking.service import BankingService, CONSENT_VERSION
from app.db import engine

LINK = 'generated-link-' + secrets.token_hex(8)
PUBLIC = 'generated-public-' + secrets.token_hex(8)
ACCESS = 'generated-access-' + secrets.token_hex(8)


def results(public=None):
    return {'bank_income_results': [], 'cra_item_add_results': [], 'cra_report_results': None,
            'cra_update_results': [], 'document_income_results': None, 'payroll_income_results': [],
            'item_add_results': [] if public is None else [{
                'public_token': public, 'institution': {'institution_id': 'ins_generated', 'name': 'Generated Bank'},
                'accounts': [{'id': f'generated-acct-{n}', 'name': f'Generated Account {n}', 'mask': f'{n}{n}{n}{n}',
                              'type': 'depository', 'subtype': 'checking'} for n in range(3)]}]}


def succeeded(public=PUBLIC, session='generated-session-ok'):
    return {'link_session_id': session, 'started_at': '2026-11-14T15:15:26Z', 'finished_at': '2026-11-14T15:21:14Z',
            'results': results(public), 'events': [
                {'event_name': 'SELECT_INSTITUTION', 'timestamp': '2026-11-14T15:19:24Z',
                 'event_metadata': {'institution_id': 'ins_generated', 'institution_name': 'Generated Bank'}},
                {'event_name': 'HANDOFF', 'timestamp': '2026-11-14T15:21:15Z', 'event_metadata': {}}]}


def unfinished():
    return {'link_session_id': 'generated-session-open', 'started_at': '2026-11-14T15:15:26Z', 'events': []}


def exited():
    return {'link_session_id': 'generated-session-exit', 'started_at': '2026-11-14T15:15:26Z',
            'finished_at': '2026-11-14T15:18:01Z', 'results': results(),
            'exit': {'error': {'error_type': 'ITEM_ERROR', 'error_code': 'INSTITUTION_NOT_RESPONDING',
                               'error_message': 'generated provider text must not be stored'},
                     'metadata': {'status': 'requires_credentials',
                                  'institution': {'institution_id': 'ins_unreachable', 'name': 'Unreachable Bank'}}},
            'events': [{'event_name': 'ERROR', 'timestamp': '2026-11-14T15:18:00Z', 'event_metadata': {
                'institution_id': 'ins_unreachable', 'institution_name': 'Unreachable Bank',
                'error_code': 'INSTITUTION_NOT_RESPONDING', 'error_type': 'ITEM_ERROR',
                'error_message': 'generated provider text must not be stored'}}]}


class Provider:
    def __init__(self):
        self.sessions = [unfinished()]
        self.calls = []
        self.exchange_delay = 0.0
        self.token_get_error = None

    def __call__(self, req):
        body = json.loads(req.content)
        self.calls.append(req.url.path)
        if req.url.path == '/link/token/create':
            return httpx.Response(200, json={'link_token': LINK, 'hosted_link_url': 'https://secure.plaid.com/hl/generated',
                                             'expiration': '2099-01-01T00:00:00Z'})
        if req.url.path == '/link/token/get':
            assert body['link_token'] == LINK
            if self.token_get_error:
                return httpx.Response(500, json={'error_code': self.token_get_error, 'error_message': 'generated'})
            return httpx.Response(200, json={'link_token': LINK, 'created_at': '2026-11-14T15:15:20Z',
                                             'expiration': '2099-01-01T00:00:00Z', 'metadata': {},
                                             'request_id': 'generatedRequest1', 'link_sessions': self.sessions})
        if req.url.path == '/item/public_token/exchange':
            assert body['public_token'] == PUBLIC
            time.sleep(self.exchange_delay)
            return httpx.Response(200, json={'access_token': ACCESS, 'item_id': 'generated-item'})
        if req.url.path == '/accounts/get':
            return httpx.Response(200, json={'accounts': [
                {'account_id': f'generated-acct-{n}', 'name': f'Generated Account {n}', 'type': 'depository',
                 'subtype': 'checking', 'balances': {'iso_currency_code': 'CAD'}} for n in range(3)]})
        raise AssertionError(req.url.path)


@pytest.fixture
def banking(household_db, tmp_path):
    credential = tmp_path / 'generated-credentials.json'
    credential.write_text(json.dumps({'client_id': secrets.token_hex(12), 'secret': secrets.token_hex(24)}))
    credential.chmod(0o600)
    provider = Provider()
    flags = {'read_only': False}
    service = BankingService(db_path=household_db, private_dir=tmp_path / 'private', credentials_file=credential,
        origin='http://banking.example.invalid', completion_uri='http://banking.example.invalid/connections',
        enabled=lambda: True, strict_local=lambda: False, read_only=lambda: flags['read_only'],
        transport=httpx.MockTransport(provider))
    service.consent(1, granted=True, version=CONSENT_VERSION)
    started = service.start_link(member_id=1, browser='generated-browser')
    return service, provider, started['session_id'], flags


def checks(service):
    with engine.read_conn(service.db_path) as conn:
        return [dict(r) for r in conn.execute('SELECT source,outcome,detail FROM bank_link_checks ORDER BY id')]


def state(service):
    with engine.read_conn(service.db_path) as conn:
        return conn.execute('SELECT state FROM bank_link_sessions').fetchone()[0]


def test_server_completes_finished_session_without_any_browser(banking):
    service, provider, session_id, _ = banking
    assert service.complete_pending_links() == [{'status': 'pending'}]
    assert '/item/public_token/exchange' not in provider.calls
    provider.sessions = [succeeded()]
    [result] = service.complete_pending_links()
    assert result['status'] == 'connected'
    assert provider.calls.count('/item/public_token/exchange') == 1
    assert len(service.overview()['connections'][0]['accounts']) == 3
    # A browser arriving later sees the result; it never exchanges again.
    assert service.finish_link(session_id, browser='generated-browser')['status'] == 'connected'
    assert service.complete_pending_links() == []
    assert provider.calls.count('/item/public_token/exchange') == 1


def test_checks_are_recorded_content_free_and_deduplicated(banking):
    service, provider, session_id, _ = banking
    for _ in range(3):
        service.complete_pending_links()
    provider.sessions = [succeeded()]
    service.complete_pending_links()
    rows = checks(service)
    assert [(r['source'], r['outcome']) for r in rows] == [('server', 'pending'), ('server', 'connected')]
    detail = json.loads(rows[-1]['detail'])
    assert detail['request_id'] == 'generatedRequest1'
    assert detail['sessions'][0]['public_tokens'] == 1
    assert detail['sessions'][0]['institutions'] == ['ins_generated']
    assert 'legacy_on_success' not in detail['sessions'][0]
    recorded = json.dumps(rows)
    for private in (LINK, PUBLIC, ACCESS, 'Generated Account', '1111'):
        assert private not in recorded
    with engine.read_conn(service.db_path) as conn:
        dump = '\n'.join(conn.iterdump())
    for token in (LINK, PUBLIC, ACCESS):
        assert token not in dump


def test_exit_settles_the_session_and_keeps_the_reason(banking):
    service, provider, session_id, _ = banking
    provider.sessions = [exited()]
    assert service.finish_link(session_id, browser='generated-browser') == {'status': 'exited'}
    assert state(service) == 'exited'
    detail = json.loads(checks(service)[-1]['detail'])
    session = detail['sessions'][0]
    assert session['exit_error_code'] == 'INSTITUTION_NOT_RESPONDING'
    assert session['exit_status'] == 'requires_credentials'
    assert {'event': 'ERROR', 'at': '2026-11-14T15:18:00Z', 'institution_id': 'ins_unreachable',
            'institution_name': 'Unreachable Bank', 'error_code': 'INSTITUTION_NOT_RESPONDING',
            'error_type': 'ITEM_ERROR'} in session['events']
    assert 'generated provider text' not in json.dumps(checks(service))
    # An exited attempt no longer blocks connecting again.
    provider.calls.clear()
    service.start_link(member_id=1, browser='generated-browser')
    assert provider.calls == ['/link/token/create']


def test_reopened_hosted_link_after_exit_is_still_rescued(banking):
    service, provider, session_id, _ = banking
    provider.sessions = [exited()]
    service.complete_pending_links()
    assert state(service) == 'exited'
    provider.sessions = [exited(), succeeded()]
    assert service.complete_pending_links() == []  # exited sessions are rechecked at most once a minute
    service._last_server_check.clear()
    [result] = service.complete_pending_links()
    assert result['status'] == 'connected'
    assert provider.calls.count('/item/public_token/exchange') == 1


def test_exit_with_another_session_still_open_stays_pending(banking):
    service, provider, session_id, _ = banking
    provider.sessions = [exited(), unfinished()]
    assert service.finish_link(session_id, browser='generated-browser') == {'status': 'pending'}
    assert state(service) == 'pending'


def test_browser_and_server_racing_exchange_exactly_once(banking):
    service, provider, session_id, _ = banking
    provider.sessions = [succeeded()]
    provider.exchange_delay = 0.3
    outcomes = []
    def browser():
        outcomes.append(service.finish_link(session_id, browser='generated-browser')['status'])
    def server():
        outcomes.extend(r['status'] for r in service.complete_pending_links())
    threads = [threading.Thread(target=browser), threading.Thread(target=server)]
    for t in threads: t.start()
    for t in threads: t.join()
    # The server may select the session before or after the browser claims it; either
    # way every reported outcome is 'connected' and the token is exchanged once.
    assert 'connected' in outcomes and set(outcomes) == {'connected'}
    assert provider.calls.count('/item/public_token/exchange') == 1
    assert state(service) == 'complete'


def test_provider_error_code_is_recorded_but_not_exposed(banking):
    service, provider, session_id, _ = banking
    provider.token_get_error = 'INTERNAL_SERVER_ERROR'
    with pytest.raises(BankingError, match='^provider_request_failed$'):
        service.finish_link(session_id, browser='generated-browser')
    row = checks(service)[-1]
    assert (row['source'], row['outcome']) == ('browser', 'provider_request_failed')
    assert json.loads(row['detail']) == {'provider_error_code': 'INTERNAL_SERVER_ERROR'}


def test_server_poll_does_nothing_when_read_only_or_idle(banking, household_db):
    service, provider, session_id, flags = banking
    provider.calls.clear()
    flags['read_only'] = True
    assert service.complete_pending_links() == []
    flags['read_only'] = False
    with engine.write_tx(household_db) as conn:
        conn.execute('UPDATE bank_link_sessions SET expires_at=?', (time.time() - 1,))
    assert service.complete_pending_links() == []
    assert provider.calls == []


def test_repair_completes_from_current_shape_without_new_token(banking):
    service, provider, _, _ = banking
    ref = service.store.put(ACCESS)
    with engine.write_tx(service.db_path) as conn:
        conn.execute("UPDATE bank_link_sessions SET state='exited'")
        ident = conn.execute('''INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state)
            VALUES (1,'generated-item',?,?,'needs_repair')''', (ref, CONSENT_VERSION)).lastrowid
    started = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)
    repaired = succeeded(public=None, session='generated-session-repair')
    provider.sessions = [repaired]
    result = service.finish_link(started['session_id'], browser='generated-browser')
    assert result == {'status': 'connected', 'connection_id': ident}
    assert '/item/public_token/exchange' not in provider.calls


def test_completion_loop_survives_failures_and_stops():
    calls = []
    class Service:
        def complete_pending_links(self):
            calls.append(1)
            raise RuntimeError('generated')
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(run_link_completion(stop, Service(), poll_seconds=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    asyncio.run(scenario())
    assert len(calls) >= 2


def _repair_pair(service):
    ref = service.store.put(ACCESS)
    with engine.write_tx(service.db_path) as conn:
        conn.execute("UPDATE bank_link_sessions SET state='exited'")
        ident = conn.execute('''INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state)
            VALUES (1,'generated-item',?,?,'needs_repair')''', (ref, CONSENT_VERSION)).lastrowid
    first = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)['session_id']
    with engine.write_tx(service.db_path) as conn:
        conn.execute("UPDATE bank_link_sessions SET state='exited' WHERE id=?", (first,))
    second = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)['session_id']
    return ident, first, second


@pytest.mark.parametrize('settled,expected', [('active', 'complete'), ('removed', 'exited')])
def test_superseded_repair_session_leaves_pending(banking, settled, expected):
    service, provider, _, _ = banking
    ident, first, second = _repair_pair(service)
    with engine.write_tx(service.db_path) as conn:
        conn.execute('UPDATE bank_connections SET state=? WHERE id=?', (settled, ident))
    provider.calls.clear()
    if settled == 'active':
        assert service.finish_link(second, browser='generated-browser')['status'] == 'connected'
    else:
        with pytest.raises(BankingError, match='connection_not_repairable'):
            service.finish_link(second, browser='generated-browser')
    assert provider.calls == []
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT state FROM bank_link_sessions WHERE id=?', (second,)).fetchone()[0] == expected
    # A settled attempt no longer blocks a new link for the member.
    service.start_link(member_id=1, browser='generated-browser')
