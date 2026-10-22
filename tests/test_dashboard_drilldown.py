"""Dashboard links must resolve to the exact accounting contributors."""
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.api import service
from app.db import engine, repo_merchant_knowledge as knowledge, views
from app.db.repo_merchant_knowledge import Evidence


def _category_contributors(db):
    ids = []
    with engine.write_tx(db) as conn:
        for i in range(54):
            amount = -2200 if i == 0 else -1200 if i == 1 else -100
            key = f'Generated contributor {i:03}'
            tid = conn.execute("""INSERT INTO transactions(account_id,posted_on,description,
                counterparty,amount_cents,source,external_id,flow_kind)
                VALUES (1,'2026-06-14',?,?,?,'sample',?,'purchase')""",
                (key, key, amount, key)).lastrowid
            assert tid is not None
            sid = conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,?)',
                               (tid, -800 if i == 1 else amount)).lastrowid
            knowledge.confirm_category(conn, descriptor=key, category_id=4,
                scope=knowledge.scope_for_transaction(conn, tid), operation_key=f'contributor-{i}',
                actor='operator:test', reason='Generated approval',
                evidence=Evidence(transaction_id=tid, transaction_split_id=sid))
            if i == 1:
                sibling = conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,5,-400)', (tid,)).lastrowid
                knowledge.confirm_category(conn, descriptor=key, category_id=5,
                    scope=knowledge.scope_for_transaction(conn, tid), operation_key='contributor-sibling',
                    actor='operator:test', reason='Generated sibling approval',
                    evidence=Evidence(transaction_id=tid, transaction_split_id=sibling))
            ids.append(tid)
        for flow in ('purchase', 'unknown'):
            tid = conn.execute("""INSERT INTO transactions(account_id,posted_on,description,
                amount_cents,source,external_id,flow_kind) VALUES (1,'2026-06-14',
                'Generated contributor excluded',-500,'sample',?,?)""", (flow, flow)).lastrowid
            conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,-500)', (tid,))
    return ids


def test_category_drilldown_accepted_contributors_preserve_exact_split_and_transaction(app_env):
    ids = _category_contributors(app_env)
    from app.web.app import create_app
    client = TestClient(create_app())
    home = client.get('/?month=2026-06')
    match = re.search(r'href="([^"]*category_id=4[^"]*)"', home.text)
    assert match is not None
    link = unescape(match[1])
    params = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
    assert params['category_resolution'] == 'accepted'
    with engine.read_conn(app_env) as conn:
        result = views.activity_results(conn, category_id=params['category_id'],
            date_from=params['date_from'], date_to=params['date_to'],
            category_resolution=params['category_resolution'], limit=100)
        assert {r['id'] for r in result['transactions']} == set(ids)
        amounts = {r['id']: r for r in result['transactions']}
        assert amounts[ids[0]]['accepted_category_amount_cents'] == -2200
        assert amounts[ids[1]]['accepted_category_amount_cents'] == -800
        assert amounts[ids[1]]['amount_cents'] == -1200
        assert sum(r['accepted_category_amount_cents'] for r in result['transactions']) == -8200
        ordinary = views.activity_results(conn, category_id=4, date_from='2026-06-01', limit=100)
        assert len(ordinary['transactions']) == 56
    initial = client.get(link)
    assert 'name="category_resolution" value="accepted"' in initial.text
    assert 'Accepted category contributions' in initial.text
    params['q'] = 'Generated contributor'
    fragment = client.get('/transactions', params=params, headers={'HX-Request': 'true'})
    match = re.search(r'hx-get="([^"]+)"', fragment.text)
    assert match is not None
    cursor = unescape(match[1])
    for key in ('q=Generated+contributor', 'category_id=4', 'date_from=2026-06-01',
                'date_to=2026-06-30', 'category_resolution=accepted'):
        assert key in cursor
    tail = client.get(cursor)
    assert 'id="tx-list"' not in tail.text
    assert 'Category contribution' in tail.text
    assert '-$8.00 CAD' in tail.text and '-$12.00 CAD' in tail.text
    assert set(map(int, re.findall(r'id="transaction-(\d+)"', fragment.text + tail.text))) == set(ids)
    for route in ('/activity', '/transactions'):
        assert client.get(route, params={'category_resolution': 'bogus'}).status_code == 400
        assert client.get(route, params={'category_resolution': 'accepted'}).status_code == 400


def test_income_drilldown_includes_interest_not_unknown_or_transfers(app_env):
    ids = []
    for flow, amount, key in [('income', 5000, 'pay'), ('interest', 50, 'interest'),
                              ('unknown', -70, 'unknown'), ('internal_transfer', 100, 'transfer')]:
        row = service.record_transaction(amount_cents=amount, posted_on='2026-06-14',
            description='Generated '+key, flow_kind=flow, external_id='drill:'+key)
        ids.append(row['transaction_id'])
    from app.web.app import create_app
    client = TestClient(create_app())
    result = client.get('/transactions', params={'date_from': '2026-06-01', 'date_to': '2026-06-30',
                                                'report_bucket': 'income'})
    assert result.status_code == 200
    shown = set(map(int, re.findall(r'id="transaction-(\d+)"', result.text)))
    assert shown == set(ids[:2])


def test_spending_drilldown_does_not_show_income_or_unknown(app_env):
    ids = []
    for flow, amount, key in [('purchase', -1500, 'purchase'), ('fee', -50, 'fee'),
                              ('unknown', -90, 'unknown'), ('income', 100, 'income')]:
        row = service.record_transaction(amount_cents=amount, posted_on='2026-06-14',
            description='Generated '+key, flow_kind=flow, external_id='spend-drill:'+key)
        ids.append(row['transaction_id'])
    from app.web.app import create_app
    client = TestClient(create_app())
    result = client.get('/transactions', params={'date_from': '2026-06-01', 'date_to': '2026-06-30',
                                                'report_bucket': 'expense'})
    assert result.status_code == 200
    shown = set(map(int, re.findall(r'id="transaction-(\d+)"', result.text)))
    assert shown == set(ids[:2])
    assert client.get('/activity?report_bucket=not-a-bucket').status_code == 400
