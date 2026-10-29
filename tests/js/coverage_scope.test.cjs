/* Generated-only real GETs in one owned headless Chromium. No shared browser. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const os = require('node:os');
const root = path.resolve(__dirname, '../..');
const artifacts = path.resolve(process.argv[2] || path.join(os.tmpdir(), 'coverage-scope-proof'));
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'coverage-scope-runtime-'));
const chrome = process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function port() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const value = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return value;
}
async function until(fn) {
  let last;
  for (let i = 0; i < 160; i++) {
    try {const value = await fn(); if (value) return value;} catch (error) {last = error;}
    await pause(50);
  }
  throw last || Error('Readiness condition timed out');
}
class CDP {
  constructor(url) {
    this.id = 0; this.pending = new Map(); this.errors = []; this.blocked = [];
    this.socket = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve);
      this.socket.addEventListener('error', reject);
    });
    this.socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id); clearTimeout(pending.timer);
        message.error ? pending.reject(message.error) : pending.resolve(message.result);
      } else if (message.method === 'Runtime.exceptionThrown') this.errors.push(message.params);
      else if (message.method === 'Fetch.requestPaused') {
        const {requestId, request} = message.params;
        const allowed = request.url.startsWith(this.base + '/') && request.method === 'GET';
        if (!allowed) this.blocked.push({url: request.url, method: request.method});
        this.call(allowed ? 'Fetch.continueRequest' : 'Fetch.failRequest',
          allowed ? {requestId} : {requestId, errorReason: 'BlockedByClient'}).catch(error => this.errors.push(error));
      }
    });
  }
  async call(method, params = {}) {
    await this.ready;
    const id = ++this.id;
    const result = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {this.pending.delete(id); reject(Error('CDP timeout ' + method));}, 12000);
      this.pending.set(id, {resolve, reject, timer});
    });
    this.socket.send(JSON.stringify({id, method, params}));
    return result;
  }
  async eval(expression) {
    const result = await this.call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async key(key, code, windowsVirtualKeyCode) {
    await this.call('Input.dispatchKeyEvent', {type: 'keyDown', key, code, windowsVirtualKeyCode});
    await this.call('Input.dispatchKeyEvent', {type: 'keyUp', key, code, windowsVirtualKeyCode});
  }
  async tabTo(selector) {
    await this.eval('document.activeElement.blur()');
    for (let i = 0; i < 100; i++) {
      await this.key('Tab', 'Tab', 9);
      if (await this.eval(`document.activeElement.matches(${JSON.stringify(selector)})`)) return;
    }
    throw Error('Native Tab did not reach ' + selector);
  }
  async shot(name, expression) {
    await this.eval(`(${expression}).scrollIntoView({block:'start',behavior:'instant'})`);
    await pause(100);
    const result = await this.call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    fs.writeFileSync(path.join(artifacts, name + '.png'), Buffer.from(result.data, 'base64'));
  }
  close() {
    for (const pending of this.pending.values()) {clearTimeout(pending.timer); pending.reject(Error('Owned CDP closed'));}
    this.pending.clear(); this.socket.close();
  }
}
const fixture = String.raw`
import os,json,socket
from pathlib import Path
from contextlib import contextmanager
root=Path(os.environ['COVERAGE_RUNTIME'])
os.environ.update(DB_PATH=str(root/'generated.sqlite'),DATA_DIR=str(root/'data'),STRICT_LOCAL_MODE='true',READ_ONLY='true',HOME_CURRENCY='CAD',API_TOKEN='',EMBEDDINGS_ENABLED='false',PLAID_ENABLED='false',MERCHANT_SEARCH_ENABLED='false',LOCAL_LLM_BASE='http://blocked.invalid/v1')
from app.config import Settings,get_settings
Settings.model_config['env_file']=None
get_settings.cache_clear()
from app.db import migrate,engine
migrate.init_db(os.environ['DB_PATH'])
from tests.test_coverage_scope_copy import seed_scope_case
cases={}
for variant,month in [('staged','2026-06'),('matched','2026-07'),('unmatched','2026-08'),('mixed','2026-09'),('credit','2026-10'),('ignored','2026-11'),('empty','2026-12')]:
 cases[variant]={'month':month,**seed_scope_case(os.environ['DB_PATH'],variant,month=month)}
with engine.read_conn(os.environ['DB_PATH']) as conn:before='\n'.join(conn.iterdump())
original=engine.connect
def readonly(path,*,read_only=False):
 assert Path(path).resolve()==(root/'generated.sqlite').resolve()
 return original(path,read_only=True)
engine.connect=readonly
def deny(*args,**kwargs):raise AssertionError('No network egress in generated fixture')
socket.socket.connect=deny
socket.socket.connect_ex=deny
socket.create_connection=deny
from app.web.app import create_app
app=create_app()
@app.get('/__fixture')
def manifest():return cases
@app.get('/__unchanged')
def unchanged():
 with engine.read_conn(os.environ['DB_PATH']) as conn:
  return {'dump_equal':before=='\n'.join(conn.iterdump()),'query_only':conn.execute('PRAGMA query_only').fetchone()[0]}
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['COVERAGE_PORT']),lifespan='off',log_level='warning')
`;
(async () => {
  fs.mkdirSync(artifacts, {recursive: true});
  const serverPort = await port(), debugPort = await port(), base = `http://127.0.0.1:${serverPort}`;
  const logs = [], checks = [], keyboard = [];
  let server, browser, cdp, success = false;
  try {
    server = spawn(path.join(root, '.venv/bin/python'), ['-c', fixture], {
      cwd: root, env: {...process.env, COVERAGE_RUNTIME: scratch, COVERAGE_PORT: String(serverPort)},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    server.stdout.on('data', data => logs.push(String(data)));
    server.stderr.on('data', data => logs.push(String(data)));
    const cases = await until(async () => {const response = await fetch(base + '/__fixture'); return response.ok && response.json();});
    browser = spawn(chrome, ['--headless=new', '--no-first-run', '--no-default-browser-check',
      '--disable-background-networking', '--disable-component-update', '--disable-sync',
      `--user-data-dir=${scratch}/chrome`, `--remote-debugging-port=${debugPort}`, 'about:blank'], {stdio: 'ignore'});
    const targets = await until(async () => {const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`); return response.ok && response.json();});
    cdp = new CDP(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
    cdp.base = base;
    await cdp.call('Page.enable'); await cdp.call('Runtime.enable');
    await cdp.call('Fetch.enable', {patterns: [{urlPattern: '*'}]});
    for (const width of [1440, 390]) for (const theme of ['light', 'dark']) {
      await cdp.call('Emulation.setDeviceMetricsOverride', {width, height: 1000, deviceScaleFactor: 1, mobile: false});
      for (const [variant, ids] of Object.entries(cases)) {
        const url = `${base}/recon?month=${ids.month}`;
        await cdp.call('Page.navigate', {url});
        await until(() => cdp.eval(`location.href===${JSON.stringify(url)} && document.readyState==='complete' && !!document.querySelector('.evidence-workspace')`));
        await cdp.eval(`document.documentElement.setAttribute('data-bs-theme',${JSON.stringify(theme)})`);
        const observed = await cdp.eval(`(()=>{
          const s=[...document.querySelectorAll('section')].find(e=>e.querySelector('h2')?.textContent==='Matched vs unmatched spend');
          window.coverageSection=s;
          const copy=[...s.querySelectorAll('p')].slice(0,2).map(e=>({text:e.textContent,visible:e.checkVisibility(),width:e.getBoundingClientRect().width,scroll:e.scrollWidth}));
          return {scope:s.innerText,badge:s.querySelector('.evidence-count').innerText,width:innerWidth,pageWidth:document.documentElement.scrollWidth,copy};
        })()`);
        assert(observed.pageWidth <= width + 1, JSON.stringify(observed));
        assert(observed.scope.includes('Expected-statement coverage · ' + ids.month));
        assert(observed.scope.includes('Staged lines awaiting a match can exist outside this coverage.'));
        assert(observed.copy.every(item => item.visible && item.scroll <= item.width + 1));
        const pct = {matched: '100.0', unmatched: '0.0', mixed: '80.2'}[variant];
        assert.equal(observed.badge, pct ? pct + '% covered' : 'No eligible spend');
        if (['credit', 'ignored'].includes(variant)) {assert(observed.scope.includes('1 line')); assert(!observed.scope.includes('0.0%'));}
        if (['empty', 'staged'].includes(variant)) assert(observed.scope.includes(`No statement lines in expected-statement coverage for ${ids.month}.`));
        if (variant === 'staged') {
          assert(await cdp.eval(`!!document.querySelector('form[action="/recon/line/${ids.line}/confirm"] button')`));
          await cdp.tabTo(`form[action="/recon/line/${ids.line}/confirm"] button`);
          assert(await cdp.eval('document.activeElement.matches(":focus-visible")'));
          await cdp.shot(`candidate-${width}-${theme}`, `document.querySelector('#line-${ids.line}')`);
          keyboard.push({width, theme, action: 'Native Tab to Confirm match, no POST'});
        }
        await cdp.shot(`${variant}-scope-${width}-${theme}`, 'window.coverageSection');
        if (variant === 'ignored') {
          const selector = '[aria-label="Statement coverage"]';
          await cdp.tabTo(selector);
          const before = await cdp.eval('({left:document.activeElement.scrollLeft,max:document.activeElement.scrollWidth-document.activeElement.clientWidth,focus:document.activeElement.matches(":focus-visible")})');
          assert(before.focus);
          await cdp.key('ArrowRight', 'ArrowRight', 39);
          if (before.max > 0) await until(() => cdp.eval(`document.activeElement.scrollLeft>${before.left}`));
          keyboard.push({width, theme, action: 'Native Tab to Statement coverage and ArrowRight', overflow: before.max});
          await cdp.shot(`ignored-table-${width}-${theme}`, `document.querySelector(${JSON.stringify(selector)})`);
        }
        checks.push({variant, width, theme, ...observed});
      }
    }
    const readback = await (await fetch(base + '/__unchanged')).json();
    assert.deepEqual(readback, {dump_equal: true, query_only: 1});
    assert.equal(cdp.errors.length, 0, JSON.stringify(cdp.errors));
    assert.deepEqual(cdp.blocked, []);
    fs.writeFileSync(path.join(artifacts, 'browser-report.json'), JSON.stringify({checks, keyboard, readback, runtimeErrors: cdp.errors}, null, 2));
    success = true;
    console.log(JSON.stringify({renders: checks.length, keyboardChecks: keyboard.length, readback, runtimeErrors: 0}));
  } finally {
    if (cdp) cdp.close();
    const processes = [];
    for (const child of [browser, server]) if (child) {
      if (child.exitCode === null) {
        child.kill('SIGTERM');
        await Promise.race([new Promise(resolve => child.once('exit', resolve)), pause(3000)]);
        if (child.exitCode === null && child.signalCode === null) {child.kill('SIGKILL'); await new Promise(resolve => child.once('exit', resolve));}
      }
      processes.push({pid: child.pid, exitCode: child.exitCode, signalCode: child.signalCode});
    }
    fs.writeFileSync(path.join(artifacts, 'fixture.log'), logs.join(''));
    fs.rmSync(scratch, {recursive: true, force: true});
    fs.writeFileSync(path.join(artifacts, 'cleanup.json'), JSON.stringify({success, processes, scratchRemoved: !fs.existsSync(scratch)}, null, 2));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
