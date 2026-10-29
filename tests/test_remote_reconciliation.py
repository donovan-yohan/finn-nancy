"""First-import acceptance through the installed CLI and real loopback HTTP.

Only setup/readback touches generated SQLite. Every workflow command uses fn api.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import httpx
import pytest
import uvicorn

from app.config import Settings, get_settings
from app.db import engine, repo_documents, repo_ledger

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def remote_household(empty_db, tmp_path, monkeypatch):
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    credential = tmp_path / 'generated-credentials.json'
    credential.write_text(json.dumps({'client_id': 'generated-client', 'secret': 'generated-secret'}))
    credential.chmod(0o600)
    values = {
        'DB_PATH': empty_db, 'DATA_DIR': str(tmp_path / 'data'),
        'API_TOKEN': 'generated-reconciliation-key', 'PLAID_ENABLED': 'true',
        'STRICT_LOCAL_MODE': 'false', 'READ_ONLY': 'false',
        'PLAID_CREDENTIALS_FILE': str(credential), 'PLAID_PRIVATE_DIR': str(tmp_path / 'private'),
    }
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    origin = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    values.update(PLAID_ORIGIN=origin, PLAID_COMPLETION_URI=origin + '/connections')
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    from app.web.app import create_app
    from app.banking.service import CONSENT_VERSION
    app = create_app()
    service = app.state.banking_service
    original = b'Generated immutable original receipt evidence.'
    document_file = tmp_path / 'original-receipt.txt'
    document_file.write_bytes(original)
    with engine.write_tx(empty_db) as conn:
        account = conn.execute("INSERT INTO accounts(name,kind,currency) VALUES ('Generated existing account','chequing','CAD')").lastrowid
        category = conn.execute("INSERT INTO categories(name,kind,brand_owner,color) VALUES ('Generated Meals','expense','nancy','#bc4700')").lastrowid
        uncategorized = repo_ledger.ensure_uncategorized(conn)
        doc = repo_documents.insert_source_document(conn, kind='receipt', original_name='generated-receipt.txt',
            storage_ref=str(document_file), sha256=hashlib.sha256(original).hexdigest(), mime_type='text/plain', status='processed')
        ids = {}
        for key, amount, day in [('overlap', -1234, '2026-06-12'), ('legacy_only', -2222, '2025-02-03'),
                                 ('equal_a', -777, '2026-06-14'), ('equal_b', -777, '2026-06-14')]:
            tid = repo_ledger.insert_transaction(conn, account_id=account, posted_on=day,
                description='Generated ' + key, counterparty='Generated merchant', amount_cents=amount,
                source='statement' if key == 'overlap' else 'manual', external_id='generated-' + key,
                source_document_id=doc if key == 'overlap' else None, source_confidence=1, flow_kind='purchase')
            assert tid is not None
            repo_ledger.insert_split(conn, transaction_id=tid, category_id=uncategorized, amount_cents=amount)
            if key == 'overlap':
                conn.execute("UPDATE transactions SET recon_status='cleared',cleared_on='2026-06-30' WHERE id=?", (tid,))
            ids[key] = tid
        baseline = [dict(row) for row in conn.execute('SELECT * FROM transactions ORDER BY id')]
        baseline_splits = [dict(row) for row in conn.execute('SELECT * FROM transaction_splits ORDER BY id')]
        document = repo_documents.get_document(conn, doc)
        assert document is not None
        baseline_document = dict(document)
    member_id = service.add_member('Generated reconciliation member')['member_id']
    service.consent(member_id, granted=True, version=CONSENT_VERSION)
    token_ref = service.store.put('generated-reconciliation-access')
    with engine.write_tx(empty_db) as conn:
        connection = conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) VALUES (?,'generated-existing-item',?,?,'active')", (member_id, token_ref, CONSENT_VERSION)).lastrowid
        conn.execute("INSERT INTO bank_accounts(connection_id,provider_account_id,name,currency,kind) VALUES (?,'generated-provider-account','Generated account','CAD','chequing')", (connection,))

    def observation(ident, amount, **updates):
        return {'transaction_id': ident, 'account_id': 'generated-provider-account', 'amount': amount,
                'iso_currency_code': 'CAD', 'date': '2026-06-14', 'pending': False,
                'name': 'Generated ' + ident, 'merchant_name': 'Generated merchant', **updates}

    rows = [observation('overlap', 12.34, date='2026-06-12'), observation('missing', 56.78),
            observation('ambiguous', 7.77), observation('pending', 3.50, pending=True),
            observation('foreign', 9, iso_currency_code='USD')]
    dispatched = []

    def provider(request):
        assert request.url.host == 'production.plaid.com'
        assert request.url.path == '/transactions/sync'
        dispatched.append(request.url.path)
        return httpx.Response(200, json={'added': rows, 'modified': [], 'removed': [],
            'has_more': False, 'next_cursor': 'generated-history-window'})

    service.transport = httpx.MockTransport(provider)
    connect = socket.socket.connect

    def loopback_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            assert ipaddress.ip_address(address[0]).is_loopback, 'non-loopback socket forbidden'
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', loopback_only)
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', access_log=False,
                                         log_config=None, log_level=None))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        yield {'origin': origin, 'db': empty_db, 'account': account, 'category': category,
               'connection': connection, 'ids': ids, 'baseline': baseline,
               'baseline_splits': baseline_splits, 'baseline_document': baseline_document, 'doc': doc,
               'original': original, 'document_file': document_file, 'dispatched': dispatched}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        get_settings.cache_clear()
        assert not thread.is_alive(), 'owned acceptance server failed to stop'


def test_remote_first_import_reconciles_overlap_without_losing_legacy_history(remote_household, tmp_path):
    fixture = remote_household
    # Remote process receives no DB_PATH, DATA_DIR or server/provider credentials.
    guard = tmp_path / 'isolation'
    guard.mkdir()
    (guard / 'sitecustomize.py').write_text(
        'from pydantic_settings.sources import DotEnvSettingsSource\n'
        'DotEnvSettingsSource._read_env_files = lambda self: {}\n')
    env = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR') if name in os.environ}
    env.update(FN_API_URL=fixture['origin'], FN_API_KEY='generated-reconciliation-key',
               PYTHONPATH=str(guard) + os.pathsep + str(ROOT))
    exchanges = []

    def api(method, endpoint, body=None, *, form=False, status=200):
        args = [str(ROOT / '.venv/bin/fn'), 'api', method, endpoint]
        if body is not None:
            args += ['--data', '-']
        if form:
            args += ['--form']
        result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
            capture_output=True, text=True, env=env, cwd=tmp_path, timeout=15)
        assert result.returncode == (1 if status >= 400 else 0), result.stdout + result.stderr
        envelope = json.loads(result.stdout)
        assert envelope['status_code'] == status, envelope
        assert envelope['caller'] == 'api' and envelope['request_id']
        exchanges.append({'method': method, 'endpoint': endpoint, 'status': status,
                          'request_id': envelope['request_id']})
        return envelope['data']

    overview = api('GET', '/connections/data/overview')
    assert any(row['id'] == fixture['account'] for row in overview['accounts'])
    api('POST', '/connections/data/bind', {'connection_id': fixture['connection'],
        'provider_account_id': 'generated-provider-account', 'account_id': fixture['account'],
        'included': True, 'ownership': 'shared', 'role': 'primary', 'expected_revision': 0})
    api('POST', '/connections/data/sync', {'connection_id': fixture['connection']})
    assert fixture['dispatched'] == ['/transactions/sync']
    with engine.read_conn(fixture['db']) as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM transactions ORDER BY id')] == fixture['baseline']
    feed = api('GET', '/connections/data/feed')['rows']
    assert len(feed) == 5
    plans = {api('GET', f'/connections/data/plan/{row["id"]}')['subject']['provider_transaction_id']: row['id'] for row in feed}

    def plan(key):
        return api('GET', f'/connections/data/plan/{plans[key]}')

    def decision(key, p, operation, action, *, status=200, **extra):
        return api('POST', f'/connections/data/decision/{plans[key]}', {
            'fingerprint': p['fingerprint'], 'operation_key': operation, 'action': action,
            'reason': 'Generated explicit initial-history review', **extra}, status=status)

    missing = plan('missing')
    assert missing['candidates'] == []
    # Even an equivalent new binding revision invalidates an old reviewed plan.
    api('POST', '/connections/data/bind', {'connection_id': fixture['connection'],
        'provider_account_id': 'generated-provider-account', 'account_id': fixture['account'],
        'included': True, 'ownership': 'shared', 'role': 'primary', 'expected_revision': 1})
    stale = decision('missing', missing, 'generated:stale', 'new', flow_kind='purchase', status=409)
    assert stale['detail'] == 'stale_review_plan'
    missing = plan('missing')
    overlap = plan('overlap')
    assert [row['id'] for row in overlap['candidates']] == [fixture['ids']['overlap']]
    matched = decision('overlap', overlap, 'generated:overlap', 'match', transaction_id=fixture['ids']['overlap'])
    assert matched['status'] == 'matched'
    assert decision('overlap', overlap, 'generated:overlap', 'match', transaction_id=fixture['ids']['overlap']) == matched
    booked = decision('missing', missing, 'generated:missing', 'new', flow_kind='purchase')
    assert booked['status'] == 'booked'
    assert decision('missing', missing, 'generated:missing', 'new', flow_kind='purchase') == booked
    conflict = decision('missing', missing, 'generated:missing', 'exclude', status=409)
    assert conflict['detail'] == 'operation_key_conflict'
    ambiguity = plan('ambiguous')
    assert {row['id'] for row in ambiguity['candidates']} == {fixture['ids']['equal_a'], fixture['ids']['equal_b']}
    refusal = decision('ambiguous', ambiguity, 'generated:ambiguous', 'new', flow_kind='purchase', status=409)
    assert refusal['detail'] == 'candidate_acknowledgement_required'
    for key, blocker in [('pending', 'pending'), ('foreign', 'currency_review')]:
        current = plan(key)
        assert blocker in current['blockers']
        assert decision(key, current, 'generated:' + key, 'new', flow_kind='purchase', status=409)['detail'] == blocker

    tid = booked['transaction_id']
    before = api('GET', f'/api/finance/transactions/{tid}')
    categories = api('GET', '/api/finance/categories')['rows']
    assert any(row['id'] == fixture['category'] for row in categories)
    proposal = api('POST', '/api/finance/category-proposals', {
        'operation_key': 'generated:initial-category', 'transaction_id': tid,
        'split_id': before['splits'][0]['id'], 'to_category_id': fixture['category'],
        'expected_state_digest': before['state_digest'], 'rationale': 'Generated reviewed missing meal'})
    api('POST', f'/actions/{proposal["id"]}/approve', form=True, status=303)
    approved = api('GET', f'/api/finance/transactions/{tid}')
    assert approved['splits'][0]['category_id'] == fixture['category']
    assert approved['amount_cents'] == -5678
    report = api('GET', '/api/reports/monthly_spend_by_category?month=2026-06')
    assert [(row['category_id'], row['amount_cents']) for row in report['rows']] == [(fixture['category'], -5678)]
    api('POST', f'/actions/{proposal["id"]}/revert', form=True, status=303)
    restored = api('GET', f'/api/finance/transactions/{tid}')
    assert restored['splits'] == before['splits'] and restored['amount_cents'] == before['amount_cents']
    audit = api('GET', f'/api/finance/proposals/{proposal["id"]}')['audit']
    assert [row['to_status'] for row in audit] == ['proposed', 'approved', 'reverted']
    unresolved = {row['id'] for row in api('GET', '/connections/data/feed')['rows'] if row['needs_review']}
    assert plans['ambiguous'] in unresolved
    ledger = api('GET', '/api/finance/transactions?limit=50')['rows']
    assert len(ledger) == 5
    assert fixture['ids']['legacy_only'] in {row['id'] for row in ledger}
    with engine.read_conn(fixture['db']) as conn:
        original_ids = [row['id'] for row in fixture['baseline']]
        assert [dict(row) for row in conn.execute('SELECT * FROM transactions WHERE id IN (?,?,?,?) ORDER BY id', original_ids)] == fixture['baseline']
        assert [dict(row) for row in conn.execute('SELECT * FROM transaction_splits WHERE transaction_id IN (?,?,?,?) ORDER BY id', original_ids)] == fixture['baseline_splits']
        document = repo_documents.get_document(conn, fixture['doc'])
        assert document is not None
        assert dict(document) == fixture['baseline_document']
        assert conn.execute('SELECT COUNT(*) FROM bank_associations').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM bank_operations').fetchone()[0] == 2
        assert conn.execute('SELECT SUM(amount_cents) FROM transaction_splits WHERE transaction_id=?', (tid,)).fetchone()[0] == -5678
        assert conn.execute('SELECT sha256 FROM source_documents WHERE id=?', (fixture['doc'],)).fetchone()[0] == hashlib.sha256(fixture['original']).hexdigest()
    assert fixture['document_file'].read_bytes() == fixture['original']
    (tmp_path / 'remote-reconciliation-receipt.json').write_text(json.dumps({
        'remote_cli_exchanges': exchanges, 'final_transactions': len(ledger),
        'preserved_original_transactions': len(fixture['baseline']),
        'new_bookings': 1, 'associations': 2, 'ambiguous_subject_preserved': True,
        'original_evidence_unchanged': True, 'category_apply_undo_report_verified': True,
    }, indent=2) + '\n')
