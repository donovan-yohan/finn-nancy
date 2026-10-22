/* Generated shell + shipped assets in a fresh headless Chromium profile. */
'use strict';
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const {once} = require('node:events');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

class Cdp {
  constructor(url) {
    this.ordinal = 0;
    this.pending = new Map();
    this.ws = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = reject;
    });
    this.ws.onmessage = event => {
      const message = JSON.parse(event.data);
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      clearTimeout(pending.timer);
      if (message.error) pending.reject(Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
    };
  }
  async call(method, params = {}) {
    await this.ready;
    const id = ++this.ordinal;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(Error(`CDP timeout: ${method}`));
      }, 10000);
      this.pending.set(id, {resolve, reject, timer});
      this.ws.send(JSON.stringify({id, method, params}));
    });
  }
  async eval(expression) {
    const result = await this.call('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    });
    assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async until(expression) {
    for (let attempt = 0; attempt < 150; attempt++) {
      if (await this.eval(expression)) return;
      await pause(30);
    }
    throw Error(`Browser condition not met: ${expression}`);
  }
}

async function key(cdp, name, code, virtual) {
  for (const type of ['rawKeyDown', 'keyUp']) {
    await cdp.call('Input.dispatchKeyEvent', {
      type, key: name, code, windowsVirtualKeyCode: virtual,
    });
  }
}

async function tabTo(cdp, selector) {
  for (let attempt = 0; attempt < 30; attempt++) {
    await key(cdp, 'Tab', 'Tab', 9);
    if (await cdp.eval(`document.activeElement.matches(${JSON.stringify(selector)})`)) return;
  }
  throw Error(`Keyboard could not reach ${selector}`);
}

async function firstInstallOffline(cdp, base, stopServer) {
  // The bootstrap has no application assets or registration; all state is fresh.
  await cdp.call('Page.navigate', {url: base + '/__bootstrap'});
  await cdp.until("location.pathname==='/__bootstrap' && document.readyState==='complete'");
  assert.deepEqual(await cdp.eval(`(async()=>({
    workers:(await navigator.serviceWorker.getRegistrations()).length,
    caches:(await caches.keys()).length
  }))()`), {workers: 0, caches: 0});
  await cdp.call('Emulation.setEmulatedMedia', {
    features: [{name: 'prefers-color-scheme', value: 'light'}],
  });
  await cdp.call('Network.setCookie', {
    url: base, name: 'finn_nancy_theme', value: 'dark', path: '/', sameSite: 'Lax',
  });
  await cdp.call('Page.navigate', {url: base + '/upload'});
  await cdp.until("location.pathname==='/upload' && document.readyState==='complete' && !!navigator.serviceWorker.controller");
  await cdp.eval('navigator.serviceWorker.ready.then(()=>true)');
  // Inspect CacheStorage without requesting theme.js through the active worker.
  // A controlled online reload here would silently mask the first-install bug.
  const themePrecached = await cdp.eval("caches.match('/static/theme.js').then(Boolean)");
  assert.equal(await cdp.eval('document.documentElement.dataset.theme'), 'dark');
  await cdp.call('Network.clearBrowserCache');
  await cdp.call('Network.setCacheDisabled', {cacheDisabled: true});
  await stopServer();
  await cdp.call('Network.emulateNetworkConditions', {
    offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0,
  });
  await cdp.call('Page.navigate', {url: base + '/upload?first-install-offline=1'});
  await cdp.until("location.search==='?first-install-offline=1' && document.readyState==='complete'");
  const state = await cdp.eval(`({
    theme:document.documentElement.dataset.theme ?? null,
    cookie:document.cookie,
    background:getComputedStyle(document.body).backgroundColor,
    controlVisible:!!document.querySelector('.theme-control:not([hidden])'),
    selected:document.querySelector('[data-theme-select]').value,
    chrome:document.querySelector('meta[name="theme-color"]').content
  })`);
  console.log(JSON.stringify({themePrecached, serverStopped: true, offline: state}));
  assert.equal(state.theme, 'dark', 'first offline navigation must retain the explicit preference, independently of HTTP cache');
  assert.equal(state.background, 'rgb(0, 0, 0)');
  assert.equal(state.chrome, '#000000');
  assert.equal(state.controlVisible, true, 'offline Appearance must remain available');
  assert.equal(state.selected, 'dark');
  assert.equal(themePrecached, true, 'theme.js must be available at first SW activation');
  await tabTo(cdp, '[data-theme-select]');
  await cdp.eval(`(()=>{const select=document.querySelector('[data-theme-select]');
    select.value='light';select.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await cdp.until("document.documentElement.dataset.theme==='light'");
  assert.ok((await cdp.eval('document.cookie')).includes('finn_nancy_theme=light'));
  await cdp.call('Emulation.setEmulatedMedia', {
    features: [{name: 'prefers-color-scheme', value: 'dark'}],
  });
  await cdp.call('Page.navigate', {url: base + '/upload?offline-changed=1'});
  await cdp.until("location.search==='?offline-changed=1' && document.readyState==='complete'");
  assert.equal(await cdp.eval('document.documentElement.dataset.theme'), 'light');
  assert.equal(await cdp.eval("document.querySelector('[data-theme-select]').value"), 'light');
  console.log(JSON.stringify({themePrecached, offlineTheme: state.theme, changedTheme: 'light', serverStopped: true}));
}

async function workspaceFocus(cdp, base, theme) {
  await cdp.call('Emulation.setEmulatedMedia', {
    features: [{name: 'prefers-color-scheme', value: theme === 'light' ? 'dark' : 'light'}],
  });
  await cdp.call('Network.setCookie', {
    url: base, name: 'finn_nancy_theme', value: theme, path: '/', sameSite: 'Lax',
  });
  await cdp.call('Page.navigate', {url: base + '/focus'});
  await cdp.until("location.pathname==='/focus' && document.readyState==='complete'");
  assert.equal(await cdp.eval('document.documentElement.dataset.theme'), theme);
  const targets = ['period-total', 'period-link', 'period-primary', 'period-select',
    'close-button', 'close-link', 'close-input', 'close-select', 'close-summary'];
  const measures = [];
  for (const id of targets) {
    await tabTo(cdp, '#' + id);
    const measure = await cdp.eval(`(()=>{
      const node=document.activeElement, style=getComputedStyle(node);
      function rgba(color){return color.match(/[\\d.]+/g).map(Number);}
      function luminance(c){return c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4})
        .reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);}
      // These fixtures place the offset ring against their opaque shared card.
      let ancestor=node.parentElement;
      while(ancestor && rgba(getComputedStyle(ancestor).backgroundColor)[3]===0) ancestor=ancestor.parentElement;
      const background=rgba(getComputedStyle(ancestor).backgroundColor);
      const ring=rgba(style.outlineColor),alpha=ring[3]??1;
      const composited=ring.slice(0,3).map((v,i)=>v*alpha+background[i]*(1-alpha));
      const levels=[luminance(composited),luminance(background)].sort((a,b)=>a-b);
      const probe=document.createElement('span');probe.style.color='var(--finn)';node.after(probe);
      const token=getComputedStyle(probe).color;probe.remove();
      return {id:node.id,visible:node.matches(':focus-visible'),color:style.outlineColor,
        token,width:style.outlineWidth,style:style.outlineStyle,offset:style.outlineOffset,
        background:getComputedStyle(ancestor).backgroundColor,contrast:(levels[1]+.05)/(levels[0]+.05)};
    })()`);
    assert.equal(measure.id, id, 'real Tab navigation must focus the expected control');
    assert.equal(measure.visible, true, 'keyboard focus must activate :focus-visible');
    assert.equal(measure.width, '3px');
    assert.equal(measure.style, 'solid');
    // The workspace's equal-specificity total-link rule intentionally uses
    // its compact 2px offset; the other scoped controls retain 3px.
    assert.equal(measure.offset, id === 'period-total' ? '2px' : '3px');
    measures.push(measure);
  }
  console.log(JSON.stringify({theme, keyboardTargets: measures.length, measures}));
  assert.deepEqual(measures.filter(measure=>measure.contrast<3), [],
    `${theme}: focused workspace controls need at least 3:1 contrast against the adjacent surface`);
  assert.deepEqual(measures.filter(measure=>measure.color!==measure.token), [],
    `${theme}: focus overrides must resolve to the theme's --finn`);
}

