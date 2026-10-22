"""Generated canonical workspace contracts; no running service or provider access."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.web.app import create_app


def test_workspace_cdp_printable_keyboard_contract():
    if not shutil.which('node'):
        pytest.skip('Node required for CDP harness checks')
    result = subprocess.run(
        ['node', '--test', 'tests/js/workspace_cdp.test.cjs'],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_activity_uses_one_native_disclosure_per_transaction(app_env):
    client = TestClient(create_app())  # Deliberately do not start the worker lifespan.
    html = client.get('/activity').text
    assert '<details class="tx-row"' in html
    assert len(re.findall(r'<details class="tx-row"', html)) == len(
        re.findall(r'data-why-url="/chat\?why=', html)
    )
    assert '<summary class="tx-summary">' in html
    assert 'role="row"' not in html
    assert 'data-label="description"' not in html
    assert 'class="tx-evidence"' in html
    assert 'href="/txn/' in html and '/edit"' in html
    assert 'href="/chat?why=' in html
    assert 'needs review:' in html or 'Accounting flow' in html


def test_cursored_fragment_retains_filters_and_single_list(app_env):
    client = TestClient(create_app())
    response = client.get('/transactions?cursor=2026-01-31:999999&q=market&account_id=1')
    assert response.status_code == 200
    assert 'id="tx-list"' not in response.text
    assert 'fnWhyHoldInstalled' not in response.text


def test_transaction_form_preserves_post_and_lock_controls(app_env):
    client = TestClient(create_app())
    html = client.get('/txn/new').text
    for name in ('posted_on', 'description', 'counterparty', 'account_id', 'amount',
                 'category_id', 'flow_kind', 'notes'):
        assert f'name="{name}"' in html
    assert 'method="post" action="/txn/new"' in html


def test_shared_shell_marks_current_navigation_and_scopes_density(app_env):
    client = TestClient(create_app())
    for route in ('/', '/activity', '/upload', '/insights', '/goals', '/manage'):
        html = client.get(route).text
        assert ' workspace-ui' in html
        assert 'aria-current="page"' in html
        assert 'href="#workspace-content"' in html
        assert html.count('<main') == 1
    assert 'Appearance' in html


def test_home_distinguishes_recorded_history_from_latest_period(app_env):
    html = TestClient(create_app()).get('/').text
    assert 'name="month"' in html and 'class="inline-form home-month"' in html
    assert '/activity?date_from=' in html
    assert 'Latest period' in html
    assert re.search(r'Latest period[^<]*|Latest period</span>', html)
    assert 'href="/statements?month=' in html
    assert 'Monthly cashflow' in html


def test_home_does_not_repeat_latest_net_as_a_fourth_metric(app_env):
    html = TestClient(create_app()).get('/').text
    assert html.count('class="stat-card') == 3
    assert 'Latest period' in html


def test_main_landmark_is_shared_for_processing_and_chat(app_env):
    client = TestClient(create_app())
    for route in ('/processing', '/chat', '/activity', '/txn/new'):
        html = client.get(route).text
        assert html.count('<main') == 1
        assert '<main class="app-main" id="workspace-content"' in html


def test_category_resolution_is_visible_when_no_accepted_categories(app_env):
    from app.web.templating import templates
    from starlette.requests import Request

    context = dict(request=Request({'type': 'http', 'path': '/', 'headers': []}),
                   stats={'money_in_cents': 0, 'money_out_cents': 500,
                          'net_cents': -500, 'net_class': 'negative'},
                   category_resolution={'excluded_expense_cents': 500,
                                        'unresolved_split_count': 2},
                   categories=[], transactions=[], cashflow=[], months=[],
                   selected_month='', semantic_review_count=0)
    html = templates.env.get_template('dashboard.html').render(context)
    assert 'Unresolved categories' in html and '$5.00' in html and '2 split(s)' in html


def test_transaction_form_has_a_scoped_compact_layout(app_env):
    html = TestClient(create_app()).get('/txn/new').text
    assert 'class="transaction-form"' in html
