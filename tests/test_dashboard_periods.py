"""Period-scoped dashboard behavior over generated accounting fixtures."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import engine, repo_ledger, views


def _transactions(db):
    with engine.write_tx(db) as conn:
        conn.execute("INSERT INTO accounts(id,name,kind,currency) VALUES (1,'Generated account','chequing','CAD')")
        conn.execute("INSERT INTO categories(id,name,kind) VALUES (1,'Uncategorized','expense')")
        ids = []
        for date, amount, flow, key in (
            ('2026-05-01', -1000, 'purchase', 'older'),
            ('2026-06-01', 5000, 'income', 'income'),
            ('2026-06-02', -1200, 'purchase', 'purchase'),
            ('2026-06-03', -300, 'unknown', 'ambiguous'),
        ):
            tid = repo_ledger.insert_transaction(conn, account_id=1, posted_on=date,
                description=f'Generated {key}', amount_cents=amount, source='manual',
                external_id=f'generated-period:{key}', flow_kind=flow,
                counterparty='', source_document_id=None, source_confidence=1.0)
            assert tid is not None
            repo_ledger.insert_split(conn, transaction_id=tid, category_id=1, amount_cents=amount)
            ids.append(tid)
        return ids


def test_dashboard_defaults_to_latest_month_not_lifetime(empty_db):
    ids = _transactions(empty_db)
    with engine.read_conn(empty_db) as conn:
        context = views.dashboard_context(conn, 6, empty_db)
    assert context['selected_month'] == '2026-06'
    assert context['months'] == ['2026-06', '2026-05']
    assert context['stats']['money_in_cents'] == 5000
    assert context['stats']['money_out_cents'] == 1200
    assert context['stats']['net_cents'] == 3800
    assert context['semantic_review_count'] == 1
    assert {t['id'] for t in context['transactions']} == set(ids[1:])
    assert context['period_date_from'] == '2026-06-01'
    assert context['period_date_to'] == '2026-06-30'


def test_dashboard_explicit_month_scopes_rows_review_and_totals(empty_db):
    ids = _transactions(empty_db)
    with engine.read_conn(empty_db) as conn:
        context = views.dashboard_context(conn, 6, empty_db, month='2026-05')
    assert context['stats']['money_in_cents'] == 0
    assert context['stats']['money_out_cents'] == 1000
    assert context['semantic_review_count'] == 0
    assert [t['id'] for t in context['transactions']] == ids[:1]
    assert context['transactions'][0]['currency'] == 'CAD'
    assert len(context['cashflow']) == 2


def test_dashboard_empty_and_missing_period_are_honest(empty_db):
    with engine.read_conn(empty_db) as conn:
        context = views.dashboard_context(conn, 6, empty_db)
        missing = views.dashboard_context(conn, 6, empty_db, month='2024-02')
    assert context['selected_month'] == ''
    assert context['transactions'] == []
    assert missing['selected_month'] == '2024-02'
    assert missing['period_date_to'] == '2024-02-29'
    assert missing['stats']['net_cents'] == 0


@pytest.mark.parametrize('month', ['2026-13', '2026-1', '2026-06-01', 'garbage'])
def test_dashboard_rejects_bad_period(empty_db, month):
    with engine.read_conn(empty_db) as conn:
        with pytest.raises(ValueError, match='month'):
            views.dashboard_context(conn, 6, empty_db, month=month)


def test_dashboard_http_validates_period(empty_db, monkeypatch):
    monkeypatch.setenv('DB_PATH', empty_db)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.web.app import create_app
    client = TestClient(create_app())
    assert client.get('/?month=2026-99').status_code == 400
    assert client.get('/?month=2024-02').status_code == 200
    get_settings.cache_clear()
