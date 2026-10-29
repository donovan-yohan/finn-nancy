"""Generated adversarial lifecycle races; all provider traffic is MockTransport."""
import json
import secrets

import httpx
import pytest

from app.banking.client import BankingError
from app.banking.service import BankingService, CONSENT_VERSION
from app.db import engine


@pytest.fixture
def banking(household_db, tmp_path):
    credential = tmp_path / 'generated-credentials.json'
    credential.write_text(json.dumps({'client_id': secrets.token_hex(12), 'secret': secrets.token_hex(24)}))
    credential.chmod(0o600)
    calls = []
    def unexpected(req):
        raise AssertionError(req.url.path)
    service = BankingService(db_path=household_db, private_dir=tmp_path/'private', credentials_file=credential,
        origin='http://banking.example.invalid', completion_uri='http://banking.example.invalid/connections',
        enabled=lambda: True, strict_local=lambda: False, read_only=lambda: False,
        transport=httpx.MockTransport(unexpected))
    service.consent(1, granted=True, version=CONSENT_VERSION)
    return service, calls


def connection(service, state='active'):
    ref = service.store.put('generated-access-' + secrets.token_hex(12))
    with engine.write_tx(service.db_path) as conn:
        return conn.execute('''INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state)
            VALUES (1,?,?,?,?)''', ('generated-item-'+secrets.token_hex(12), ref, CONSENT_VERSION, state)).lastrowid


def sync_page(more=False, cursor='generated-final'):
    return {'added': [], 'modified': [], 'removed': [], 'has_more': more, 'next_cursor': cursor}


@pytest.mark.parametrize('transition', ['remove_uncertain', 'needs_repair', 'generation'])
def test_sync_stops_every_later_dispatch_after_connection_changes(banking, transition):
    service, calls = banking
    ident = connection(service)
    def provider(req):
        calls.append(req.url.path)
        if req.url.path == '/item/remove':
            raise httpx.ReadTimeout('generated unknown removal')
        assert req.url.path == '/transactions/sync'
        if len(calls) == 1:
            if transition == 'remove_uncertain':
                with pytest.raises(BankingError, match='revocation_uncertain_do_not_retry'):
                    service.disconnect(ident)
            else:
                with engine.write_tx(service.db_path) as conn:
                    sql = ("UPDATE bank_connections SET state='needs_repair' WHERE id=?" if transition == 'needs_repair'
                           else 'UPDATE bank_connections SET generation=generation+1 WHERE id=?')
                    conn.execute(sql, (ident,))
            return httpx.Response(200, json=sync_page(True, 'generated-next'))
        return httpx.Response(200, json=sync_page())
    service.transport = httpx.MockTransport(provider)
    with pytest.raises(BankingError):
        service.sync(ident)
    assert calls == (['/transactions/sync', '/item/remove'] if transition == 'remove_uncertain'
                     else ['/transactions/sync'])
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_windows').fetchone()[0] == 0
        if transition == 'remove_uncertain':
            assert conn.execute('SELECT state FROM bank_connections').fetchone()[0] == 'remove_uncertain'


def test_sequential_sync_windows_and_remove_keep_their_own_dispatch_permission(banking):
    service, calls = banking
    ident = connection(service)
    def provider(req):
        calls.append(req.url.path)
        if req.url.path == '/transactions/sync':
            return httpx.Response(200, json=sync_page(cursor='generated-' + str(len(calls))))
        assert req.url.path == '/item/remove'
        return httpx.Response(200, json={})
    service.transport = httpx.MockTransport(provider)
    assert service.sync(ident)['generation'] == 1
    assert service.sync(ident)['generation'] == 2
    assert service.disconnect(ident) == {'status': 'removed'}
    assert calls == ['/transactions/sync', '/transactions/sync', '/item/remove']


def link_created():
    return {'link_token': 'generated-link', 'hosted_link_url': 'https://secure.plaid.com/hl/generated',
            'expiration': '2099-01-01T00:00:00Z'}


def link_finished():
    return {'link_token': 'generated-link', 'link_sessions': [
        {'finished_at': 'generated-finished', 'on_success': {'public_token': 'generated-public'}}]}


@pytest.mark.parametrize('repair,boundary', [(False, 'create'), (True, 'create'),
    (False, 'get'), (True, 'get'), (False, 'exchange')])
