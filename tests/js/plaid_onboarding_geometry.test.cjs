/* Generated native geometry controls for the onboarding pointer driver. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {spawn} = require('node:child_process');
const net = require('node:net');
const {test, before, after} = require('node:test');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const source = fs.readFileSync(path.join(__dirname, '../browser/plaid_onboarding.cjs'), 'utf8');
const Cdp = vm.runInNewContext(source.slice(source.indexOf('class Cdp {'), source.indexOf('\nasync function firstRun')) + '; Cdp', {WebSocket, setTimeout, clearTimeout, pause});
const stopChild = vm.runInNewContext(source.slice(source.indexOf('async function stopChild('), source.indexOf('\nclass Cdp {')) + '; stopChild', {setTimeout, clearTimeout});
let chrome, c, root;
before(async () => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'fn-onboarding-geometry-'));
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  chrome = spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking', `--remote-debugging-port=${port}`, `--user-data-dir=${path.join(root, 'chrome')}`, 'about:blank'], {stdio: 'ignore'});
  for (let i = 0; i < 100; i++) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      c = new Cdp(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
      break;
    } catch { await pause(100); }
  }
  assert.ok(c, 'owned Chrome ready');
  await c.call('Page.enable');
  await c.call('Emulation.setDeviceMetricsOverride', {width: 800, height: 600, deviceScaleFactor: 1, mobile: false});
});
after(async () => {
  if (c) c.ws.close();
  if (chrome) await stopChild(chrome);
  if (root) console.log('Owned geometry fixture stopped: ' + root);
});
async function fixture() {
  const html = `<style>html{scroll-behavior:smooth}body{margin:0}button,label,summary{display:block;min-height:48px}#target{margin-top:2200px}#tail{height:1400px}</style>
    <button id="wrong">Other action</button><details id="closed"><summary>Disclosure</summary><button id="hidden">Hidden action</button></details>
    <button id="target">Requested action</button><button id="neighbor">Neighbor action</button><label><input id="include" type="checkbox">Include</label><div id="tail"></div>
    <script>window.actions=[];for(const n of document.querySelectorAll('button'))n.addEventListener('click',()=>actions.push(n.id));</script>`;
  await c.call('Page.navigate', {url: 'data:text/html,' + encodeURIComponent(html)});
  await c.until("document.readyState==='complete' && !!document.querySelector('#target')");
}
test('native smooth scroll settles before a real pointer reaches only its target', async () => {
  await fixture();
  await c.click('#target');
  assert.deepEqual(Array.from(await c.eval('actions')), ['target']);
  assert.equal(await c.eval("document.activeElement.id"), 'target');
  await c.click('#include');
  assert.equal(await c.eval("document.querySelector('#include').checked"), true, 'native checkbox toggled by pointer');
});
test('a canceled initial native scroll is re-armed once before pointer readiness', async () => {
  await fixture();
  await c.eval(`(() => {
    const target=document.querySelector('#target'),native=Element.prototype.scrollIntoView;
    window.scrollRequests=[];window.cancelBoundary=null;
    Element.prototype.scrollIntoView=function(options){
      if(this!==target)return native.call(this,options);
      scrollRequests.push({options,y:scrollY,top:this.getBoundingClientRect().top});
      native.call(this,options);
      if(scrollRequests.length===1)requestAnimationFrame(()=>{
        // A real competing native request cancels, rather than delays or mocks,
        // Chromium's pending smooth scroll without changing target or layout.
        scrollTo({top:scrollY,left:scrollX});
        cancelBoundary={y:scrollY,top:target.getBoundingClientRect().top,height:innerHeight,extent:document.scrollingElement.scrollHeight};
      });
    };
  })()`);
  await c.click('#target');
  const observed=await c.eval('({requests:scrollRequests,boundary:cancelBoundary,y:scrollY})');
  assert.equal(observed.requests.length,2,'only one bounded native re-arm');
  assert.deepEqual(JSON.parse(JSON.stringify(observed.requests.map(r=>r.options))),[{block:'center'},{block:'center'}],'keep CSS-owned native smooth scrolling');
  assert.ok(observed.boundary.top>observed.boundary.height,'first request canceled while wholly offscreen');
  assert.equal(observed.boundary.y,0,'cancellation precedes scroll arrival');
  assert.ok(observed.boundary.extent>observed.boundary.height,'target has legitimate scroll range');
  assert.ok(observed.y>0,'re-armed native scroll actually arrived');
  assert.deepEqual(Array.from(await c.eval('actions')),['target']);
  assert.equal(await c.eval('document.activeElement.id'),'target');
});
test('persistent native scroll cancellation refuses within the original budget after one re-arm', async () => {
  await fixture();
  await c.eval(`(() => {
    const target=document.querySelector('#target'),native=Element.prototype.scrollIntoView;
    window.scrollRequests=0;
    Element.prototype.scrollIntoView=function(options){
      if(this===target)scrollRequests++;
      native.call(this,options);
      requestAnimationFrame(()=>scrollTo({top:scrollY,left:scrollX}));
    };
  })()`);
  await assert.rejects(c.click('#target'),/did not settle in viewport/i);
  assert.equal(await c.eval('scrollRequests'),2,'not a scroll-until-green loop');
  assert.deepEqual(Array.from(await c.eval('actions')),[]);
});
test('a target that cannot fit the viewport never becomes pointer-ready', async () => {
  await fixture();
  await c.eval("document.querySelector('#target').style.height='1000px'");
  await assert.rejects(c.click('#target'),/did not settle in viewport/i);
  assert.deepEqual(Array.from(await c.eval('actions')),[]);
});
test('a native scroll clipped by a non-scrollable ancestor refuses pointer dispatch', async () => {
  await fixture();
  await c.eval(`(() => {
    const target=document.querySelector('#target'),clip=document.createElement('div');
    clip.style='display:flow-root;height:100px;overflow:clip';target.before(clip);clip.append(target);
    const outer=clip.getBoundingClientRect(),inner=target.getBoundingClientRect();
    return {clipped:inner.top>outer.bottom,overflow:getComputedStyle(clip).overflowY};
  })()`).then(state=>{
    assert.equal(state.clipped,true,'fixture positively places the native target beyond the ancestor clip');
    assert.equal(state.overflow,'clip');
  });
  await assert.rejects(c.click('#target'),/covered|did not settle in viewport/i);
  assert.deepEqual(Array.from(await c.eval('actions')),[]);
});
test('a replaced same-selector node is refused during native scroll settlement', async () => {
  await fixture();
  await c.eval(`(() => {
    const target=document.querySelector('#target'),native=Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView=function(options){native.call(this,options);if(this===target)requestAnimationFrame(()=>target.replaceWith(target.cloneNode(true)));};
  })()`);
  await assert.rejects(c.click('#target'),/changed or hidden/i);
  assert.deepEqual(Array.from(await c.eval('actions')),[]);
});
test('a target hidden during native scroll settlement is refused', async () => {
  await fixture();
  await c.eval(`(() => {
    const target=document.querySelector('#target'),native=Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView=function(options){native.call(this,options);if(this===target)requestAnimationFrame(()=>target.style.visibility='hidden');};
  })()`);
  await assert.rejects(c.click('#target'),/changed or hidden/i);
  assert.deepEqual(Array.from(await c.eval('actions')),[]);
});
test('covered target refuses pointer dispatch', async () => {
  await fixture();
  await c.eval("const cover=document.createElement('div');cover.style='position:fixed;inset:0;z-index:9999';document.body.append(cover)");
  await assert.rejects(c.click('#target'), /covered/i);
  assert.deepEqual(Array.from(await c.eval('actions')), []);
});
test('closed disclosure and CSS-hidden targets refuse pointer dispatch', async () => {
  await fixture();
  await assert.rejects(c.click('#hidden'), /visible|hidden/i);
  await c.eval("document.querySelector('#target').style.visibility='hidden'");
  await assert.rejects(c.click('#target'), /visible|hidden/i);
  assert.deepEqual(Array.from(await c.eval('actions')), []);
});
test('an existing wrong target coordinate is refused without scrolling or clicking', async () => {
  await fixture();
  await c.pointer('#target');
  const wrong = await c.eval("(()=>{const r=document.querySelector('#neighbor').getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2};})()");
  const before = await c.eval('scrollY');
  await assert.rejects(c.verifyPointer('#target', wrong), /target|viewport/i);
  assert.equal(await c.eval('scrollY'), before);
  assert.deepEqual(Array.from(await c.eval('actions')), []);
});
