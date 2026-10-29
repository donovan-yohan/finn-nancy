/* Generated route HTML, production styles and close.js in owned headless Chrome. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const os = require('node:os');

class Cdp {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.pending = new Map();
    this.next = 1;
    this.errors = [];
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve, { once: true });
      this.socket.addEventListener('error', reject, { once: true });
    });
    this.socket.addEventListener('message', ({ data }) => {
      const message = JSON.parse(String(data));
      if (message.method === 'Runtime.exceptionThrown') this.errors.push(message.params);
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
    });
  }
  async call(method, params = {}) {
    await this.ready;
    const id = this.next++;
    const result = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('CDP timeout ' + method)), 10000);
      this.pending.set(id, {
        resolve: value => { clearTimeout(timer); resolve(value); },
        reject: error => { clearTimeout(timer); reject(error); },
      });
    });
    this.socket.send(JSON.stringify({ id, method, params }));
    return result;
  }
  async eval(expression) {
    const result = await this.call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    assert.equal(result.exceptionDetails, undefined, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
}

async function until(fn) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 30));
  }
  throw new Error('Expected browser condition was not reached');
}


// Read-only browser result: exact fragment identity plus painted heading/amount
// text rectangles. checkVisibility() alone also accepts offscreen elements.
function fragmentState(href) {
  const target = document.getElementById(decodeURIComponent(href.slice(1)));
  const textState = element => {
    if (!element) return { visible: false, rects: [] };
    const range = document.createRange();
    range.selectNodeContents(element);
    const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0);
    return {
      text: element.textContent.trim(),
      rects: rects.map(r => ({left:r.left, top:r.top, right:r.right, bottom:r.bottom})),
      visible: element.checkVisibility({checkOpacity:true, checkVisibilityCSS:true}) && rects.length > 0 &&
        rects.every(r => r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight &&
          [0.25, 0.5, 0.75].every(part => element.contains(document.elementFromPoint(
            r.left + r.width * part, r.top + r.height / 2)))),
    };
  };
  const heading = textState(target?.querySelector('h2'));
  const amount = textState(target?.querySelector('strong'));
  return {href, hash:location.hash, exists:!!target, top:target?.getBoundingClientRect().top,
    scrollX, scrollY, heading, amount,
    ready:location.hash === href && !!target && heading.visible && amount.visible};
}

async function visibleFragment(cdp, href) {
  let previous;
  let stable = 0;
  // Native smooth scrolling is asynchronous. Do not focus a disclosure or start
  // another fragment while its predecessor is still moving through the viewport.
  return until(async () => {
    const state = await cdp.eval(`(${fragmentState})(${JSON.stringify(href)})`);
    const geometry = JSON.stringify([state.scrollX, state.scrollY, state.heading.rects, state.amount.rects]);
    stable = state.ready && geometry === previous ? stable + 1 : 0;
    previous = geometry;
    return state.ready && stable >= 3 && state;
  });
}

test('foundation close/report rendered geometry, guards, disclosure and anchors', {
  skip: !process.env.DESIGN_CLOSE_RENDER_DIR,
  timeout: 120000,
}, async () => {
  const repo = path.resolve(__dirname, '../..');
  const renderDir = path.resolve(process.env.DESIGN_CLOSE_RENDER_DIR);
  const output = path.resolve(process.env.DESIGN_CLOSE_BROWSER_DIR || path.join(renderDir, '../browser'));
  fs.mkdirSync(output, { recursive: true });
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    let file;
    if (url.pathname.startsWith('/static/')) file = path.join(repo, 'app/web', url.pathname);
    else if (/^\/[a-z_-]+\.html$/.test(url.pathname)) file = path.join(renderDir, url.pathname);
    if (!file || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : file.endsWith('.js') ? 'text/javascript' : 'text/html');
    res.end(fs.readFileSync(file));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const origin = 'http://127.0.0.1:' + server.address().port;
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'fn-design-close-chrome-'));
  const browser = spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-component-update',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
    '--user-data-dir=' + profile, 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '';
  browser.stderr.on('data', chunk => { stderr += String(chunk); });
  let cdp;
  const metrics = [];
  const anchors = [];
  let checkedRefusals = false;
  try {
    const port = await until(async () => {
      const file = path.join(profile, 'DevToolsActivePort');
      return fs.existsSync(file) && fs.readFileSync(file, 'utf8').split('\n')[0];
    });
    const pages = await (await fetch('http://127.0.0.1:' + port + '/json/list')).json();
    cdp = new Cdp(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
    await cdp.call('Page.enable');
    await cdp.call('Runtime.enable');
    for (const width of [1440, 768, 390, 320]) {
      await cdp.call('Emulation.setDeviceMetricsOverride', { width, height: 960, deviceScaleFactor: 1, mobile: false });
      for (const file of ['close-setup.html', 'close-frozen.html', 'close-clean.html', 'close-reopened.html', 'close-unknown.html', 'close-waivers.html', 'statement-household.html', 'statement-account.html', 'statement-stress.html', 'statement-blocked-unsupported_currency.html', 'statement-blocked-legacy_snapshot_unavailable.html', 'statement-blocked-snapshot_integrity_error.html']) {
        const url = origin + '/' + file;
        await cdp.call('Page.navigate', { url });
        await until(() => cdp.eval(`location.href === ${JSON.stringify(url)} && document.readyState === 'complete' && !!document.querySelector('.close-report-section')`));
        for (const theme of ['light', 'dark']) {
          const result = await cdp.eval(`(() => {
            document.documentElement.dataset.bsTheme = ${JSON.stringify(theme)};
            document.documentElement.style.fontSize = ${JSON.stringify(width === 320 ? '200%' : '100%')};
            const visible = e => e.checkVisibility();
            const amounts = [...document.querySelectorAll('.period-total-link > strong')];
            const controls = [...document.querySelectorAll('main button, main select, main summary, main .fn-check')].filter(visible);
            const boxes = controls.map(e => ({text:e.textContent.trim().slice(0,60), height:e.getBoundingClientRect().height}));
            return {
              file:${JSON.stringify(file)}, width:innerWidth, theme:${JSON.stringify(theme)},
              scrollWidth:document.documentElement.scrollWidth,
              overflow:[...document.querySelectorAll('main *')].filter(e=>visible(e) && e.getBoundingClientRect().right>innerWidth).map(e=>({tag:e.tagName, cls:e.className, right:e.getBoundingClientRect().right})).slice(0,10),
              mainLeft:document.querySelector('main').getBoundingClientRect().left,
              sectionLeft:document.querySelector('.close-report-section').getBoundingClientRect().left,
              amountRight:amounts.map(e=>Math.round(e.getBoundingClientRect().right)),
              truncated:amounts.some(e=>getComputedStyle(e).textOverflow==='ellipsis' || e.scrollWidth>e.clientWidth+1),
              boxes, legacy:!!document.querySelector('link[href="/static/app.css"]'),
            };
          })()`);
          assert.ok(result.scrollWidth <= width + 1, JSON.stringify(result));
          assert.equal(result.mainLeft, result.sectionLeft);
          assert.equal(result.truncated, false);
          assert.equal(result.legacy, false);
          if (result.amountRight.length) assert.equal(new Set(result.amountRight).size, 1);
          for (const box of result.boxes) assert.ok(box.height >= 44, JSON.stringify(box));
          metrics.push(result);
          fs.writeFileSync(path.join(output, 'metrics.json'), JSON.stringify(metrics, null, 2));
        }
        if ([390,1440].includes(width) && ['close-setup.html','close-frozen.html','statement-household.html'].includes(file)) {
          await cdp.eval("document.documentElement.dataset.bsTheme='light'; scrollTo(0,0)");
          const screenshot = await cdp.call('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
          fs.writeFileSync(path.join(output, file.replace('.html', '-' + width + '.png')), Buffer.from(screenshot.data, 'base64'));
        }
        if (file === 'close-waivers.html' || file === 'close-reopened.html') {
          const editors = await cdp.eval(`(() => {
            document.querySelectorAll('.close-waiver-editor').forEach(e=>e.open=true);
            return [...document.querySelectorAll('.close-row-form')].map(f=>({
              width:f.getBoundingClientRect().width,
              labeled:[...f.querySelectorAll('input[required]')].every(i=>i.labels.length>0),
              right:f.getBoundingClientRect().right,
            }));
          })()`);
          assert.ok(editors.length > 0);
          for (const editor of editors) {
            assert.ok(editor.width <= 640, JSON.stringify(editor));
            assert.ok(editor.right <= width, JSON.stringify(editor));
            assert.equal(editor.labeled, true);
          }
        }
        if (file === 'close-setup.html') {
          const guard = await cdp.eval(`(() => {
            const form=document.querySelector('form[action="/close/signoff"]');
            form.querySelectorAll('[required]').forEach(e=>{e.type==='checkbox'?e.checked=true:e.value='Generated review';});
            form.dispatchEvent(new Event('input',{bubbles:true}));
            const prepare=document.querySelector('form[action="/close/prepare"]');
            prepare.querySelectorAll('[required]').forEach(e=>{e.type==='checkbox'?e.checked=true:e.value='Generated review';});
            prepare.dispatchEvent(new Event('input',{bubbles:true}));
            return {blocked:form.querySelector('button').disabled, message:form.querySelector('[data-form-requirements]').textContent,
                    prepareReady:!prepare.querySelector('button').disabled};
          })()`);
          assert.equal(guard.blocked, true);
          assert.match(guard.message, /audited statement matrix/);
          assert.equal(guard.prepareReady, true);
        }
        if (file === 'close-frozen.html' || file === 'close-clean.html') {
          assert.equal(await cdp.eval(`(() => {
            const f=document.querySelector('form[action="/close/reopen"]');
            f.querySelectorAll('[required]').forEach(e=>{e.type==='checkbox'?e.checked=true:e.value='Generated correction';});
            f.dispatchEvent(new Event('change',{bubbles:true}));
            return !f.querySelector('button').disabled;
          })()`), true);
        }
        if (file.startsWith('statement-') && !file.startsWith('statement-blocked-')) {
          const evidenceHref = await cdp.eval(`(() => {
            const link=document.querySelector('.period-total-link');
            const href=link.getAttribute('href');
            link.click();
            return href;
          })()`);
          const evidence = await visibleFragment(cdp, evidenceHref);
          anchors.push({file, width, stage:'evidence', ...evidence});
          await cdp.eval("document.querySelector('.period-source-details').open=false; document.querySelector('.period-source-details summary').focus()");
          await cdp.call('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49, text: ' ', unmodifiedText: ' ' });
          await cdp.call('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49 });
          assert.equal(await cdp.eval("document.querySelector('.period-source-details').open"), true);
          const expectedRow = await cdp.eval(`(() => {
            const evidence=document.getElementById(${JSON.stringify(evidenceHref.slice(1))});
            const link=evidence.querySelector('.period-reference-links a[href^="#report-row-"]');
            const href=link.getAttribute('href');
            const row=document.getElementById(href.slice(1));
            const expected={href, heading:row.querySelector('h2').textContent.trim(),
                            amount:row.querySelector('strong').textContent.trim()};
            link.click();
            return expected;
          })()`);
          const rowTarget = await visibleFragment(cdp, expectedRow.href);
          assert.equal(rowTarget.heading.text, expectedRow.heading);
          assert.equal(rowTarget.amount.text, expectedRow.amount);
          anchors.push({file, width, stage:'row', ...rowTarget});
          fs.writeFileSync(path.join(output, 'anchors.json'), JSON.stringify(anchors, null, 2));
          if (!checkedRefusals) {
            const wrongTarget = await cdp.eval(`(${fragmentState})(${JSON.stringify(evidenceHref)})`);
            assert.equal(wrongTarget.exists, true);
            assert.equal(wrongTarget.ready, false);
            const refusals = await cdp.eval(`(() => {
              const href=${JSON.stringify(expectedRow.href)};
              const state=${fragmentState};
              const amount=document.getElementById(href.slice(1)).querySelector('strong');
              const oldTransform=amount.style.transform;
              const cover=document.createElement('div');
              cover.style.cssText='position:fixed;inset:0;z-index:2147483647;background:white';
              try {
                document.body.append(cover);
                const covered=state(href).ready;
                cover.remove();
                amount.style.transform='translateY(-20000px)';
                const outOfView=state(href).ready;
                return {covered, outOfView};
              } finally {
                cover.remove();
                amount.style.transform=oldTransform;
              }
            })()`);
            assert.deepEqual(refusals, {covered:false, outOfView:false});
            assert.equal((await visibleFragment(cdp, expectedRow.href)).ready, true);
            fs.writeFileSync(path.join(output, 'refusals.json'), JSON.stringify({wrongTarget:false, ...refusals}, null, 2));
            checkedRefusals = true;
          }
          if (width === 390 && file === 'statement-household.html') {
            const screenshot = await cdp.call('Page.captureScreenshot', {format:'png', captureBeyondViewport:false});
            fs.writeFileSync(path.join(output,'statement-row-anchor-390.png'),Buffer.from(screenshot.data,'base64'));
          }
          await cdp.call('Emulation.setEmulatedMedia', {media:'print'});
          const print = await cdp.eval(`(() => {
            const details=document.querySelector('.period-source-details'); details.open=false;
            return {visible:details.querySelector('dl').checkVisibility({contentVisibilityAuto:true}),
                    height:details.querySelector('dl').getBoundingClientRect().height};
          })()`);
          assert.equal(print.visible, true, JSON.stringify(print));
          assert.ok(print.height > 0, JSON.stringify(print));
          await cdp.call('Emulation.setEmulatedMedia', {media:''});
          assert.equal(rowTarget.exists, true);
          assert.match(rowTarget.hash, /^#report-row-/);
        }
      }
    }
    assert.equal(metrics.length, 96);
    assert.equal(anchors.length, 24);
    assert.equal(checkedRefusals, true);
    assert.deepEqual(cdp.errors, []);
    fs.writeFileSync(path.join(output, 'metrics.json'), JSON.stringify(metrics, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(output, 'chrome-errors.txt'), stderr);
    throw error;
  } finally {
    if (cdp) cdp.socket.close();
    const exited = once(browser, 'exit');
    browser.kill('SIGTERM');
    await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 1500))]);
    if (browser.exitCode === null) { browser.kill('SIGKILL'); await exited; }
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(profile, { recursive: true, force: true });
    fs.writeFileSync(path.join(output, 'cleanup.json'), JSON.stringify({
      browserPid:browser.pid, exitCode:browser.exitCode, signalCode:browser.signalCode,
      profile, profileExists:fs.existsSync(profile), serverListening:server.listening,
    }, null, 2));
  }
});