def test_link_consent_revision_cannot_be_replaced_in_flight(banking, repair, boundary):
    service, calls = banking
    ident = connection(service, 'needs_repair') if repair else None
    def provider(req):
        calls.append(req.url.path)
        if req.url.path == {'create': '/link/token/create', 'get': '/link/token/get',
                            'exchange': '/item/public_token/exchange'}[boundary]:
            service.consent(1, granted=False, version=CONSENT_VERSION)
            service.consent(1, granted=True, version=CONSENT_VERSION)
        if req.url.path == '/link/token/create':
            return httpx.Response(200, json=link_created())
        if req.url.path == '/link/token/get':
            return httpx.Response(200, json=link_finished())
        if req.url.path == '/item/public_token/exchange':
            return httpx.Response(200, json={'access_token': 'generated-access', 'item_id': 'generated-item'})
        if req.url.path == '/accounts/get':
            return httpx.Response(200, json={'accounts': []})
        raise AssertionError(req.url.path)
    service.transport = httpx.MockTransport(provider)
    with pytest.raises(BankingError):
        started = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)
        service.finish_link(started['session_id'], browser='generated-browser')
    assert calls.count('/item/public_token/exchange') == int(boundary == 'exchange')
    assert '/accounts/get' not in calls
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM bank_link_sessions WHERE state='complete'").fetchone()[0] == 0
        if boundary == 'create':
            assert conn.execute('SELECT COUNT(*) FROM bank_link_sessions').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM bank_connections WHERE state='active'").fetchone()[0] == 0
    if boundary == 'exchange':
        with pytest.raises(BankingError, match='link_outcome_uncertain_do_not_retry'):
            service.finish_link(started['session_id'], browser='generated-browser')
        assert calls.count('/item/public_token/exchange') == 1


@pytest.mark.parametrize('transition', ['removed', 'repair_cycle', 'generation'])
def test_discovery_cannot_publish_after_lifecycle_changes(banking, transition):
    service, calls = banking
    ident = connection(service)
    def provider(req):
        calls.append(req.url.path)
        if req.url.path == '/item/remove':
            return httpx.Response(200, json={})
        assert req.url.path == '/accounts/get'
        if transition == 'removed':
            service.disconnect(ident)
        else:
            with engine.write_tx(service.db_path) as conn:
                conn.execute("UPDATE bank_connections SET generation=generation+1,state=? WHERE id=?",
                             ('needs_repair' if transition == 'repair_cycle' else 'active', ident))
                if transition == 'repair_cycle':
                    conn.execute("UPDATE bank_connections SET state='active',generation=generation+1 WHERE id=?", (ident,))
        return httpx.Response(200, json={'accounts': [{'account_id': 'generated-account',
            'name': 'Generated Checking', 'type': 'depository', 'balances': {'iso_currency_code': 'CAD'}}]})
    service.transport = httpx.MockTransport(provider)
    with pytest.raises(BankingError, match='stale_connection'):
        service.discover_accounts(ident)
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_accounts').fetchone()[0] == 0


@pytest.mark.parametrize('boundary', ['create', 'get'])
def test_removed_repair_cannot_create_or_complete_a_link(banking, boundary):
    service, calls = banking
    ident = connection(service, 'needs_repair')
    def provider(req):
        calls.append(req.url.path)
        if req.url.path == '/item/remove':
            return httpx.Response(200, json={})
        if req.url.path == '/link/token/' + boundary:
            service.disconnect(ident)
        if req.url.path == '/link/token/create':
            return httpx.Response(200, json=link_created())
        if req.url.path == '/link/token/get':
            return httpx.Response(200, json=link_finished())
        if req.url.path == '/accounts/get':
            return httpx.Response(200, json={'accounts': []})
        raise AssertionError(req.url.path)
    service.transport = httpx.MockTransport(provider)
    with pytest.raises(BankingError, match='stale_connection|connection_not_repairable'):
        started = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)
        service.finish_link(started['session_id'], browser='generated-browser')
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT state FROM bank_connections').fetchone()[0] == 'removed'
        assert conn.execute("SELECT COUNT(*) FROM bank_link_sessions WHERE state='complete'").fetchone()[0] == 0
        if boundary == 'create':
            assert conn.execute('SELECT COUNT(*) FROM bank_link_sessions').fetchone()[0] == 0
    assert '/accounts/get' not in calls


