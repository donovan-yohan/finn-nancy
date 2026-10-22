"""Generated full household recovery: ledger, immutable originals and token refs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import socket

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import engine, migrate, repo_documents
from app.banking.service import BankingService, CONSENT_VERSION
from scripts.backup_manifest_lib import create_manifest, verify_manifest


def test_generated_household_restore_preserves_data_evidence_and_token_references(tmp_path, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError('restore rehearsal cannot open network connections')

    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    source = tmp_path / 'source'
    source.mkdir()
    database = source / 'ledger.sqlite'
    migrate.seed_sample(database)
    data = source / 'data'
    original = b'Generated immutable household receipt for recovery.'
    digest = hashlib.sha256(original).hexdigest()
    relative = Path('originals/blobs') / digest[:2] / digest[2:4] / (digest + '.txt')
    (data / relative).parent.mkdir(parents=True)
    (data / relative).write_bytes(original)
    banking = BankingService(db_path=database, private_dir=source / 'private',
        credentials_file=None, origin='', completion_uri='', enabled=lambda: False,
        strict_local=lambda: True, read_only=lambda: False, home_currency='CAD')
    banking.consent(1, granted=True, version=CONSENT_VERSION)
    reference = banking.store.put('generated-restored-access-token')
    with engine.write_tx(database) as conn:
        document = repo_documents.insert_source_document(conn, kind='receipt',
            original_name='generated-recovery-receipt.txt', storage_ref=relative.as_posix(),
            sha256=digest, mime_type='text/plain', status='processed')
        conn.execute('UPDATE transactions SET source_document_id=? WHERE id=3', (document,))
        conn.execute("INSERT INTO bank_connections(member_id,item_id,token_ref,consent_version,state) VALUES (1,'generated-recovery-item',?,?,'active')", (reference, CONSENT_VERSION))
        expected = {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                    for table in ('accounts', 'transactions', 'transaction_splits', 'source_documents',
                                  'bank_connections', 'bank_consents', 'transaction_relationships')}
        identity = migrate.read_database_identity(conn)
    # WAL may still contain recent writes: exercise the real online backup entrypoint.
    backup = tmp_path / 'backup'
    backup.mkdir(mode=0o700)
    receipt = create_manifest(database, backup / 'ledger.sqlite', backup / 'manifest.json')
    verified, verified_database = verify_manifest(backup / 'manifest.json')
    assert verified == receipt
    shutil.copytree(data, backup / 'data')
    shutil.copytree(source / 'private', backup / 'private')
    restored = tmp_path / 'restored'
    restored.mkdir(mode=0o700)
    shutil.copy2(verified_database, restored / 'ledger.sqlite')
    shutil.copytree(backup / 'data', restored / 'data')
    shutil.copytree(backup / 'private', restored / 'private')
    assert migrate.init_db(restored / 'ledger.sqlite') == []
    with engine.read_conn(restored / 'ledger.sqlite') as conn:
        for table, rows in expected.items():
            assert [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')] == rows
        assert migrate.read_database_identity(conn) == identity
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        for row in conn.execute('SELECT storage_ref,sha256 FROM source_documents'):
            assert hashlib.sha256((restored / 'data' / row['storage_ref']).read_bytes()).hexdigest() == row['sha256']
    for name, value in {
        'DB_PATH': str(restored / 'ledger.sqlite'), 'DATA_DIR': str(restored / 'data'),
        'PLAID_PRIVATE_DIR': str(restored / 'private'), 'PLAID_ENABLED': 'false',
        'STRICT_LOCAL_MODE': 'true', 'READ_ONLY': 'true', 'API_TOKEN': 'generated-recovery-api-key',
        'HERMES_CHAT_URL': '', 'INBOX_DIR': '', 'TELEGRAM_BOT_TOKEN': '',
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        from app.web import app as web_app

        async def deny_worker(*args, **kwargs):
            raise AssertionError('restore rehearsal cannot start workers')

        monkeypatch.setattr(web_app, 'run_worker', deny_worker)
        create_app = web_app.create_app
        application = create_app()
        service = application.state.banking_service
        # No restored secret store or workers opened merely by constructing the app.
        assert service._store is None
        client = TestClient(application)
        for route in ('/healthz', '/version', '/', '/activity', '/connections', '/bank-feed'):
            response = client.get(route)
            assert response.status_code == 200, (route, response.text)
        version = client.get('/version').json()
        assert version['database_identity'] == identity
        assert version['schema_digest'] == receipt['candidate']['schema_digest']
        assert version['migration_sequence_matches'] is True
        assert version['applied_migration_digest'] == receipt['candidate']['migration_digest']
        headers = {'Authorization': 'Bearer generated-recovery-api-key'}
        assert client.get('/api/finance/transactions/3').status_code == 401
        detail = client.get('/api/finance/transactions/3', headers=headers)
        assert detail.status_code == 200
        assert detail.json()['amount_cents'] == -16243
        assert client.get('/connections/data/overview', headers=headers).json()['network_allowed'] is False
        assert service._store is None
        # Reading a generated opaque reference explicitly proves matching token custody.
        assert service.store.get(reference) == 'generated-restored-access-token'
        denied = client.post('/connections/data/sync', headers=headers, json={'connection_id': 1})
        assert denied.status_code == 409
        assert denied.json()['detail'] == 'read_only'
        assert (restored / 'data' / relative).read_bytes() == original
        # Source and backup are retained, not moved or rewound after destination writes.
        assert database.exists() and verified_database.exists()
        assert (source / 'data' / relative).read_bytes() == original
        (tmp_path / 'restore-receipt.json').write_text(json.dumps({
            'generated_only': True, 'database_identity_preserved': True,
            'original_document_count': 1, 'matching_private_reference_count': 1,
            'account_transaction_split_rows_preserved': True, 'source_retained': True,
            'integrity': 'ok', 'foreign_keys': 'ok', 'restored_route_count': 6,
            'read_only_sync_refused': True, 'workers_or_provider_started': False,
        }, indent=2) + '\n')
    finally:
        get_settings.cache_clear()
