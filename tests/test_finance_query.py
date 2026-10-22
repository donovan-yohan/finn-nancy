"""Bounded analysis against generated SQLite; no production DB or credentials."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest

from app.config import Settings, get_settings
from app.db import engine
from app.web.app import create_app


@pytest.fixture
def analysis_client(app_env, monkeypatch):
    monkeypatch.setitem(Settings.model_config, 'env_file', None)
    monkeypatch.setenv('API_TOKEN', 'generated-query-key')
    monkeypatch.setenv('STRICT_LOCAL_MODE', 'true')
    monkeypatch.setenv('PLAID_ENABLED', 'false')
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def auth():
    return {'Authorization': 'Bearer generated-query-key'}


def test_bounded_query_returns_exact_cents_parameterized_rows_and_schema(analysis_client):
    schema = analysis_client.get('/api/finance/schema', headers=auth())
    assert schema.status_code == 200
    tables = schema.json()['relations']
    assert set(tables) == {'analysis_transactions', 'analysis_splits', 'analysis_accounts', 'analysis_categories'}
    assert 'amount_cents' in tables['analysis_transactions']
    assert 'token_ref' not in json.dumps(tables)
    query = '''SELECT t.id, t.amount_cents, a.currency, c.name AS category
        FROM analysis_transactions t JOIN analysis_accounts a ON a.id=t.account_id
        JOIN analysis_splits s ON s.transaction_id=t.id
        JOIN analysis_categories c ON c.id=s.category_id
        WHERE t.id=:id'''
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': query, 'parameters': {'id': 3}})
    assert response.status_code == 200, response.text
    assert response.headers['x-fn-caller'] == 'api'
    value = response.json()
    assert value['columns'] == ['id', 'amount_cents', 'currency', 'category']
    assert value['rows'] == [[3, -16243, 'CAD', 'Groceries']]
    assert value['row_count'] == 1 and value['truncated'] is False


def test_query_api_is_keyed_even_on_trusted_ui(analysis_client):
    assert analysis_client.get('/api/finance/schema').status_code == 401
    assert analysis_client.post('/api/finance/query', json={'sql': 'SELECT 1'}).status_code == 401


@pytest.mark.parametrize('sql', [
    'UPDATE transactions SET amount_cents=0',
    'DELETE FROM transactions',
    'INSERT INTO categories(name,kind) VALUES (\'Forbidden\',\'expense\')',
    'CREATE TABLE forbidden(value)',
    'CREATE TEMP TABLE forbidden(value)',
    'DROP TABLE transactions',
    'ATTACH DATABASE \'other.sqlite\' AS other',
    'DETACH DATABASE main',
    'PRAGMA query_only=OFF',
    'PRAGMA table_info(transactions)',
    'VACUUM',
    'BEGIN',
    'SELECT 1; SELECT 2',
    'WITH x AS (SELECT 1) DELETE FROM transactions',
    'SELECT * FROM settings',
    'SELECT * FROM bank_connections',
    'SELECT * FROM sqlite_master',
    'SELECT * FROM pragma_database_list',
    'SELECT load_extension(\'generated.so\')',
    'SELECT readfile(\'generated.txt\')',
    'SELECT writefile(\'generated.txt\',\'x\')',
    'SELECT * FROM main.transactions',
    'SELECT COUNT(*) FROM transactions',
    'WITH analysis_transactions AS (SELECT * FROM bank_connections) SELECT * FROM analysis_transactions',
    'SELECT randomblob(100000000)',
    'SELECT sqlite_version()',
    'SELECT id AS collision,id AS collision FROM analysis_transactions; DELETE FROM transactions',
])
def test_query_refuses_writes_and_nonfinancial_surfaces(analysis_client, app_env, sql):
    with engine.read_conn(app_env) as conn:
        before = [tuple(r) for r in conn.execute('SELECT * FROM transactions ORDER BY id')]
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': sql})
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'query is not an allowed financial read'}
    with engine.read_conn(app_env) as conn:
        assert [tuple(r) for r in conn.execute('SELECT * FROM transactions ORDER BY id')] == before


@pytest.mark.parametrize('relation,table,columns', [
    ('analysis_transactions', 'transactions', 'id,amount_cents'),
    ('analysis_splits', 'transaction_splits', 'id,amount_cents'),
    ('analysis_accounts', 'accounts', 'id,name'),
    ('analysis_categories', 'categories', 'id,name'),
])
def test_query_cte_cannot_spoof_projection_provenance(analysis_client, relation, table, columns):
    sql = f'WITH {relation} AS (SELECT {columns} FROM main.{table}) SELECT * FROM {relation}'
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': sql})
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'query is not an allowed financial read'}


def test_query_empty_result_headers_obey_output_budget(analysis_client):
    name = 'header_' + 'x' * 10000
    sql = 'WITH x AS (SELECT 1 AS "' + name + '") SELECT ' + ','.join(['x.*'] * 100) + ' FROM x WHERE 0'
    assert len(sql.encode('utf-8')) < 16000
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': sql})
    assert response.status_code == 422, f'Accepted {len(response.content)} response bytes'
    assert response.json() == {'detail': 'query output limit exceeded'}


@pytest.mark.parametrize('body', [
    '{"sql":"SELECT 1","limit":1e999}',
    '{"sql":"SELECT \\ud800"}',
])
def test_query_malformed_json_values_return_sanitized_422(analysis_client, body):
    client = TestClient(analysis_client.app, raise_server_exceptions=False)
    response = client.post('/api/finance/query', headers={**auth(), 'Content-Type': 'application/json'}, content=body)
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'query input is invalid'}


@pytest.mark.parametrize('sql,limit,rows,truncated', [
    ('SELECT 1 AS value WHERE 0', 100, [], False),
    ('SELECT 1 AS value', 100, [[1]], False),
    ('SELECT 1 AS value UNION ALL SELECT 2', 1, [[1]], True),
    ('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10) SELECT x AS value FROM n',
     100, [[i] for i in range(1, 11)], False),
])
def test_query_output_budget_includes_complete_envelope(analysis_client, monkeypatch, sql, limit, rows, truncated):
    from app.api import analysis
    expected = {'columns': ['value'], 'rows': rows, 'row_count': len(rows), 'truncated': truncated}
    size = len(json.dumps(expected, separators=(',', ':')).encode('utf-8'))
    monkeypatch.setattr(analysis, 'MAX_OUTPUT', size - 1)
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': sql, 'limit': limit})
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'query output limit exceeded'}
    # The budget reserves the longer false flag before knowing whether rows truncate.
    monkeypatch.setattr(analysis, 'MAX_OUTPUT', size + int(truncated))
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': sql, 'limit': limit})
    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert len(response.content) <= analysis.MAX_OUTPUT


def test_query_internal_projection_is_not_a_direct_read_surface(analysis_client, monkeypatch):
    from app.api import analysis
    import sqlite3
    internal = {'financial_generated': ('analysis_transactions', 'transactions', ('id', 'amount_cents'))}
    assert analysis._authorize(internal, sqlite3.SQLITE_READ, 'financial_generated', 'id', 'temp', None) == sqlite3.SQLITE_DENY
    assert analysis._authorize(internal, sqlite3.SQLITE_READ, 'financial_generated', '', None, None) == sqlite3.SQLITE_DENY
    assert analysis._authorize(internal, sqlite3.SQLITE_READ, 'transactions', 'id', 'main', 'analysis_transactions') == sqlite3.SQLITE_DENY
    assert analysis._authorize(internal, sqlite3.SQLITE_READ, 'transactions', 'private_payload', 'main', 'financial_generated') == sqlite3.SQLITE_DENY


def test_query_validation_keeps_discoverable_schema_and_other_routes(analysis_client):
    operation = analysis_client.app.openapi()['paths']['/api/finance/query']['post']
    assert operation['requestBody']['content']['application/json']['schema']['$ref'].endswith('/FinancialQuery')
    response = analysis_client.post('/api/finance/category-proposals', headers=auth(), json={'invalid': True})
    assert response.status_code == 422
    assert isinstance(response.json()['detail'], list)


def test_query_row_limit_reports_truncation(analysis_client):
    response = analysis_client.post('/api/finance/query', headers=auth(), json={
        'sql': 'SELECT id FROM analysis_transactions ORDER BY id', 'limit': 2})
    assert response.status_code == 200, response.text
    assert response.json() == {'columns': ['id'], 'rows': [[1], [2]], 'row_count': 2, 'truncated': True}


def test_query_readonly_mode_still_allows_analysis(analysis_client, monkeypatch):
    monkeypatch.setenv('READ_ONLY', 'true')
    get_settings.cache_clear()
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': 'SELECT COUNT(*) AS n FROM analysis_transactions'})
    assert response.status_code == 200, response.text
    assert response.json()['rows'] == [[20]]


def test_query_allows_cte_and_grouping_without_sign_based_accounting(analysis_client):
    response = analysis_client.post('/api/finance/query', headers=auth(), json={
        'sql': '''WITH monthly AS (SELECT flow_kind, amount_cents FROM analysis_transactions
            WHERE posted_on >= :start AND posted_on < :end)
            SELECT flow_kind, SUM(amount_cents) AS signed_cents FROM monthly
            GROUP BY flow_kind ORDER BY flow_kind''',
        'parameters': {'start': '2026-01-01', 'end': '2026-02-01'}})
    assert response.status_code == 200, response.text
    assert response.json()['rows'] == [['income', 660000], ['internal_transfer', 0], ['purchase', -238692]]


def test_query_expensive_work_is_interrupted_without_raw_sql_errors(analysis_client):
    response = analysis_client.post('/api/finance/query', headers=auth(), json={
        'sql': '''WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000000)
                  SELECT sum(x) FROM n'''})
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'query work limit exceeded'}


def test_query_output_is_bounded(analysis_client, app_env):
    with engine.write_tx(app_env) as conn:
        conn.execute('UPDATE transactions SET description=? WHERE id=1', ('x' * 300000,))
    response = analysis_client.post('/api/finance/query', headers=auth(), json={
        'sql': 'SELECT a.description,b.description FROM analysis_transactions a CROSS JOIN analysis_transactions b WHERE a.id=1 AND b.id=1'})
    assert response.status_code == 422, response.text
    assert response.json() == {'detail': 'query output limit exceeded'}


@pytest.mark.parametrize('body', [
    {'sql': ''}, {'sql': 'SELECT 1', 'limit': 0}, {'sql': 'SELECT 1', 'limit': 501},
    {'sql': 'SELECT 1', 'limit': True}, {'sql': 'SELECT 1', 'actor': 'forged'},
    {'sql': 'SELECT :x', 'parameters': {'x': {'nested': True}}},
])
def test_query_input_contract_is_strict(analysis_client, body):
    assert analysis_client.post('/api/finance/query', headers=auth(), json=body).status_code == 422


def test_query_service_bounds_parameters_and_capacity_without_mutation(analysis_client):
    from app.api import analysis
    for parameters in ({'value': 2**100}, {'value': 'x' * 10001}):
        with pytest.raises(ValueError, match='query parameter is invalid'):
            analysis.query(sql='SELECT :value', parameters=parameters)
    acquired = []
    try:
        for _ in range(4):
            assert analysis.CAPACITY.acquire(blocking=False)
            acquired.append(True)
        response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': 'SELECT 1'})
        assert response.status_code == 422
        assert response.json() == {'detail': 'query capacity exceeded'}
    finally:
        for _ in acquired:
            analysis.CAPACITY.release()
    assert analysis.query(sql='SELECT 1')['rows'] == [[1]]


def test_query_json_finiteness_and_duplicate_columns_are_unambiguous(analysis_client):
    response = analysis_client.post('/api/finance/query', headers=auth(), json={
        'sql': 'SELECT id AS repeated, amount_cents AS repeated FROM analysis_transactions WHERE id=1'})
    assert response.status_code == 200
    assert response.json()['columns'] == ['repeated', 'repeated']
    assert response.json()['rows'] == [[1, 520000]]
    response = analysis_client.post('/api/finance/query', headers=auth(), json={'sql': 'SELECT 1e999'})
    assert response.status_code == 422
    assert response.json() == {'detail': 'query result is not JSON financial data'}


def test_installed_remote_cli_queries_actual_app_without_local_database(analysis_client, tmp_path):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import threading
    import time
    import uvicorn

    root = Path(__file__).resolve().parents[1]
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    origin = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(analysis_client.app, lifespan='off',
        access_log=False, log_config=None, log_level=None))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        guard = tmp_path / 'isolation'
        guard.mkdir()
        (guard / 'sitecustomize.py').write_text(
            'from pydantic_settings.sources import DotEnvSettingsSource\n'
            'DotEnvSettingsSource._read_env_files = lambda self: {}\n')
        env = {name: os.environ[name] for name in ('PATH', 'HOME', 'TMPDIR') if name in os.environ}
        env.update(FN_API_URL=origin, FN_API_KEY='generated-query-key',
                   PYTHONPATH=str(guard) + os.pathsep + str(root))
        body = {'sql': 'SELECT id,amount_cents FROM analysis_transactions WHERE id=:id',
                'parameters': {'id': 3}}
        result = subprocess.run([str(root / '.venv/bin/fn'), 'api', 'POST',
            '/api/finance/query', '--data', '-'], input=json.dumps(body), env=env,
            cwd=tmp_path, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        value = json.loads(result.stdout)
        assert value['caller'] == 'api' and value['request_id'] and value['status_code'] == 200
        assert value['data'] == {'columns': ['id', 'amount_cents'], 'rows': [[3, -16243]],
                                 'row_count': 1, 'truncated': False}
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