def test_completed_repair_replay_reports_current_removed_state_without_http(banking):
    service, calls = banking
    ident = connection(service, 'needs_repair')
    def provider(req):
        calls.append(req.url.path)
        responses = {'/link/token/create': link_created(), '/link/token/get': link_finished(),
                     '/accounts/get': {'accounts': []}, '/item/remove': {}}
        return httpx.Response(200, json=responses[req.url.path])
    service.transport = httpx.MockTransport(provider)
    started = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)
    assert service.finish_link(started['session_id'], browser='generated-browser')['status'] == 'connected'
    service.disconnect(ident)
    before = list(calls)
    assert service.finish_link(started['session_id'], browser='generated-browser') == {
        'status': 'removed', 'connection_id': ident}
    assert calls == before


@pytest.mark.parametrize('marker', ['directory', 'linked_file', 'ignored', 'bare'])
def test_private_storage_refuses_any_git_ancestor_before_io(tmp_path, monkeypatch, marker):
    from app.banking.client import PlaidClient
    from app.banking.secrets import PrivateStore, private_read
    root = tmp_path / 'generated-repo'
    root.mkdir()
    if marker == 'linked_file':
        (root/'.git').write_text('gitdir: generated-metadata-not-read')
    elif marker == 'bare':
        (root/'HEAD').write_text('ref: refs/heads/generated')
        (root/'objects').mkdir()
        (root/'refs').mkdir()
    else:
        (root/'.git').mkdir()
        if marker == 'ignored':
            (root/'.gitignore').write_text('private/\n*.json\n')
    nested = root/'nested'
    nested.mkdir()
    credential = nested/'generated-credentials.json'
    credential.write_text(json.dumps({'client_id': secrets.token_hex(12), 'secret': secrets.token_hex(24)}))
    credential.chmod(0o600)
    opened = []
    import app.banking.secrets as storage
    real_open = storage.os.open
    def record_open(*args, **kwargs):
        opened.append(str(args[0]))
        return real_open(*args, **kwargs)
    monkeypatch.setattr(storage.os, 'open', record_open)
    with pytest.raises(BankingError, match='private_path_inside_git'):
        PrivateStore(nested/'not-created'/'private')
    assert not (nested/'not-created').exists()
    with pytest.raises(BankingError, match='private_path_inside_git'):
        private_read(credential)
    calls = []
    client = PlaidClient(credential, policy=lambda: None, transport=httpx.MockTransport(
        lambda req: (calls.append(req.url.path) or httpx.Response(200, json={}))))
    with pytest.raises(BankingError, match='private_path_inside_git'):
        client.post('/accounts/get', {})
    assert calls == []
    assert opened == []
    # A safe sibling must not inherit its neighbor's Git restriction.
    store = PrivateStore(tmp_path/'safe-sibling')
    ref = store.put('generated-safe-value')
    assert store.get(ref) == 'generated-safe-value'
    store.remove(ref)


def test_private_store_rechecks_git_boundary_after_construction(tmp_path):
    from app.banking.secrets import PrivateStore
    root = tmp_path/'generated-repo'
    root.mkdir()
    store = PrivateStore(root/'private')
    ref = store.put('generated-secret')
    (root/'.git').mkdir()
    for action in (lambda: store.get(ref), lambda: store.put('generated-new'), lambda: store.remove(ref)):
        with pytest.raises(BankingError, match='private_path_inside_git'):
            action()


