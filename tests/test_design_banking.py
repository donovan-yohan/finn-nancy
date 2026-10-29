"""Shared-system bank UI proof. Generated records only; no live provider or settings."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
import re
import shutil
import subprocess
import time
import urllib.request

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request
from websockets.sync.client import connect

from app.web.templating import templates
from tests.test_banking_feed import banking  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/web/static'
SURFACES = ['banking_feed', 'banking_review', 'connections', 'sandbox_activity',
            'sandbox_accounts', 'sandbox_overview', 'sandbox_unavailable', 'connections_notice']


def generated_context() -> dict[str, Any]:
    subject = dict(id=11, posted_on='2026-06-12', merchant='Generated <cafe>',
                   description='Generated original description', amount_cents=-1234,
                   currency=None, observation_id=21, kind='added', pending=False,
                   suggestion_json='Generated suggestion, not approved', account_name='Generated chequing',
                   member_name='Generated Alex', account_id=7, included=True, role='primary',
                   transaction_id=None, needs_review=True)
    candidate = dict(id=31, posted_on='2026-06-12', counterparty='Generated cafe',
                     description='Generated purchase', amount_cents=-1234, recon_status='cleared', account_id=7)
    return dict(rows=[subject], receipts=[dict(extraction_id=41, merchant='Generated cafe',
                amount_cents=-1234, posted_on='2026-06-12', document_id=51, candidates=[candidate])],
                plan=dict(subject=subject, fingerprint='a' * 64, binding=dict(account_id=7,
                role='primary', included=True), association=None, blockers=[], candidates=[candidate]))


def render(name, **context):
    request = Request(dict(type='http', scheme='http', path='/activity',
                      server=('127.0.0.1', 8772), headers=[], query_string=b''))
    return templates.get_template(name + '.html').render(
        request=request, sandbox_candidate=name.startswith('sandbox_') or name == 'connections',
        active='activity', private_handoff=False, **context)


@pytest.mark.parametrize('surface', SURFACES)
def test_every_destination_uses_one_foundation_and_no_legacy_styles(surface):
    html = render(surface, **generated_context())
    for asset in ['vendor/bootstrap.min.css', 'tokens.css', 'components.css', 'banking.css']:
        assert html.count('/static/foundation/' + asset) == 1
    assert '/static/app.css' not in html
    assert '/static/banking.css' not in html and '/static/plaid-sandbox.css' not in html
    assert html.count('<h1>') == 1
    assert html.index('/static/foundation/banking.css') < html.index('</head>')
    if surface in ['connections', 'sandbox_activity', 'sandbox_accounts', 'sandbox_overview']:
        assert html.count('/static/plaid-sandbox.js') == 1
        assert html.index('/static/plaid-sandbox.js') > html.index('</main>')
        assert 'Sandbox · test data' in html and '>Not booked<' in html
        assert 'capture-outbox.js' not in html and 'serviceWorker.register' not in html
        assert 'aria-label="Sandbox destinations"' in html


def test_bank_forms_preserve_authority_and_unknowns():
    context = generated_context()
    feed = render('banking_feed', **context)
    review = render('banking_review', **context)
    assert 'Currency unknown' in feed and 'Currency unknown' in review
    assert 'Generated &lt;cafe&gt;' in review
    for value in ['data-banking="review"', 'data-subject="11"', 'data-fingerprint="' + 'a' * 64,
                  'name="action" value="match:31"', 'name="action" value="new"',
                  'name="action" value="exclude"', 'name="acknowledge_candidates"',
                  '<option value="unknown">Choose meaning</option>', 'id="banking-error"']:
        assert value in review
    for value in ['class="banking-receipt fn-editor"', 'data-extraction="41"',
                  'name="transaction_id" required', 'name="reason" required maxlength="500"']:
        assert value in feed
    assert 'Connected by Generated Alex' in feed and 'Not booked' in feed
    assert 'Feed role' in feed and 'Household view' in feed
    context['plan']['binding']['role'] = 'secondary'
    assert 'value="new"' not in render('banking_review', **context)
    context['plan']['blockers'] = ['pending', 'currency_review']
    blocked = render('banking_review', **context)
    assert 'value="match:31"' not in blocked and 'value="exclude"' in blocked
    context['plan']['association'] = dict(transaction_id=31)
    associated = render('banking_review', **context)
    assert 'name="action" value="revoke"' in associated
    assert 'Associated with ledger transaction 31' in associated
    assert 'value="exclude"' not in associated
    for update, expected in [(dict(pending=True), 'Bank pending'),
                             (dict(pending=False, kind='removed'), 'Removed')]:
        context['rows'][0].update(update)
        assert expected in render('banking_feed', **context)
    for filename in ['banking.css', 'plaid-sandbox.css']:
        assert "@import url('/static/foundation/banking.css');" in (STATIC / filename).read_text()


def test_generated_router_review_and_read_only_authority(banking):
    from app.banking.web import build_router
    service, connection, account, pages, calls = banking
    service.sync(connection)
    app = FastAPI()
    app.include_router(build_router(service))
    with TestClient(app, base_url=service.origin) as client:
        feed = client.get('/bank-feed')
        subject = service.feed()[0]['id']
        response = client.get(f'/bank-feed/{subject}')
        assert feed.status_code == response.status_code == 200
        plan = service.review_plan(subject)
        assert f'data-fingerprint="{plan["fingerprint"]}"' in response.text
        assert '/static/foundation/banking.css' in response.text
        csrf = client.get('/connections/data/overview').json()['csrf']
        service.read_only = lambda: True
        result = client.post(f'/connections/data/decision/{subject}', headers={
            'Origin': service.origin, 'X-Banking-CSRF': csrf}, json=dict(
            fingerprint=plan['fingerprint'], operation_key='generated-design-read-only',
            action='new', reason='Generated UI proof', flow_kind='purchase'))
        assert result.status_code == 409 and result.json() == {'detail': 'read_only'}
        assert calls == ['/transactions/sync']


def test_sandbox_child_blocks_compose_parent_styles_and_scripts():
    html = templates.env.from_string('''{% extends 'sandbox_shell.html' %}
        {% block styles %}{{ super() }}<style id="generated-child-style"></style>{% endblock %}
        {% block scripts %}{{ super() }}<script id="generated-child-script"></script>{% endblock %}
        {% block sandbox_content %}<p>Generated child</p>{% endblock %}''').render(
        request=Request(dict(type='http', scheme='http', path='/', headers=[], query_string=b'')),
        page_title='Generated child', sandbox_candidate=True, active='home')
    assert html.count('/static/foundation/banking.css') == 1
    assert html.count('/static/plaid-sandbox.js') == 1
    assert 'generated-child-style' in html and 'generated-child-script' in html


def test_real_generated_dom_geometry_and_recovery(tmp_path):
    """One owned headless browser, no server, script tags or provider network."""
    chrome = os.environ.get('CHROMIUM_BIN', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not Path(chrome).is_file():
        chrome = shutil.which('chromium')
    assert chrome, 'Chromium is required for banking layout proof'
    process = subprocess.Popen([chrome, '--headless=new', '--disable-gpu', '--no-first-run',
        '--disable-background-networking', '--remote-debugging-port=0',
        f'--user-data-dir={tmp_path / "chrome"}', 'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checks = []
    try:
        port_file = tmp_path / 'chrome/DevToolsActivePort'
        for _ in range(200):
            if port_file.exists():
                break
            time.sleep(.05)
        port = port_file.read_text().splitlines()[0]
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list') as response:
            target = next(t['webSocketDebuggerUrl'] for t in json.load(response) if t['type'] == 'page')
        with connect(target) as socket:
            ordinal = 0

            def call(method, params=None):
                nonlocal ordinal
                ordinal += 1
                socket.send(json.dumps(dict(id=ordinal, method=method, params=params or {})))
                while True:
                    result = json.loads(socket.recv())
                    if result.get('id') == ordinal:
                        assert 'error' not in result, result
                        return result.get('result', {})

            def evaluate(code):
                result = call('Runtime.evaluate', dict(expression=code, returnByValue=True, awaitPromise=True))
                assert 'exceptionDetails' not in result, result
                return result['result'].get('value')

            styles = '\n'.join((STATIC / 'foundation' / name).read_text() for name in
                               ['vendor/bootstrap.min.css', 'tokens.css', 'components.css', 'banking.css'])
            source = (STATIC / 'plaid-sandbox.js').read_text()
            record = dict(name='Generated long merchant with a complete readable name', account_name='Generated chequing',
                          owner_id='alpha', connection_id='generated', account_id='account', date='2026-06-12',
                          state='pending', selected=False, ownership='shared', amount_cents='-8999999999999999',
                          currency=None, transaction_id='generated-txn')
            positive = dict(record, amount_cents='9007199254740991', currency='CAD', transaction_id='generated-positive')
            overview = dict(enabled=True, configured=True, csrf='generated',
                            members=[dict(id='alpha', name='Generated Alex')],
                            connections=[dict(id='generated', owner_id='alpha', status='revocation_uncertain')],
                            accounts=[dict(name='Generated chequing', connection_id='generated',
                                           account_id='account', selected=False, ownership='unassigned')],
                            link_attempts=[dict(id='attempt', owner_id='alpha', status='uncertain')],
                            recent=[record, positive], summary=dict(connection_count=1, account_count=1, transaction_count=2, pending_count=2))
            call('Page.enable')
            frame = call('Page.getFrameTree')['frameTree']['frame']['id']
            for surface in SURFACES:
                html = render(surface, **generated_context())
                html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>|<link\b[^>]*>', '', html)
                html = html.replace('</head>', f'<style>{styles}</style></head>')
                call('Page.setDocumentContent', dict(frameId=frame, html=html))
                if surface in ['connections', 'sandbox_activity', 'sandbox_accounts', 'sandbox_overview']:
                    evaluate('window.__requests=[];window.__overview=' + json.dumps(overview) + ';window.__row=' + json.dumps(record) + ';' + '''
                        window.fetch=async (path, options)=>{window.__requests.push([path,options?.method||'GET']);
                        return {ok:true,json:async()=>path.includes('activity?')?{rows:window.__overview.recent,total:2,offset:0,limit:50,has_more:false}:window.__overview};};''')
                    evaluate(source)
                    for _ in range(100):
                        if evaluate("document.querySelector('.sandbox-page').dataset.loaded==='true'"):
                            break
                        time.sleep(.01)
                    assert evaluate("document.querySelector('.sandbox-page').dataset.loaded==='true'")
                    if surface in ['sandbox_activity', 'sandbox_overview']:
                        assert evaluate("JSON.stringify([...document.querySelectorAll('.tx-row .sandbox-money')].map(n=>n.textContent))===JSON.stringify(['−89,999,999,999,999.99 Unknown currency','+90,071,992,547,409.91 CAD'])")
                    if surface == 'connections':
                        assert evaluate("document.querySelector('#sandbox-attempts').innerText.includes('Never replay this exchange')")
                        assert evaluate("document.querySelector('[data-action=disconnect]').innerText==='Reconcile uncertain revocation'")
                        assert evaluate("document.querySelector('#sandbox-link').disabled")
                    if surface == 'sandbox_accounts':
                        evaluate("document.querySelector('[data-action=edit-account]').click()")
                        assert evaluate("document.activeElement.tagName==='SELECT'")
                        assert evaluate("document.querySelector('[data-account-form] button[type=submit]').disabled")
                    assert evaluate("window.__requests.every(([p,m])=>p.startsWith('/api/sandbox/')&&m==='GET')")
                if surface == 'banking_review':
                    call('Emulation.setFocusEmulationEnabled', dict(enabled=True))
                    evaluate("document.querySelector('.banking-provider>summary').focus()")
                    assert evaluate("document.activeElement===document.querySelector('.banking-provider>summary')")
                    call('Input.dispatchKeyEvent', dict(type='rawKeyDown', key=' ', code='Space', windowsVirtualKeyCode=32))
                    call('Input.dispatchKeyEvent', dict(type='keyUp', key=' ', code='Space', windowsVirtualKeyCode=32))
                    for _ in range(50):
                        if evaluate("document.querySelector('.banking-provider').open"):
                            break
                        time.sleep(.01)
                    assert evaluate("document.querySelector('.banking-provider').open")
                evaluate("document.querySelectorAll('#workspace-content details').forEach(d=>d.open=true)")
                for width in [320, 390, 1100, 1440]:
                    call('Emulation.setDeviceMetricsOverride', dict(width=width, height=1000, deviceScaleFactor=1, mobile=width < 500))
                    for theme in ['light', 'dark']:
                        evaluate(f"document.documentElement.dataset.bsTheme='{theme}'")
                        geometry = evaluate('''(()=>({overflow:document.documentElement.scrollWidth>innerWidth,
                            small:[...document.querySelectorAll('#workspace-content button,#workspace-content select,#workspace-content input:not([type=checkbox]):not([type=radio]),#workspace-content summary')]
                            .filter(n=>n.getClientRects().length&&!n.hidden).filter(n=>n.getBoundingClientRect().height<47).map(n=>n.outerHTML),
                            clipped:[...document.querySelectorAll('.banking-amount,.sandbox-money')].some(n=>n.scrollWidth>n.clientWidth+1)}))()''')
                        assert not geometry['overflow'] and not geometry['clipped'] and not geometry['small'], (surface, width, theme, geometry)
                        if surface in ['sandbox_activity', 'sandbox_overview']:
                            money = evaluate("""[...document.querySelectorAll('.tx-row .sandbox-money')].map(n=>{
const cell=n.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(n);
return {text:n.textContent,font:parseFloat(getComputedStyle(n).fontSize),cell:{left:cell.left,right:cell.right,top:cell.top,bottom:cell.bottom},
glyphs:[...range.getClientRects()].map(r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom}))};})""")
                            for amount in money:
                                assert amount['font'] >= 15 and amount['glyphs'], (surface, width, amount)
                                assert all(g['left'] >= amount['cell']['left'] - 1 and g['right'] <= amount['cell']['right'] + 1
                                           and g['top'] >= amount['cell']['top'] - 1 and g['bottom'] <= amount['cell']['bottom'] + 1
                                           for g in amount['glyphs']), (surface, width, amount)
                            geometry['money'] = money
                            if width >= 1100:
                                columns = evaluate("""(()=>{
const cells=row=>Object.fromEntries(['sandbox-merchant','sandbox-account-label','sandbox-date','sandbox-state','sandbox-money','sandbox-chevron'].map(name=>{
const r=row.querySelector('.'+name).getBoundingClientRect();return [name,{left:r.left,right:r.right,width:r.width,center:(r.left+r.right)/2,layoutWidth:row.querySelector('.'+name).offsetWidth}]}));
return {head:cells(document.querySelector('.sandbox-column-head')),rows:[...document.querySelectorAll('.tx-row')].map(cells)};})()""")
                                for row in columns['rows']:
                                    for name, cell in row.items():
                                        # Native open disclosure rotates the arrow around its center.
                                        edge = 'center' if name == 'sandbox-chevron' else 'right' if name == 'sandbox-money' else 'left'
                                        assert abs(cell[edge] - columns['head'][name][edge]) < 1, (surface, width, name, columns)
                                    assert row['sandbox-chevron']['layoutWidth'] >= 16, (surface, width, columns)
                                geometry['columns'] = columns
                            if surface == 'sandbox_activity':
                                evidence = evaluate("""[...document.querySelectorAll('.sandbox-transaction')].map(row=>{
const bounds=n=>{const r=n.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width}};
return {summary:bounds(row.querySelector('summary')),panels:[...row.children].slice(1).map(bounds)};})""")
                                for row in evidence:
                                    for panel in row['panels']:
                                        assert abs(panel['left'] - row['summary']['left']) < 1 and abs(panel['right'] - row['summary']['right']) < 1, (surface, width, evidence)
                                geometry['evidence'] = evidence
                        if surface == 'sandbox_activity' and width < 500:
                            filters = evaluate("({viewport:innerWidth,grid:getComputedStyle(document.querySelector('.sandbox-filter-grid')).gridTemplateColumns, widths:[...document.querySelectorAll('.sandbox-filter-grid select')].map(n=>n.getBoundingClientRect().width)})")
                            assert all(w > width - 40 for w in filters['widths']), filters
                        checks.append(dict(surface=surface, width=width, theme=theme, **geometry))
                        output = os.environ.get('BANKING_DESIGN_EVIDENCE')
                        if output and width == 390 and theme == 'light' and surface in ['banking_review', 'sandbox_activity', 'sandbox_accounts']:
                            directory = Path(output)
                            directory.mkdir(parents=True, exist_ok=True)
                            evaluate("new Promise(resolve=>{scrollTo(0,0);requestAnimationFrame(()=>requestAnimationFrame(resolve))})")
                            shot = call('Page.captureScreenshot', dict(format='png', captureBeyondViewport=True))['data']
                            (directory / f'{surface}-phone-light.png').write_bytes(base64.b64decode(shot))
                if surface == 'sandbox_activity':
                    evaluate("window.fetch=async()=>{throw Error('Generated read failure')};document.querySelector('#sandbox-retry').click()")
                    for _ in range(100):
                        if evaluate("document.querySelector('.sandbox-page').dataset.loaded==='false'"):
                            break
                        time.sleep(.01)
                    assert evaluate("document.querySelector('#sandbox-status').innerText==='Import status unavailable.'&&document.querySelector('#sandbox-message').innerText==='Generated read failure'&&!document.querySelector('#sandbox-retry').hidden")
                    assert evaluate("!document.querySelector('#sandbox-staged').innerText.includes('0 transactions')")
                if surface == 'sandbox_accounts':
                    evaluate("document.querySelector('[data-action=cancel-account]').click()")
                    assert evaluate("!document.querySelector('[data-account-form]')&&document.activeElement.dataset.action==='edit-account'")
                output = os.environ.get('BANKING_DESIGN_EVIDENCE')
                if output and surface in ['banking_feed', 'banking_review', 'sandbox_activity', 'sandbox_accounts', 'connections']:
                    directory = Path(output)
                    directory.mkdir(parents=True, exist_ok=True)
                    screenshot = call('Page.captureScreenshot', dict(format='png', captureBeyondViewport=True))['data']
                    (directory / f'{surface}-desktop-dark.png').write_bytes(base64.b64decode(screenshot))
            if os.environ.get('BANKING_DESIGN_EVIDENCE'):
                (Path(os.environ['BANKING_DESIGN_EVIDENCE']) / 'browser-checks.json').write_text(json.dumps(checks, indent=2))
            print(json.dumps(dict(renders=len(checks), provider_requests=0, owned_browser=True)))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