async function main() {
  const [site, profile, scenario] = process.argv.slice(2);
  const types = {'.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png',
    '.webmanifest': 'application/manifest+json'};
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    if (pathname === '/__bootstrap') {
      response.writeHead(200, {'Content-Type': 'text/html'});
      return response.end('<!doctype html><title>Generated first-install fixture</title>');
    }
    const filename = path.resolve(site, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!filename.startsWith(site + path.sep) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) {
      response.writeHead(404);
      return response.end();
    }
    response.writeHead(200, {'Content-Type': types[path.extname(filename)] || 'text/html',
      'Cache-Control': 'public, max-age=3600'});
    response.end(fs.readFileSync(filename));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  const socket = net.createServer().listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const debugPort = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  // Keep diagnostics in the generated test directory, not a shared/user profile.
  const stderrPath = profile + '.stderr.log';
  const stderrFd = fs.openSync(stderrPath, 'w', 0o600);
  let chrome;
  try {
    chrome = spawn(process.env.CHROMIUM_BIN, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--disable-background-networking', '--disable-component-update',
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`, 'about:blank',
    ], {stdio: ['ignore', 'ignore', stderrFd]});
  } finally {
    fs.closeSync(stderrFd);
  }
  let spawnError;
  let closed = false;
  // Install observers immediately: exit/error may precede the first CDP poll.
  const stopped = new Promise(resolve => {
    chrome.once('error', error => {spawnError = error;});
    chrome.once('close', () => {closed = true; resolve();});
  });
  const terminated = () => closed || spawnError || chrome.exitCode !== null || chrome.signalCode !== null;
  function startupError(reason) {
    const tail = fs.readFileSync(stderrPath).subarray(-16384).toString('utf8');
    return Error(`Chromium ${reason}: ${process.env.CHROMIUM_BIN}; ` +
      `code=${chrome.exitCode}; signal=${chrome.signalCode}; ` +
      `${spawnError ? spawnError.message : ''}\nstderr (${stderrPath}):\n${tail}`);
  }
  let cdp;
  async function stopServer() {
    if (!server.listening) return;
    await new Promise(resolve => {server.close(resolve); server.closeAllConnections();});
  }
  try {
    let target;
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      if (terminated()) throw startupError('exited before CDP became ready');
      try {
        const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`, {
          signal: AbortSignal.timeout(Math.max(1, Math.min(50, deadline - Date.now()))),
        })).json();
        target = targets.find(candidate => candidate.type === 'page');
        if (target) break;
      } catch (_) { /* Chromium has not bound its isolated CDP socket yet. */ }
      await pause(Math.max(0, Math.min(50, deadline - Date.now())));
    }
    if (!target) throw startupError(terminated() ? 'exited before CDP became ready' : 'startup deadline exceeded');
    cdp = new Cdp(target.webSocketDebuggerUrl);
    await cdp.call('Page.enable');
    await cdp.call('Runtime.enable');
    await cdp.call('Network.enable');
    if (scenario === 'first-install-offline') await firstInstallOffline(cdp, base, stopServer);
    else if (scenario === 'focus-light' || scenario === 'focus-dark') await workspaceFocus(cdp, base, scenario.slice(6));
    else throw Error(`Unknown scenario: ${scenario}`);
  } finally {
    try {
      if (cdp) cdp.ws.close();
      if (!terminated()) chrome.kill('SIGTERM');
      // Never subscribe to an exit that may already have been emitted.
      let timer;
      try {
        await Promise.race([stopped, new Promise(resolve => {timer = setTimeout(resolve, 1000);})]);
      } finally {
        clearTimeout(timer);
      }
      if (!closed) {
        chrome.kill('SIGKILL');
        await stopped;
      }
    } finally {
      await stopServer();
    }
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