@pytest.mark.parametrize('previously_excluded', [False, True])
def test_new_binding_activation_requires_reopening_every_observed_month(banking, previously_excluded):
    from app.db import repo_period_policy
    service, calls = banking
    ident = connection(service)
    def provider(req):
        calls.append(req.url.path)
        if req.url.path == '/accounts/get':
            return httpx.Response(200, json={'accounts': [{'account_id': 'generated-account',
                'name': 'Generated Checking', 'type': 'depository', 'balances': {'iso_currency_code': 'CAD'}}]})
        assert req.url.path == '/transactions/sync'
        posted = '2026-06-01' if len(calls) == 2 else '2026-07-01'
        return httpx.Response(200, json={**sync_page(cursor='generated-'+posted), 'added': [{
            'transaction_id': 'generated-'+posted, 'account_id': 'generated-account', 'amount': 5,
            'date': posted, 'pending': False, 'iso_currency_code': 'CAD', 'name': 'Generated purchase'}]})
    service.transport = httpx.MockTransport(provider)
    service.discover_accounts(ident)
    with engine.write_tx(service.db_path) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Generated','chequing','CAD')").lastrowid
    revision = 0
    if previously_excluded:
        revision = service.bind(ident, 'generated-account', account_id=account, included=False,
            ownership='shared', role='primary', expected_revision=0)['revision']
    service.sync(ident)
    with engine.write_tx(service.db_path) as conn:
        repo_period_policy.close_period(conn, '2026-06', snapshot={'generated': True}, exceptions=[],
            actor='generated-test', reason='Generated close', operation_key='generated-close')
    # Raw observation staging remains allowed while June is closed.
    assert service.sync(ident)['status'] == 'synced'
    with pytest.raises(repo_period_policy.PeriodLockedError, match='2026-06'):
        service.bind(ident, 'generated-account', account_id=account, included=True,
            ownership='shared', role='primary', expected_revision=revision)
    with engine.write_tx(service.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bank_bindings').fetchone()[0] == revision
        assert conn.execute('SELECT COUNT(*) FROM v_bank_feed_blockers').fetchone()[0] == 0
        repo_period_policy.reopen_period(conn, '2026-06', actor='generated-test', reason='Review historical feed',
            operation_key='generated-reopen')
    assert service.bind(ident, 'generated-account', account_id=account, included=True,
        ownership='shared', role='primary', expected_revision=revision)['revision'] == revision + 1


@pytest.mark.parametrize('operation', ['sync', 'discover', 'repair_create', 'repair_get', 'remove'])
def test_connection_is_rechecked_between_credentials_and_actual_dispatch(banking, monkeypatch, operation):
    import app.banking.secrets as storage
    service, calls = banking
    ident = connection(service, 'needs_repair' if operation.startswith('repair') else 'active')
    def provider(req):
        calls.append(req.url.path)
        assert req.url.path == '/link/token/create'
        return httpx.Response(200, json=link_created())
    service.transport = httpx.MockTransport(provider)
    started = None
    if operation == 'repair_get':
        started = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)
        calls.clear()
    read = storage.private_read
    def change_after_read(path):
        value = read(path)
        if path == service.credentials_file:
            with engine.write_tx(service.db_path) as conn:
                conn.execute('UPDATE bank_connections SET generation=generation+1 WHERE id=?', (ident,))
        return value
    monkeypatch.setattr(storage, 'private_read', change_after_read)
    with pytest.raises(BankingError):
        if operation == 'sync':
            service.sync(ident)
        elif operation == 'discover':
            service.discover_accounts(ident)
        elif operation == 'repair_create':
            service.start_link(member_id=1, browser='generated-browser', connection_id=ident)
        elif operation == 'repair_get':
            assert started is not None
            service.finish_link(started['session_id'], browser='generated-browser')
        else:
            service.disconnect(ident)
    assert calls == []


def test_credentials_git_boundary_is_rechecked_immediately_before_dispatch(tmp_path):
    from app.banking.client import PlaidClient
    root = tmp_path/'generated-repo'
    root.mkdir()
    credential = root/'generated.json'
    credential.write_text(json.dumps({'client_id': 'generated-client', 'secret': 'generated-secret'}))
    credential.chmod(0o600)
    checks = []
    calls = []
    def policy():
        checks.append(True)
        if len(checks) == 2:
            (root/'.git').mkdir()
    client = PlaidClient(credential, policy=policy, transport=httpx.MockTransport(
        lambda req: (calls.append(req.url.path) or httpx.Response(200, json={}))))
    with pytest.raises(BankingError, match='private_path_inside_git'):
        client.post('/accounts/get', {})
    assert calls == []


def test_service_refuses_in_git_private_store_before_link_dispatch(banking, tmp_path):
    service, calls = banking
    root = tmp_path/'generated-repo'
    root.mkdir()
    (root/'.git').write_text('gitdir: generated-unread-metadata')
    service.private_dir = root/'private'
    service.transport = httpx.MockTransport(lambda req: (
        calls.append(req.url.path) or httpx.Response(200, json=link_created())))
    with pytest.raises(BankingError, match='private_path_inside_git'):
        service.start_link(member_id=1, browser='generated-browser')
    assert calls == []
    assert not service.private_dir.exists()


