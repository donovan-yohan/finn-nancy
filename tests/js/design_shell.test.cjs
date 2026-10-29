/* Actual Jinja shell, native navigation and first-install offline proof. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
class Cdp {
  constructor(url) {
    this.id = 0; this.pending = new Map(); this.ws = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {
      this.ws.onopen = resolve; this.ws.onerror = reject;
    });
    this.ws.onmessage = event => {
      const message = JSON.parse(event.data), item = this.pending.get(message.id);
      if (!item) return;
      this.pending.delete(message.id); clearTimeout(item.timer);
      if (message.error) item.reject(Error(JSON.stringify(message.error)));
      else item.resolve(message.result);
    };
  }
  async call(method, params = {}) {
    await this.ready;
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {this.pending.delete(id); reject(Error(method + ' timeout'));}, 10000);
      this.pending.set(id, {resolve, reject, timer});
      this.ws.send(JSON.stringify({id, method, params}));
    });
  }
  async eval(expression) {
    const result = await this.call('Runtime.evaluate', {expression, awaitPromise:true, returnByValue:true});
    assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async until(expression) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      if (await this.eval(expression)) return;
      await pause(30);
    }
    throw Error('Condition not reached: ' + expression);
  }
  close() {
    for (const item of this.pending.values()) {clearTimeout(item.timer); item.reject(Error('closed'));}
    this.pending.clear(); this.ws.close();
  }
}
async function key(cdp, key, code, virtual, native, text) {
  await cdp.call('Input.dispatchKeyEvent', {type:'keyDown', key, code,
    windowsVirtualKeyCode:virtual, nativeVirtualKeyCode:native,
    ...(text ? {text, unmodifiedText:text} : {})});
  await cdp.call('Input.dispatchKeyEvent', {type:'keyUp', key, code,
    windowsVirtualKeyCode:virtual, nativeVirtualKeyCode:native});
}
async function click(cdp, selector) {
  const point = await cdp.eval(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});
    n.scrollIntoView({block:'center',behavior:'instant'});const r=n.getBoundingClientRect();
    const x=r.x+r.width/2,y=r.y+r.height/2;
    if(!n.contains(document.elementFromPoint(x,y)))throw Error('Covered target');return {x,y};})()`);
  await cdp.call('Input.dispatchMouseEvent', {type:'mousePressed', button:'left', clickCount:1, ...point});
  await cdp.call('Input.dispatchMouseEvent', {type:'mouseReleased', button:'left', clickCount:1, ...point});
}
async function main() {
  const [site, profile, proof] = process.argv.slice(2);
  const receipt = {renders:[], offline:false, cleanup:false};
  const types = {'.css':'text/css','.js':'application/javascript','.png':'image/png','.webmanifest':'application/manifest+json'};
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    if (pathname === '/favicon.ico') {response.writeHead(204); return response.end();}
    const filename = path.resolve(site, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!filename.startsWith(site + path.sep) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) {
      response.writeHead(404); return response.end();
    }
    response.writeHead(200, {'Content-Type':types[path.extname(filename)] || 'text/html'});
    response.end(fs.readFileSync(filename));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  let closed = false, spawnError, cdp;
  const fd = fs.openSync(profile + '.stderr.log', 'w', 0o600);
  const chrome = spawn(process.env.CHROMIUM_BIN, ['--headless=new','--disable-gpu',
    '--no-first-run','--no-default-browser-check','--disable-background-networking',
    '--disable-component-update','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],
    {stdio:['ignore','ignore',fd]});
  fs.closeSync(fd);
  const stopped = new Promise(resolve => {
    chrome.once('error', error => {spawnError=error;});
    chrome.once('close', () => {closed=true;resolve();});
  });
  async function stopServer() {
    if (server.listening) await new Promise(resolve => {server.close(resolve);server.closeAllConnections();});
  }
  try {
    const portFile = path.join(profile, 'DevToolsActivePort');
    const deadline = Date.now() + 20000;
    while (!fs.existsSync(portFile) && Date.now() < deadline && !closed && !spawnError) await pause(50);
    assert.ok(fs.existsSync(portFile), `Chrome readiness: ${spawnError || fs.readFileSync(profile+'.stderr.log','utf8')}`);
    const port = fs.readFileSync(portFile,'utf8').split('\n')[0];
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`, {signal:AbortSignal.timeout(5000)})).json();
    cdp = new Cdp(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
    await cdp.call('Page.enable'); await cdp.call('Runtime.enable'); await cdp.call('Network.enable');
    // No fixture can request an external asset, even if a product regression adds one.
    await cdp.call('Network.setBlockedURLs', {urlPatterns:[
      {urlPattern:base+'/*',block:false}, {urlPattern:'http://*/*',block:true},
      {urlPattern:'https://*/*',block:true}]});
    for (const width of [320,390,1440]) {
      await cdp.call('Emulation.setDeviceMetricsOverride', {width,height:900,deviceScaleFactor:1,mobile:false});
      for (const theme of ['light','dark']) {
        await cdp.call('Network.setCookie', {url:base,name:'finn_nancy_theme',value:theme,path:'/'});
        for (const route of ['/upload','/connections']) {
          await cdp.call('Page.navigate', {url:base+route});
          await cdp.until(`location.pathname===${JSON.stringify(route)} && document.readyState==='complete' && !!document.querySelector('[data-theme-select]')`);
          const geometry = await cdp.eval(`(()=>{
            const main=document.querySelector('main'),header=document.querySelector('.fn-header'),nav=document.querySelector('.fn-nav');
            const rect=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom};};
            const targets=[...document.querySelectorAll('.fn-nav>a,.fn-more>summary,[data-theme-select]')];
            return {theme:document.documentElement.dataset.bsTheme,overflow:document.documentElement.scrollWidth>innerWidth,
              main:rect(main),header:rect(header),position:getComputedStyle(header).position,nav:rect(nav),
              body:getComputedStyle(document.body).backgroundColor,appearanceHidden:document.querySelector('.theme-control').hidden,
              ink:getComputedStyle(document.body).color,
              planningInk:document.querySelector('#nancy-neutral') ? getComputedStyle(document.querySelector('#nancy-neutral')).color : null,
              targets:targets.map(n=>({height:n.getBoundingClientRect().height,width:n.getBoundingClientRect().width})),
              h1:document.querySelectorAll('h1').length,selects:document.querySelectorAll('[data-theme-select]').length};})()`);
          assert.equal(geometry.theme,theme); assert.equal(geometry.overflow,false,JSON.stringify(geometry));
          if (geometry.planningInk) assert.equal(geometry.planningInk,geometry.ink,'Planning facts must not inherit orange from a page-level accent class');
          assert.equal(geometry.position,'static'); assert.ok(geometry.main.y >= geometry.header.bottom);
          assert.equal(geometry.appearanceHidden,false); assert.equal(geometry.selects,1); assert.equal(geometry.h1,1);
          assert.ok(geometry.targets.every(target=>target.height>=48 && target.width>0));
          assert.equal(geometry.body,theme==='dark'?'rgb(0, 0, 0)':'rgb(255, 255, 255)');
          if(width===1440) assert.equal(geometry.main.x,120);
          if(width<768) assert.equal(geometry.main.x,16);
          // Pointer toggles native More, which reflows instead of covering content.
          await click(cdp,'.fn-more>summary');
          await cdp.until("document.querySelector('.fn-more').open");
          assert.equal(await cdp.eval('document.documentElement.scrollWidth>innerWidth'),false);
          assert.equal(await cdp.eval("document.querySelector('main').getBoundingClientRect().y>=document.querySelector('.fn-header').getBoundingClientRect().bottom"),true);
          assert.ok(await cdp.eval("(()=>{const n=document.querySelector('.fn-more a[href=\"/connections\"]'),r=n.getBoundingClientRect();return n.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()"));
          // Native Enter closes the focused disclosure, Space opens it again.
          await key(cdp,'Enter','Enter',13,36,'\r');
          await cdp.until("!document.querySelector('.fn-more').open");
          await key(cdp,' ','Space',32,49,' ');
          await cdp.until("document.querySelector('.fn-more').open");
          await key(cdp,'Enter','Enter',13,36,'\r');
          await cdp.until("!document.querySelector('.fn-more').open");
          await cdp.eval('scrollTo({top:0,behavior:"instant"})');
          const shot = await cdp.call('Page.captureScreenshot', {format:'png'});
          const image=`${route.slice(1)}-${width}-${theme}.png`;
          fs.writeFileSync(path.join(proof,image),Buffer.from(shot.data,'base64'));
          receipt.renders.push({route,width,theme,image,geometry});
        }
      }
    }
    receipt.zoom = [];
    for (const width of [320,1440]) {
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
      for (const theme of ['light','dark']) {
        await cdp.call('Network.setCookie',{url:base,name:'finn_nancy_theme',value:theme,path:'/'});
        await cdp.call('Page.navigate',{url:base+'/connections'});
        await cdp.until("location.pathname==='/connections' && document.readyState==='complete'");
        await cdp.eval("document.documentElement.style.fontSize='200%'");
        const zoom = await cdp.eval(`(()=>{const input=document.querySelector('#member-name'),button=document.querySelector('button[type=submit]');
          return {body:parseFloat(getComputedStyle(document.body).fontSize),overflow:document.documentElement.scrollWidth>innerWidth,
            inputHeight:input.getBoundingClientRect().height,buttonHeight:button.getBoundingClientRect().height,
            theme:document.documentElement.dataset.bsTheme};})()`);
        assert.equal(zoom.body,30);assert.equal(zoom.overflow,false,JSON.stringify(zoom));
        assert.ok(zoom.inputHeight>=48 && zoom.buttonHeight>=48);assert.equal(zoom.theme,theme);
        receipt.zoom.push({width,theme,...zoom});
      }
    }
    await cdp.eval("document.documentElement.style.fontSize=''");
    // A replaced Appearance control reconnects after HTMX, without a second store.
    await cdp.eval(`(()=>{const control=document.querySelector('.theme-control');
      control.replaceWith(control.cloneNode(true));document.body.dispatchEvent(new Event('htmx:afterSwap',{bubbles:true}));
      const select=document.querySelector('[data-theme-select]');select.value='light';
      select.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    assert.equal(await cdp.eval('document.documentElement.dataset.bsTheme'),'light');
    await cdp.eval(`(()=>{const select=document.querySelector('[data-theme-select]');select.value='dark';select.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await cdp.eval(`(()=>{const select=document.querySelector('[data-theme-select]');select.focus();
      document.body.dispatchEvent(new Event('htmx:afterSwap',{bubbles:true}));})()`);
    assert.equal(await cdp.eval("document.activeElement.matches('[data-theme-select]')"),true);
    await cdp.eval("document.activeElement.blur()");
    // HTMX closes an open quick-picker without removing its file/provenance hooks.
    await click(cdp,'.fn-more>summary');
    await cdp.until("document.querySelector('.fn-more').open");
    await click(cdp,'.fn-quick-capture>summary');
    await cdp.until("document.querySelector('details.fab').open");
    await cdp.eval("document.body.dispatchEvent(new Event('htmx:afterOnLoad'))");
    assert.equal(await cdp.eval("document.querySelector('details.fab').open"),false);
    assert.deepEqual(await cdp.eval("['capture-camera-input','capture-files-input'].map(id=>{const n=document.getElementById(id);return {source:n.dataset.source,intent:n.dataset.intent,name:n.name}})"),[
      {source:'camera',intent:'receipt',name:'files'},{source:'file',intent:'receipt',name:'files'}]);
    // Normal notices remain readable with a 48px dismiss target, not Bootstrap's hidden .toast.
    await cdp.eval(`document.querySelector('#toast').innerHTML='<div class="capture-toast"><a class="capture-toast-link" href="/processing">Generated file Pending · View processing</a><button class="capture-toast-dismiss">Dismiss</button></div>'`);
    assert.ok(await cdp.eval("document.querySelector('.capture-toast-dismiss').getBoundingClientRect().height>=48"));
    await cdp.eval("document.querySelector('#toast').replaceChildren()");
    await cdp.until('!!navigator.serviceWorker.controller');
    await cdp.eval('navigator.serviceWorker.ready.then(()=>true)');
    for (const asset of ['foundation/vendor/bootstrap.min.css','foundation/tokens.css','foundation/components.css','generated.css']) {
      assert.equal(await cdp.eval(`caches.match('/static/${asset}').then(Boolean)`),true,asset+' first-install precache');
    }
    await cdp.call('Network.clearBrowserCache');
    await cdp.call('Network.setCacheDisabled',{cacheDisabled:true});
    await stopServer();
    await cdp.call('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:0,uploadThroughput:0});
    await cdp.call('Page.navigate',{url:base+'/processing?offline-proof=1'});
    await cdp.until("location.search==='?offline-proof=1' && document.readyState==='complete'");
    assert.equal(await cdp.eval('document.documentElement.dataset.bsTheme'),'dark');
    assert.equal(await cdp.eval('getComputedStyle(document.body).backgroundColor'),'rgb(0, 0, 0)');
    assert.equal(await cdp.eval("document.querySelector('[data-theme-select]').value"),'dark');
    assert.equal(await cdp.eval("getComputedStyle(document.querySelector('.fn-header')).position"),'static');
    await click(cdp,'.fn-more>summary');
    await cdp.until("document.querySelector('.fn-more').open");
    assert.equal(await cdp.eval("document.querySelector('#processing-nav-count').closest('a').pathname"),'/processing');
    receipt.offline=true;
  } finally {
    if(cdp) cdp.close();
    if(!closed) chrome.kill('SIGTERM');
    await Promise.race([stopped,pause(1000)]);
    if(!closed) {chrome.kill('SIGKILL');await stopped;}
    await stopServer();
    receipt.cleanup=closed && !server.listening;
    fs.writeFileSync(path.join(proof,'browser-report.json'),JSON.stringify(receipt,null,2));
  }
  console.log(JSON.stringify({renders:receipt.renders.length,offline:receipt.offline,cleanup:receipt.cleanup}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
