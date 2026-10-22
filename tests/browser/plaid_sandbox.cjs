/* Rendered candidate checks. Generated transport evidence, not live Plaid proof. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const pause = ms => new Promise(r => setTimeout(r, ms));
async function freePort() {
  const s = net.createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r));
  const port = s.address().port; await new Promise(r => s.close(r)); return port;
}
class Cdp {
  constructor(url) {
    this.id = 0; this.pending = new Map(); this.listeners = [];
    this.ws = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {this.ws.onopen = resolve; this.ws.onerror = reject;});
    this.ws.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.id) { const p = this.pending.get(m.id); this.pending.delete(m.id); m.error ? p.reject(Error(m.error.message)) : p.resolve(m.result); }
      else for (const f of this.listeners) f(m);
    };
  }
  async call(method, params={}) {
    await this.ready; const id = ++this.id;
    return new Promise((resolve,reject) => { this.pending.set(id,{resolve,reject}); this.ws.send(JSON.stringify({id,method,params})); });
  }
  async eval(expression) {
    const r = await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(r.exceptionDetails) throw Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
  async until(expression) {
    for(let i=0;i<100;i++) { if(await this.eval(expression)) return; await pause(50); }
    throw Error('browser condition: '+expression+' '+JSON.stringify(await this.eval("({url:location.href,text:document.body.innerText.slice(0,2000)})")));
  }
}
async function merchantChecks(cdp, base, artifacts) {
  const cases=[];
  for(const theme of ['dark','light']) {
    for(const width of [390,1440]) {
      await cdp.call('Page.navigate',{url:base+'/activity?q=Northstar'});
      await cdp.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
      await cdp.eval(`(()=>{const s=document.querySelector('[data-theme-select]');s.value=${JSON.stringify(theme)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
      const compact=await cdp.eval(`(()=>{const row=document.querySelector('#sandbox-staged > details');return {
        rows:document.querySelectorAll('#sandbox-staged > details').length,
        name:row?.querySelector('summary .sandbox-merchant').textContent,
        rawVisible:document.body.innerText.includes('CARD 0042 GENERATED NORTHSTAR MARKET'),
        externalImages:[...document.images].filter(n=>new URL(n.src).origin!==location.origin).length,
        externalLinks:[...document.querySelectorAll('a[href]')].filter(n=>new URL(n.href).origin!==location.origin).length,
        overflow:document.documentElement.scrollWidth>innerWidth
      };})()`);
      assert.equal(compact.rows,1,'normalized merchant is searchable');
      assert.equal(compact.name,'Generated Northstar Market');
      assert.equal(compact.rawVisible,false,'raw bank text stays out of the compact scan view');
      assert.equal(compact.externalImages,0,'do not add remote logo requests');
      assert.equal(compact.externalLinks,0,'do not silently add provider website links');
      assert.equal(compact.overflow,false);
      await cdp.eval("document.querySelector('#sandbox-staged > details').open=true;document.querySelectorAll('#sandbox-staged details').forEach(n=>n.open=true)");
      const expanded=await cdp.eval(`(()=>{const row=document.querySelector('#sandbox-staged > details');return {
        text:row.innerText,
        suggestion:(()=>{const probe=document.createElement('span');probe.style.color='var(--nancy)';row.append(probe);const color=getComputedStyle(probe).color;probe.remove();return [...row.querySelectorAll('.nancy')].some(n=>getComputedStyle(n).color===color && n.textContent.toLowerCase().includes('grocer'));})(),
        overflow:document.documentElement.scrollWidth>innerWidth
      };})()`);
      assert.ok(expanded.text.includes('CARD 0042 GENERATED NORTHSTAR MARKET'),'original description retained');
      assert.ok(expanded.text.includes('Generated merchant 001'),'legacy description retained separately');
      assert.ok(expanded.text.includes('Generated Delivery'),'marketplace is inspectable separately from merchant');
      assert.ok(expanded.text.includes('generated-merchant-entity'),'merchant entity survives source disclosure');
      assert.ok(expanded.text.toLowerCase().includes('suggest'),'category remains a suggestion');
      assert.equal(expanded.suggestion,true,'suggested category uses interpretation accent');
      assert.equal(expanded.overflow,false);
      await cdp.eval('scrollTo(0,0)');
      const shot=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(artifacts,`merchant-${theme}-${width}.png`),Buffer.from(shot.data,'base64'));
      cases.push({theme,width,normalizedName:compact.name,noExternalImages:true,sourceRetained:true});
    }
  }
  await cdp.call('Page.navigate',{url:base+'/activity?q=Generated%20merchant%20001'});
  await cdp.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await cdp.eval("document.querySelectorAll('#sandbox-staged > details').length"),1,'legacy description remains searchable');
  await cdp.call('Page.navigate',{url:base+'/activity?q=CARD%200042'});
  await cdp.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await cdp.eval("document.querySelectorAll('#sandbox-staged > details').length"),1,'original bank description remains searchable');
  await cdp.call('Page.navigate',{url:base+'/activity?q=Generated%20excluded'});
  await cdp.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await cdp.eval("document.querySelector('#sandbox-staged .sandbox-merchant').textContent"),'Generated excluded merchant','missing enrichment falls back honestly');
  await cdp.call('Page.navigate',{url:base+'/'});
  await cdp.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.ok(await cdp.eval("[...document.querySelectorAll('#sandbox-recent .sandbox-merchant')].some(n=>n.textContent==='Generated Northstar Market')"),'Home uses the same preferred merchant');
  fs.writeFileSync(path.join(artifacts,'merchant-browser.json'),JSON.stringify({kind:'generated Plaid enrichment rendering',cases},null,2));
}
async function designChecks(cdp, base, artifacts) {
  const measures = [];
  async function visit(route) {
    await cdp.call('Page.navigate', {url: base + route});
    await cdp.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  }
  async function appearance(value) {
    await cdp.eval(`(()=>{const s=document.querySelector('[data-theme-select]');s.value=${JSON.stringify(value)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  }
  await cdp.call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
  await visit('/activity');
  assert.equal(await cdp.eval("Boolean(document.querySelector('[data-theme-select]'))"),true,'a shared appearance control is available');
  await appearance('system');
  for (const theme of ['dark','light']) {
    await cdp.call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
    await cdp.until(`getComputedStyle(document.body).backgroundColor===${JSON.stringify(theme==='dark'?'rgb(0, 0, 0)':'rgb(255, 255, 255)')}`);
  }
  for (const theme of ['dark','light']) {
    await appearance(theme);
    await visit('/activity?q=Generated%20merchant');
    assert.equal(await cdp.eval("document.querySelector('[data-theme-select]').value"),theme,'explicit appearance survives navigation');
    assert.equal(await cdp.eval("getComputedStyle(document.body).backgroundColor"),theme==='dark'?'rgb(0, 0, 0)':'rgb(255, 255, 255)');
    const contrast = await cdp.eval(`(()=>{
      const style=getComputedStyle(document.documentElement);
      function rgb(value){const n=document.createElement('span');n.style.color=value;document.body.append(n);const c=getComputedStyle(n).color.match(/[\\d.]+/g).slice(0,3).map(Number);n.remove();return c;}
      function luminance(c){return c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);}
      function ratio(a,b){const x=[luminance(rgb(a)),luminance(rgb(b))].sort((a,b)=>a-b);return (x[1]+.05)/(x[0]+.05);}
      const canvas=style.getPropertyValue('--bg').trim(),panel=style.getPropertyValue('--panel-2').trim();
      return Object.fromEntries(['--finn','--nancy','--muted'].map(key=>[key,Math.min(ratio(style.getPropertyValue(key).trim(),canvas),ratio(style.getPropertyValue(key).trim(),panel))]));
    })()`);
    for (const [name,value] of Object.entries(contrast)) assert.ok(value>=4.5,`${theme} ${name} contrast ${value}`);
    for (const width of [320,390,768,1440]) {
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
      const geometry=await cdp.eval(`(()=>{const rows=[...document.querySelectorAll('#sandbox-staged > .sandbox-transaction > summary')];return {
        overflow:document.documentElement.scrollWidth>innerWidth,
        heights:rows.slice(0,5).map(n=>n.getBoundingClientRect().height),
        first:rows[0]?.getBoundingClientRect().top,
        visible:rows.filter(n=>{const r=n.getBoundingClientRect();return r.top>=0&&r.bottom<760}).length,
        slots:rows.every(n=>['.sandbox-merchant','.sandbox-account-label','.sandbox-date','.sandbox-state','.sandbox-money'].every(s=>n.querySelector(s))),
        repeated:['Imported activity','Bank imports · not booked'].filter(t=>document.body.innerText.includes(t)),
        targets:[...document.querySelectorAll('button,a,summary,input:not([type=checkbox]),select')].filter(n=>n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden').filter(n=>{const r=n.getBoundingClientRect();return r.width<43.9||r.height<43.9}).map(n=>[n.tagName,n.textContent.slice(0,40)])
      };})()`);
      assert.equal(geometry.overflow,false,`${theme}/${width} page overflow`);
      assert.equal(geometry.slots,true,'named transaction slots carry factual fields');
      assert.deepEqual(geometry.repeated,[],'remove redundant page narration');
      assert.deepEqual(geometry.targets,[],`${theme}/${width} touch targets`);
      assert.ok(geometry.heights.every(h=>h>=44&&h<=(width<851?100:64)),`${theme}/${width} row heights ${geometry.heights}`);
      if(width===390){assert.ok(geometry.first<300,`first mobile row ${geometry.first}`);assert.ok(geometry.visible>=5,`mobile visible rows ${geometry.visible}`);}
      if(width===1440)assert.ok(geometry.visible>=8,`desktop visible rows ${geometry.visible}`);
      measures.push({theme,width,contrast,...geometry});
      await cdp.call('Page.bringToFront');
      await cdp.eval("document.querySelector('#sandbox-staged > .sandbox-transaction > summary').focus()");
      assert.equal(await cdp.eval("document.activeElement.matches('#sandbox-staged > .sandbox-transaction > summary')"),true,'transaction disclosure receives focus');
      await cdp.call('Input.dispatchKeyEvent',{type:'rawKeyDown',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:49});
      await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:49});
      await cdp.until("document.querySelector('#sandbox-staged > .sandbox-transaction').open");
      assert.equal(await cdp.eval("document.querySelector('#sandbox-staged > .sandbox-transaction').innerText.includes('Unknown · not booked')"),true,'accounting uncertainty survives compact rows');
      assert.equal(await cdp.eval('document.documentElement.scrollWidth<=innerWidth'),true,'expanded evidence does not overflow');
      await cdp.eval("document.querySelector('#sandbox-staged > .sandbox-transaction').open=false;scrollTo(0,0)");
      const shot=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(artifacts,`design-${theme}-${width}.png`),Buffer.from(shot.data,'base64'));
    }
  }
  await appearance('system');
  await cdp.call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
  await visit('/activity');
  assert.equal(await cdp.eval("document.querySelector('[data-theme-select]').value"),'system');
  const storage=await cdp.eval("(async()=>({local:localStorage.length,session:sessionStorage.length,caches:(await caches.keys()).length,workers:(await navigator.serviceWorker.getRegistrations()).length}))()");
  assert.deepEqual(storage,{local:0,session:0,caches:0,workers:0});
  fs.writeFileSync(path.join(artifacts,'design-browser.json'),JSON.stringify({kind:'isolated generated redesign QA',measures,storage},null,2));
}
async function productPages(cdp, base, artifacts) {
  for (const [route,title] of [['/','Home'],['/activity','Activity'],['/accounts','Accounts'],['/connections','Connections']]) {
    await cdp.call('Page.navigate',{url:base+route});
    await cdp.until("document.readyState==='complete'");
    assert.equal(await cdp.eval('location.pathname'),route,'destination must not redirect');
    assert.equal(await cdp.eval("document.querySelector('h1').textContent"),title);
    await cdp.until("document.querySelector('.sandbox-page').dataset.loaded==='true'");
    assert.equal(await cdp.eval("document.querySelector('.tabbar [aria-current=page]').textContent.trim()"),title);
    assert.equal(await cdp.eval("getComputedStyle(document.querySelector('#sandbox-retry')).display"),'none','hidden retry must not render');
    assert.equal(await cdp.eval("document.querySelectorAll('.sandbox-badge').length"),1);
    assert.equal(await cdp.eval("Boolean(document.querySelector('script[src^=\"https://cdn.plaid.com\"]'))"),false);
    assert.equal(await cdp.eval("[...document.querySelectorAll('button')].every(b=>b.classList.contains('primary')||b.classList.contains('ghost'))"),true,'use product button primitives');
    for (const width of [390,1440]) {
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
      assert.equal(await cdp.eval('document.documentElement.scrollWidth<=innerWidth'),true,route+' overflow');
      if(width===390) assert.equal(await cdp.eval("document.querySelector('.sandbox-page .card').getBoundingClientRect().top<200"),true,'mobile chrome must not consume the first screen');
      assert.equal(await cdp.eval("getComputedStyle(document.querySelector('.tabbar')).gridTemplateColumns.split(' ').length"),4,'four equally sized navigation destinations');
      if(route==='/activity' && process.argv.includes('--generated')) assert.equal(await cdp.eval("document.querySelector('[data-staged-row]').getBoundingClientRect().top < innerHeight-100"),true,'mobile activity should show data before a full screen of filters');
      if(width===390 && process.argv.includes('--generated') && (route==='/'||route==='/activity')) assert.equal(await cdp.eval("document.querySelector('.sandbox-wide-amount > .sandbox-money').getBoundingClientRect().width>200"),true,'give extreme cents a full readable line');
      const undersized = await cdp.eval("[...document.querySelectorAll('button,a,summary,input:not([type=checkbox]),select,.sandbox-check')].filter(n=>n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden').filter(n=>{const r=n.getBoundingClientRect();return r.height<43.9||r.width<43.9}).map(n=>[n.tagName,n.textContent.slice(0,50),n.getBoundingClientRect().width,n.getBoundingClientRect().height])");
      assert.deepEqual(undersized,[],route+' touch targets');
      const shot=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
      fs.writeFileSync(path.join(artifacts,`product-${title.toLowerCase()}-${width}.png`),Buffer.from(shot.data,'base64'));
      if(width===390) {
        const viewport=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
        fs.writeFileSync(path.join(artifacts,`product-${title.toLowerCase()}-${width}-viewport.png`),Buffer.from(viewport.data,'base64'));
        await cdp.eval("document.querySelectorAll('.sandbox-page details').forEach(d=>d.open=true)");
        assert.equal(await cdp.eval('document.documentElement.scrollWidth<=innerWidth'),true,route+' expanded details overflow');
        assert.equal(await cdp.eval("[...document.querySelectorAll('.sandbox-page button,.sandbox-page input:not([type=checkbox]),.sandbox-page select,.sandbox-page summary')].filter(n=>n.getClientRects().length).every(n=>n.getBoundingClientRect().height>=44)"),true,route+' expanded control targets');
        await cdp.eval("document.querySelectorAll('.sandbox-page details').forEach(d=>d.open=false)");
      }
    }
  }
}
async function generatedProductChecks(cdp, base, artifacts) {
  async function visit(route) {
    await cdp.call('Page.navigate',{url:base+route});
    await cdp.until(`location.pathname===${JSON.stringify(route.split('?')[0])} && document.querySelector('.sandbox-page')?.dataset.loaded==='true'`);
  }
  async function count(expected) {await cdp.until(`document.querySelectorAll('#sandbox-staged [data-staged-row]').length===${expected}`);}
  await visit('/connections');
  assert.equal(await cdp.eval("document.querySelector('#setup-details').open"),false);
  assert.ok((await cdp.eval("document.querySelector('#sandbox-attempts').parentElement.querySelector('summary').textContent")).includes('1 uncertain'),'uncertain exchange must remain visible after reload');
  assert.equal(await cdp.eval("document.querySelector('#sandbox-attempts').parentElement.open"),true,'unresolved exchange recovery must be expanded by default');
  assert.equal(await cdp.eval("(()=>{const d=[...document.querySelectorAll('.sandbox-connection dt')].filter(n=>n.textContent==='Last bank update').at(-1);return d.nextElementSibling.textContent})()"),'Unknown','missing bank refresh time is unknown, not never');
  await cdp.eval("const s=document.querySelector('#link-owner');s.value=[...s.options].find(o=>o.textContent==='Generated member Beta').value;document.querySelector('#link-consent').checked=true;s.dispatchEvent(new Event('change'))");
  assert.equal(await cdp.eval("document.querySelector('#link-consent').checked"),false,'changing member clears consent');
  await cdp.eval("document.querySelector('#link-consent').click()");
  assert.equal(await cdp.eval("document.querySelector('#sandbox-link').disabled"),true,'do not blindly relink uncertain member even with normal Link consent');
  const uncertainAttempt = await cdp.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.link_attempts.find(a=>a.status==='uncertain'))");
  assert.equal(uncertainAttempt.acknowledged_at,null);
  assert.equal(await cdp.eval("document.querySelectorAll('#sandbox-attempts input[type=checkbox]').length"),1,'manual dashboard acknowledgement needs a distinct explicit checkbox');
  assert.equal(await cdp.eval("document.querySelector('#sandbox-attempts [data-action=acknowledge-exchange]').disabled"),true);
  await cdp.eval("document.querySelector('#sandbox-attempts details').open=true");
  assert.equal(await cdp.eval(`document.querySelector('#sandbox-attempts').innerText.includes(${JSON.stringify(uncertainAttempt.id)})`),true,'show exact attempt identity in details');
  await cdp.eval("document.querySelector('#sandbox-attempts input[type=checkbox]').click();document.querySelector('#sandbox-attempts [data-action=acknowledge-exchange]').click()");
  await cdp.until("document.querySelector('#sandbox-message').textContent.startsWith('Dashboard check recorded.') && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await cdp.eval("document.querySelector('#link-consent').checked"),false,'recording recovery must not grant fresh Link consent');
  await visit('/connections');
  await cdp.eval("const owner=document.querySelector('#link-owner');owner.value=[...owner.options].find(o=>o.textContent==='Generated member Beta').value;owner.dispatchEvent(new Event('change'))");
  assert.equal(await cdp.eval("document.querySelector('#sandbox-link').disabled"),true,'acknowledged uncertainty still requires fresh consent after reload');
  assert.equal(await cdp.eval("document.querySelector('#link-consent').checked"),false,'new Link still requires normal owner consent');
  const attemptsBeforeConsent = await cdp.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.link_attempts.length)");
  await cdp.eval("document.querySelector('#sandbox-link-form').requestSubmit()");
  assert.equal(await cdp.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.link_attempts.length)"),attemptsBeforeConsent,'no fresh Link without explicit owner consent');
  await cdp.eval("document.querySelector('.sandbox-connect-panel').open=true;document.querySelector('#link-consent').click()");
  assert.equal(await cdp.eval("document.querySelector('#sandbox-link').disabled"),false,'separately consented Connect remains available');
  assert.equal(await cdp.eval("document.querySelector('#sandbox-attempts').textContent.includes('Remote outcome remains unverified')"),true);
  assert.equal(await cdp.eval("document.querySelectorAll('#sandbox-attempts input[type=checkbox]').length"),0,'acknowledged history must not offer replay or redundant recovery');
  const retainedAttempt = await cdp.eval(`fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.link_attempts.find(a=>a.id===${JSON.stringify(uncertainAttempt.id)}))`);
  assert.equal(retainedAttempt.status,'uncertain');
  assert.ok(retainedAttempt.acknowledged_at);
  assert.equal(retainedAttempt.connection_id,null);
  assert.equal(await cdp.eval("Boolean(document.querySelector('script[src^=\"https://cdn.plaid.com\"]'))"),false,'acknowledgement never automatically launches Link');
  await visit('/');
  assert.equal(await cdp.eval("document.querySelectorAll('#sandbox-recent [data-staged-row]').length"),8);
  assert.deepEqual(await cdp.eval("[...document.querySelectorAll('.stat-number')].map(n=>Number(n.textContent))"),[2,2,115+3+1+1-1,1]);
  assert.equal(await cdp.eval("document.body.innerText.includes('same-account-id')"),false,'raw IDs stay collapsed');
  await visit('/activity'); await count(50);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-result-count').textContent"),`1–50 of ${115+3+1+1-1} transactions`);
  await cdp.eval("document.querySelector('#sandbox-next').click()"); await count(50);
  await cdp.until("new URL(location.href).searchParams.get('offset')==='50' && document.querySelector('#sandbox-result-count').textContent.startsWith('51–100')");
  await cdp.eval("document.querySelector('#sandbox-next').click()"); await count(19);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-next').disabled"),true);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-page-status').textContent"),'End of results');
  await visit('/activity?q=Generated%20excluded'); await count(1);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-staged .sandbox-money').textContent"),'+20.01 USD');
  assert.equal(await cdp.eval("document.body.innerText.includes('Excluded')"),true,'fresh excluded rows must remain visible');
  await visit('/activity?q=precise'); await count(1);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-staged .sandbox-money').textContent"),'−89,999,999,999,999.99 CAD');
  await visit('/activity?q=unknown%20currency'); await count(1);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-staged .sandbox-money').textContent"),'+20.01 Unknown currency');
  await cdp.eval("document.querySelector('.sandbox-transaction').open=true");
  assert.equal(await cdp.eval("document.body.innerText.includes('Unknown · not booked')"),true);
  assert.equal(await cdp.eval("document.body.innerText.includes('same-account-id')"),false);
  await cdp.eval("document.querySelector('.sandbox-transaction details').open=true");
  assert.equal(await cdp.eval("document.body.innerText.includes('same-account-id')"),true);
  await visit('/activity?state=pending'); await count(1);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-staged .sandbox-money').textContent"),'−0.01 CAD');
  await visit('/activity?state=removed'); await count(1);
  await visit('/activity?state=superseded'); await count(1);
  await visit('/activity?inclusion=excluded'); await count(1);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-advanced-filters > summary').textContent"),'Filters · 1 active');
  await visit('/activity?date_from=2030-01-01'); await count(0);
  assert.equal(await cdp.eval("document.querySelector('#sandbox-result-count').textContent"),'0 transactions');
  await visit('/accounts');
  const accountHref=await cdp.eval("[...document.querySelectorAll('.sandbox-account-group')].find(a=>a.querySelector('h2').textContent.includes('Generated member Beta')).querySelector('a[href^=\"/activity\"]').getAttribute('href')");
  assert.ok(new URL(accountHref,base).searchParams.has('connection_id'));
  assert.equal(new URL(accountHref,base).searchParams.get('account_id'),'same-account-id');
  await visit(accountHref); await count(1);
  // Saved mapping is read back from the real metadata endpoint after the UI action.
  await visit('/accounts');
  await cdp.eval("(()=>{const a=[...document.querySelectorAll('.sandbox-account-group')].find(a=>a.querySelector('h2').textContent.includes('Generated member Beta')).querySelector('.sandbox-account');a.querySelector('[data-action=edit-account]').click();const f=[...document.querySelectorAll('.sandbox-account-group')].find(a=>a.querySelector('h2').textContent.includes('Generated member Beta')).querySelector('form');f.querySelector('select').value='shared';f.querySelector('input').checked=true;f.querySelector('select').dispatchEvent(new Event('change'));f.requestSubmit()})()");
  await cdp.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && [...document.querySelectorAll('.sandbox-account-group')].find(a=>a.querySelector('h2').textContent.includes('Generated member Beta')).querySelector('form')===null");
  const mappedConnection=new URL(accountHref,base).searchParams.get('connection_id');
  assert.deepEqual(await cdp.eval(`fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>{const a=s.accounts.find(a=>a.connection_id===${JSON.stringify(mappedConnection)});return [Boolean(a.selected),a.ownership]})`),[true,'shared']);
  // Fast new search must not be overwritten by a slow, older response, even without abort support.
  await visit('/activity'); await count(50);
  await cdp.eval(`window.originalFetch=window.fetch; window.fetch=async function(input,options){const url=String(input); if(url.includes('/activity?')&&url.includes('q=slow')){await new Promise(r=>setTimeout(r,350));return window.originalFetch(input,{...options,signal:undefined});} return window.originalFetch(input,options);}; const f=document.querySelector('#sandbox-filters');f.elements.q.value='slow';f.requestSubmit();f.elements.q.value='excluded';f.requestSubmit();`);
  await count(1); await pause(500);
  assert.equal(await cdp.eval("document.querySelectorAll('[data-staged-row]').length"),1);
  assert.equal(await cdp.eval("document.querySelector('[data-staged-row]').textContent.includes('Generated excluded merchant')"),true);
  assert.equal(await cdp.eval("new URL(location.href).searchParams.get('q')"),'excluded');
  // Error and retry are real rendered states, not an indefinite loading message.
  await cdp.eval("window.fetch=async()=>new Response('{}',{status:503});document.querySelector('#sandbox-filters').requestSubmit()");
  await cdp.until("document.querySelector('#sandbox-result-count').textContent==='Activity could not be loaded.'");
  assert.equal(await cdp.eval("getComputedStyle(document.querySelector('#sandbox-retry')).display!=='none'"),true);
  await cdp.eval("window.fetch=window.originalFetch;document.querySelector('#sandbox-retry').click()"); await count(1);
  fs.writeFileSync(path.join(artifacts,'product-generated-browser.json'),JSON.stringify({kind:'generated product fixtures; no live Plaid',counts:{connections:2,accounts:2,current:119,pending:1},checks:['four destinations','mobile geometry','counts','pagination/end','search/date/state/inclusion','excluded accounts','precision/unknown currency','source disclosure','mapping persistence','stale request','error/retry']},null,2));
}
async function main() {
  // Canonicalize macOS's /var TMPDIR alias; the handoff correctly refuses symlink ancestors.
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'fn-sandbox-browser-')));
  const artifacts = process.env.SANDBOX_QA_ARTIFACTS || root;
  fs.mkdirSync(artifacts,{recursive:true});
  const port = await freePort(); const cdpPort = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const python = path.resolve('.venv/bin/python');
  const generated = process.argv.includes('--generated');
  const handoff = process.argv.includes('--handoff');
  let privateFile='none';
  if(handoff){const dir=path.join(root,'private');fs.mkdirSync(dir,{mode:0o700});privateFile=path.join(dir,'plaid-sandbox.json');fs.writeFileSync(privateFile,JSON.stringify({environment:'sandbox',client_id:'generated-client',secret:'generated-secret'}),{mode:0o600});}
  let serverCode = "import sys; from pathlib import Path; import uvicorn; from app.plaid_sandbox.web import create_app; " + (generated ? "import httpx; from tests.test_plaid_sandbox_service import GeneratedPlaid,page,row; provider=GeneratedPlaid(); provider.pages=[page([row()]),page(),page([row()]),page()]; transport=httpx.MockTransport(provider); " : "transport=None; ") + "uvicorn.run(create_app(Path(sys.argv[1]),port=int(sys.argv[2]),transport=transport,credentials_file=Path(sys.argv[3]) if sys.argv[3]!='none' else None),host='127.0.0.1',port=int(sys.argv[2]),access_log=False,log_level='critical')";
  if(generated && process.argv.includes('--product')) serverCode = "import sys; from pathlib import Path; import uvicorn; from tests.browser.plaid_product_fixture import generated_product_app; uvicorn.run(generated_product_app(Path(sys.argv[1]),int(sys.argv[2])),host='127.0.0.1',port=int(sys.argv[2]),access_log=False,log_level='critical')";
  const server = spawn(python,['-c',serverCode,path.join(root,'data'),String(port),privateFile],{env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root},stdio:['ignore','ignore','pipe']});
  const chrome = spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-background-networking',`--remote-debugging-port=${cdpPort}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'
  ],{stdio:'ignore'});
  let cdp;
  try {
    let targets;
    for(let i=0;i<100;i++) { try {targets=await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json(); if((await fetch(base+'/healthz')).ok) break;}catch{} await pause(100); }
    assert.ok(targets); cdp=new Cdp(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    await cdp.call('Page.enable'); await cdp.call('Runtime.enable'); await cdp.call('Network.enable');
    const external = []; const errors=[];
    cdp.listeners.push(m=>{if(m.method==='Network.requestWillBeSent' && /^https?:/.test(m.params.request.url) && !m.params.request.url.startsWith(base)) external.push(m.params.request.url); if(m.method==='Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.text);});
    await cdp.call('Page.navigate',{url:base+'/connections'});
    await cdp.until("document.readyState==='complete'");
    assert.deepEqual(await cdp.eval("[...document.querySelectorAll('.tabbar a')].map(a=>[a.textContent.trim(),new URL(a.href).pathname])"), [['Home','/'],['Activity','/activity'],['Accounts','/accounts'],['Connections','/connections']]);
    assert.equal(await cdp.eval("document.querySelector('.tabbar [aria-current=page]').textContent.trim()"),'Connections');
    if(process.argv.includes('--merchant')) await merchantChecks(cdp,base,artifacts);
    if(process.argv.includes('--design')) await designChecks(cdp,base,artifacts);
    if(process.argv.includes('--product')) {await productPages(cdp,base,artifacts); if(generated) await generatedProductChecks(cdp,base,artifacts); assert.deepEqual(errors,[]); assert.deepEqual(external,[]); console.log(JSON.stringify({kind:'rendered product QA; generated isolated server',artifacts,errors,external})); return;}
    assert.equal(await cdp.eval(`Boolean(document.querySelector('${handoff?'#sandbox-file-setup':'#sandbox-setup'}'))`),true,'private setup form missing');
    await cdp.until("document.querySelector('#sandbox-status').textContent.includes('Disabled')");
    assert.equal(await cdp.eval("getComputedStyle(document.querySelector('#sandbox-retry')).display"),'none','hidden retry must not render');
    if(!handoff){
      assert.equal(await cdp.eval("document.querySelector('#sandbox-secret').type"),'password');
      assert.equal(await cdp.eval("document.querySelector('#sandbox-client').type"),'password');
    } else assert.equal(await cdp.eval("Boolean(document.querySelector('#sandbox-secret'))"),false);
    assert.equal(await cdp.eval("document.querySelector('#sandbox-link').disabled"),true);
    assert.equal(await cdp.eval("Boolean(document.querySelector('script[src^=\\\"https://cdn.plaid.com/\\\"]'))"),false);
    assert.deepEqual(external,[]);
    await cdp.eval("document.querySelector('#member-name').value='Generated member Alpha'; document.querySelector('#sandbox-member').requestSubmit()");
    await cdp.until("document.querySelector('#link-owner').options.length===1");
    await cdp.eval("document.querySelector('#member-name').value='Generated member Beta'; document.querySelector('#sandbox-member').requestSubmit()");
    await cdp.until("document.querySelector('#link-owner').options.length===2");
    assert.equal(await cdp.eval("document.querySelector('#sandbox-members').children.length"),2);
    assert.equal(await cdp.eval("document.querySelector('#setup-details').open"),true,'disabled setup remains the visible prerequisite');
    for(const [width,height] of [[390,844],[1440,1000]]) {
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
      assert.equal(await cdp.eval("document.documentElement.scrollWidth<=innerWidth"),true,'page overflow');
      assert.ok(['rgb(0, 0, 0)','rgb(255, 255, 255)'].includes(await cdp.eval("getComputedStyle(document.body).backgroundColor")),'neutral theme canvas');
      const shot=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
      fs.writeFileSync(path.join(artifacts,`connections-disabled-${width}.png`),Buffer.from(shot.data,'base64'));
    }
    if(generated) {
      // Fulfil only the official script URL with an explicitly generated test double.
      const stub = "window.Plaid={create:function(o){return {open:async function(){try{await fetch('https://sandbox.plaid.com/link/client/get');window.generatedSandboxConnected=true;}catch(e){window.generatedSandboxConnected=false;return;}try{await fetch('https://production.plaid.com/link/client/get');window.generatedProductionBlocked=false;}catch(e){window.generatedProductionBlocked=true;}window.generatedLinkOrdinal=Number(o.token.split('-').at(-1));o.onSuccess('public-sandbox-callback-'+window.generatedLinkOrdinal,{institution:{name:'untrusted browser label'}})},destroy:function(){}}}}";
      let productionDispatched=0;
      cdp.listeners.push(m=>{if(m.method==='Fetch.requestPaused') {
        const script=m.params.request.url.includes('cdn.plaid.com');
        if(m.params.request.url.includes('production.plaid.com'))productionDispatched++;
        cdp.call('Fetch.fulfillRequest',{requestId:m.params.requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:script?'application/javascript':'application/json'},{name:'Access-Control-Allow-Origin',value:'*'}],body:Buffer.from(script?stub:'{}').toString('base64')});
      }});
      await cdp.call('Fetch.enable',{patterns:[{urlPattern:'https://cdn.plaid.com/link/v2/stable/link-initialize.js',requestStage:'Request'},{urlPattern:'https://sandbox.plaid.com/*',requestStage:'Request'},{urlPattern:'https://production.plaid.com/*',requestStage:'Request'}]});
      if(handoff) await cdp.eval("document.querySelector('#sandbox-file-consent').checked=true;document.querySelector('#sandbox-file-setup').requestSubmit()");
      else await cdp.eval("document.querySelector('#sandbox-client').value='generated-client';document.querySelector('#sandbox-secret').value='generated-secret';document.querySelector('#sandbox-enable-consent').checked=true;document.querySelector('#sandbox-setup').requestSubmit()");
      await cdp.until("document.querySelector('.sandbox-page').dataset.loaded==='true' && !document.querySelector('#sandbox-disable').disabled && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
      if(!handoff){assert.equal(await cdp.eval("document.querySelector('#sandbox-secret').value"),'');assert.equal(await cdp.eval("document.querySelector('#sandbox-client').value"),'');}
      assert.equal(external.length,0,'enable must not launch Link or load the CDN');
      for(let ordinal=1;ordinal<=2;ordinal++) {
        await cdp.eval(`document.querySelector('#link-owner').selectedIndex=${ordinal-1};document.querySelector('#link-owner').dispatchEvent(new Event('change'));document.querySelector('#link-consent').checked=true;document.querySelector('#link-consent').dispatchEvent(new Event('change'));document.querySelector('#sandbox-link-form').requestSubmit()`);
        await cdp.until(`document.querySelectorAll('[data-connection-id]').length===${ordinal}`);
        await cdp.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
        assert.equal(await cdp.eval('window.generatedSandboxConnected'),true);
        assert.equal(await cdp.eval('window.generatedProductionBlocked'),true);
        await cdp.call('Page.navigate',{url:base+'/accounts'});
        await cdp.until(`document.querySelectorAll('.sandbox-account').length===${ordinal} && document.querySelector('.sandbox-page').dataset.loaded==='true'`);
        await cdp.eval(`(()=>{document.querySelectorAll('[data-action=edit-account]')[${ordinal-1}].click();const f=document.querySelector('[data-account-form]');f.querySelector('input[type=checkbox]').checked=true;f.querySelector('select').value='shared';f.querySelector('select').dispatchEvent(new Event('change'));f.requestSubmit()})()`);
        await cdp.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && document.querySelector('[data-account-form]')===null");
        await cdp.call('Page.navigate',{url:base+'/connections'});
        await cdp.until(`document.querySelectorAll('[data-connection-id]').length===${ordinal}`);
        await cdp.eval(`document.querySelectorAll('[data-action=sync]')[${ordinal-1}].click()`);
        await cdp.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && !document.querySelector('[data-action=sync]').disabled");
      }
      assert.equal(productionDispatched,0,'Production must be refused before dispatch');
      await cdp.call('Page.navigate',{url:base+'/activity'});
      await cdp.until("document.querySelectorAll('[data-staged-row]').length===2");
      await cdp.eval("document.querySelector('#owner-filter').selectedIndex=1;document.querySelector('#sandbox-filters').requestSubmit()");
      await cdp.until("document.querySelectorAll('[data-staged-row]').length===1");
      await cdp.call('Page.navigate',{url:base+'/connections'});
      await cdp.until("document.querySelectorAll('[data-connection-id]').length===2");
      await cdp.eval("document.querySelector('[data-repair-consent]').checked=true;document.querySelector('[data-repair-consent]').dispatchEvent(new Event('change'));document.querySelector('[data-action=repair]').click()");
      await cdp.until("document.querySelector('#sandbox-message').textContent.includes('Repair checked') && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
      assert.equal(await cdp.eval("document.querySelectorAll('[data-connection-id]').length"),2);
      cdp.listeners.push(m=>{if(m.method==='Page.javascriptDialogOpening') cdp.call('Page.handleJavaScriptDialog',{accept:true});});
      await cdp.eval("setTimeout(()=>document.querySelector('[data-action=disconnect]').click(),0);true");
      await cdp.until("document.querySelector('#sandbox-connections').textContent.includes('Disconnected')");
      assert.ok(external.length>=1 && external.every(u=>u==='https://cdn.plaid.com/link/v2/stable/link-initialize.js'||u==='https://sandbox.plaid.com/link/client/get'),JSON.stringify(external));
      for(const width of [390,1440]) {
        await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width<500});
        assert.equal(await cdp.eval("document.documentElement.scrollWidth<=innerWidth"),true);
        const shot=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
        fs.writeFileSync(path.join(artifacts,`connections-generated-${width}.png`),Buffer.from(shot.data,'base64'));
      }
      await cdp.eval("document.querySelector('#sandbox-disable').click()");
      await cdp.until("document.querySelector('#sandbox-status').textContent.includes('Disabled')");
    }
    const storage=await cdp.eval("(async()=>({local:localStorage.length,session:sessionStorage.length,caches:(await caches.keys()).length,workers:(await navigator.serviceWorker.getRegistrations()).length}))()");
    assert.deepEqual(storage,{local:0,session:0,caches:0,workers:0});
    assert.deepEqual(errors,[]);
    const report={kind:generated?'rendered generated-protocol QA including CSP; no live Plaid':'rendered disabled-state QA; no live Plaid',viewports:[390,1440],member_count:2,external_requests:external,storage,errors};
    fs.writeFileSync(path.join(artifacts,'connections-browser.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {
    if(cdp) cdp.ws.close(); chrome.kill('SIGTERM'); server.kill('SIGTERM');
    await Promise.all([new Promise(r=>chrome.once('exit',r)),new Promise(r=>server.once('exit',r))]);
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