@pytest.mark.parametrize('repair', [False, True])
def test_consent_revision_is_checked_inside_the_link_claim_transaction(banking, monkeypatch, repair):
    from contextlib import contextmanager
    service, calls = banking
    ident = connection(service, 'needs_repair') if repair else None
    def provider(req):
        calls.append(req.url.path)
        assert req.url.path in {'/link/token/create', '/link/token/get'}
        return httpx.Response(200, json=link_created() if req.url.path.endswith('create') else link_finished())
    service.transport = httpx.MockTransport(provider)
    started = service.start_link(member_id=1, browser='generated-browser', connection_id=ident)
    original = engine.write_tx
    changed = False
    @contextmanager
    def change_before_begin(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            with original(service.db_path) as conn:
                for granted in (0, 1):
                    conn.execute('INSERT INTO bank_consents(member_id,version,granted) VALUES (1,?,?)',
                                 (CONSENT_VERSION, granted))
        with original(*args, **kwargs) as conn:
            yield conn
    monkeypatch.setattr(engine, 'write_tx', change_before_begin)
    with pytest.raises(BankingError, match='link_consent_changed_start_new_session'):
        service.finish_link(started['session_id'], browser='generated-browser')
    assert calls == ['/link/token/create', '/link/token/get']
    with engine.read_conn(service.db_path) as conn:
        assert conn.execute('SELECT state FROM bank_link_sessions').fetchone()[0] == 'pending'


def test_consent_revision_is_rechecked_after_claim_before_exchange_dispatch(banking, monkeypatch):
    import app.banking.secrets as storage
    service, calls = banking
    def provider(req):
        calls.append(req.url.path)
        assert req.url.path in {'/link/token/create', '/link/token/get'}
        return httpx.Response(200, json=link_created() if req.url.path.endswith('create') else link_finished())
    service.transport = httpx.MockTransport(provider)
    started = service.start_link(member_id=1, browser='generated-browser')
    read = storage.private_read
    def change_after_exchange_credentials(path):
        value = read(path)
        if path == service.credentials_file:
            with engine.read_conn(service.db_path) as conn:
                state = conn.execute('SELECT state FROM bank_link_sessions').fetchone()[0]
            if state == 'exchanging':
                service.consent(1, granted=False, version=CONSENT_VERSION)
                service.consent(1, granted=True, version=CONSENT_VERSION)
        return value
    monkeypatch.setattr(storage, 'private_read', change_after_exchange_credentials)
    for _ in range(2):
        with pytest.raises(BankingError, match='link_outcome_uncertain_do_not_retry'):
            service.finish_link(started['session_id'], browser='generated-browser')
    assert calls == ['/link/token/create', '/link/token/get']


@pytest.mark.parametrize('gate', ['enabled', 'strict_local', 'read_only', 'consent'])
def test_policy_kill_switches_still_block_the_next_sync_page(banking, gate):
    service, calls = banking
    ident = connection(service)
    def provider(req):
        calls.append(req.url.path)
        if gate == 'consent':
            service.consent(1, granted=False, version=CONSENT_VERSION)
        else:
            setattr(service, gate, lambda: gate != 'enabled')
        return httpx.Response(200, json=sync_page(True, 'generated-next'))
    service.transport = httpx.MockTransport(provider)
    with pytest.raises(BankingError, match='egress_blocked|read_only|current_member_consent_required'):
        service.sync(ident)
    assert calls == ['/transactions/sync']


@pytest.mark.parametrize('accounts', [[], [{'account_id': 'generated-account', 'name': 'Generated unknown currency',
    'type': 'depository', 'balances': {}}]])
def test_discovery_preserves_empty_accounts_and_unknown_currency(banking, accounts):
    service, calls = banking
    ident = connection(service)
    service.transport = httpx.MockTransport(lambda req: (
        calls.append(req.url.path) or httpx.Response(200, json={'accounts': accounts})))
    assert service.discover_accounts(ident) == {'status': 'accounts_loaded', 'count': len(accounts)}
    assert calls == ['/accounts/get']
    with engine.read_conn(service.db_path) as conn:
        rows = conn.execute('SELECT currency FROM bank_accounts').fetchall()
        assert [r['currency'] for r in rows] == ([''] if accounts else [])
