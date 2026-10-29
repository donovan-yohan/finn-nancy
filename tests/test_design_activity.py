"""Generated-only Hotate Activity and Home presentation contracts."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def client():
    from app.web.app import create_app
    return TestClient(create_app())


@pytest.mark.parametrize("route", ["/", "/activity", "/txn/new", "/txn/3/edit"])
def test_surface_uses_installed_foundation_without_legacy_css(app_env, route):
    response = client().get(route)
    assert response.status_code == 200
    assert '/static/foundation/vendor/bootstrap.min.css' in response.text
    assert '/static/foundation/activity.css' in response.text
    assert '/static/app.css' not in response.text
    assert len(re.findall(r'<h1\b', response.text)) == 1


def test_filter_labels_and_native_toolbar_preserve_query_contract(app_env):
    text = client().get('/activity?q=weekly&category_id=4&account_id=1&flow_kind=purchase&date_from=2026-01-01&date_to=2026-06-30&report_bucket=expense&category_resolution=accepted&semantic_review=1').text
    for field in ('category_id', 'account_id', 'flow_kind', 'date_from', 'date_to'):
        assert f'for="filter-{field}"' in text
        assert f'id="filter-{field}"' in text
    assert 'hx-target="#tx"' in text
    assert 'hx-get="/transactions"' in text
    assert 'name="semantic_review" value="1"' in text
    assert 'name="report_bucket" value="expense"' in text
    assert 'name="category_resolution" value="accepted"' in text
    assert '<details class="tx-filter-details" open>' in text
    assert 'id="activity-read-error"' in text
    assert 'Activity could not load.' in text


def test_editor_amount_is_exact_even_beyond_float_precision(app_env):
    from app.db import engine
    with engine.write_tx(app_env) as conn:
        conn.execute('UPDATE transactions SET amount_cents=-9007199254740993 WHERE id=3')
        conn.execute('UPDATE transaction_splits SET amount_cents=-9007199254740993 WHERE transaction_id=3')
    text = client().get('/txn/3/edit').text
    assert 'name="amount" value="-90071992547409.93"' in text
    assert 'for="txn-amount"' in text


def test_empty_and_filtered_states_are_distinct(empty_db, monkeypatch):
    monkeypatch.setenv('DB_PATH', empty_db)
    get_settings.cache_clear()
    first = client().get('/activity').text
    assert 'No activity yet.' in first
    assert 'href="/upload"' in first
    filtered = client().get('/activity?q=missing').text
    assert 'No matching activity.' in filtered
    assert 'Try another merchant or clear your filters.' in filtered


def test_home_has_period_linked_neutral_totals_and_native_history(app_env):
    text = client().get('/?month=2026-03').text
    assert '<h1>Home</h1>' in text
    for scope in ('income', 'expense'):
        assert f'date_from=2026-03-01&amp;date_to=2026-03-31&amp;report_bucket={scope}' in text
    assert 'Monthly cashflow' in text
    assert '<details' in text
    assert 'stat-number positive' not in text
    assert 'stat-number negative' not in text


def test_native_row_summary_uses_canonical_amount_currency_and_review(app_env):
    text = client().get('/transactions?q=weekly').text
    assert '<details class="tx-row"' in text
    assert 'class="tx-summary' in text
    assert '-$162.43' in text
    assert 'CAD' in text
    assert 'id="transaction-3"' in text
    assert 'data-why-url="/chat?why=3"' in text
    assert 'Accounting flow' in text
    assert 'href="/txn/3/edit"' in text


def test_unknown_currency_and_flow_stay_visible_in_scan_row(app_env):
    from app.db import engine
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE accounts SET currency='' WHERE id=(SELECT account_id FROM transactions WHERE id=3)")
        conn.execute("UPDATE transactions SET flow_kind='unknown' WHERE id=3")
    text = client().get('/transactions?q=weekly').text
    match = re.search(r'id="transaction-3">\s*<summary[^>]*>(.*?)</summary>', text, re.S)
    assert match is not None
    summary = match[1]
    assert 'Currency unknown' in summary
    assert 'Needs accounting review' in summary
    assert 'unknown' in text
    assert '-$162.43' in summary


def test_opening_and_multisplit_editor_retain_restrictions(app_env):
    from app.db import engine
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE transactions SET source='opening' WHERE id=3")
    text = client().get('/txn/3/edit').text
    for field in ('amount', 'category_id', 'flow_kind'):
        assert re.search(r'<(?:input|select)[^>]*name="' + field + r'"[^>]* disabled', text)
    with engine.write_tx(app_env) as conn:
        conn.execute("UPDATE transactions SET source='manual' WHERE id=3")
        conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (3,5,0)')
    text = client().get('/txn/3/edit').text
    assert 'multiple splits' in text
    assert 'name="amount"' not in text
    assert 'name="category_id"' not in text
    assert 'name="flow_kind"' not in text


def test_closed_period_error_keeps_audited_overrides_and_confirm(app_env):
    from app.db import engine, repo_close
    with engine.write_tx(app_env) as conn:
        repo_close.mark_closed(conn, '2026-01')
    response = client().post('/txn/3/edit', data={
        'posted_on': '2026-01-05', 'description': 'weekly groceries',
        'counterparty': 'Synthetic Market', 'notes': '', 'amount': '-162.43', 'category_id': '4',
    })
    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert '2026-01 is closed' in response.text
    assert response.text.count('name="override" value="1"') == 2
    assert 'confirm("Delete this transaction? weekly groceries")' in response.text


def test_keyset_append_keeps_all_rows_despite_sentence_case_action(app_env):
    from html import unescape
    from app.db import engine
    with engine.write_tx(app_env) as conn:
        for i in range(60):
            tid = conn.execute("""INSERT INTO transactions(account_id,posted_on,description,
                amount_cents,source,external_id,flow_kind)
                VALUES (1,'2026-05-01',?,-1000,'sample',?,'purchase')""",
                (f'Generated pagination {i}', f'design-pagination-{i}')).lastrowid
            conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,-1000)', (tid,))
    http = client()
    first = http.get('/transactions').text
    assert first.count('class="tx-row"') == 50
    match = re.search(r'hx-get="([^"]+)"', first)
    assert match is not None
    tail = http.get(unescape(match[1])).text
    assert tail.count('class="tx-row"') == 30
    assert 'id="tx-list"' not in tail
    ids = re.findall(r'id="transaction-(\d+)"', first + tail)
    assert len(ids) == len(set(ids)) == 80
