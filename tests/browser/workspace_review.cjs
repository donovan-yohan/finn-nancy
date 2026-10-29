/* Generated acceptance: native evidence anchors and exact accepted contributors. */
const assert = require('node:assert/strict');
const {spawn,execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function freePort() {
  const server = net.createServer();
  await new Promise((resolve,reject) => {server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve)); return port;
}
class Cdp {
  constructor(url) {
    this.id=0;this.pending=new Map();this.listeners=[];this.ws=new WebSocket(url);
    this.ready=new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('CDP connection deadline')),15000);
      this.ws.onopen=()=>{clearTimeout(timer);resolve();};
      this.ws.onerror=error=>{clearTimeout(timer);reject(error);};
    });
    this.ws.onclose=()=>{for(const p of this.pending.values())p.reject(Error('CDP connection closed'));this.pending.clear();};
    this.ws.onmessage=event=>{
      const m=JSON.parse(event.data);
      if(m.id){const p=this.pending.get(m.id);this.pending.delete(m.id);if(!p)return;
        m.error?p.reject(Error(m.error.message)):p.resolve(m.result);
      }else for(const listener of this.listeners)listener(m);
    };
  }
  async call(method,params={}) {
    await this.ready;const id=++this.id;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('CDP deadline: '+method));},15000);
      this.pending.set(id,{resolve:v=>{clearTimeout(timer);resolve(v);},reject:e=>{clearTimeout(timer);reject(e);}});
      this.ws.send(JSON.stringify({id,method,params}));
    });
  }
  async eval(expression) {
    const r=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;
  }
  async until(expression) {
    for(let i=0;i<120;i++){if(await this.eval(expression))return;await pause(50);}
    throw Error('Timeout: '+expression+' '+await this.eval("JSON.stringify({url:location.href,rows:document.querySelectorAll('.tx-row').length,more:document.querySelector('.load-more')?.outerHTML,body:document.body.innerText.slice(-1200)})"));
  }
}

// FragmentObservation = {href,hash,exists,scrollX,scrollY,heading,amount,ready}.
// Like design_close.test.cjs, inspect painted text, not a potentially empty box.
// This is read-only: only the real anchor click may navigate or scroll the page.
function fragmentState(href) {
  const target=document.getElementById(decodeURIComponent(href.slice(1)));
  const textState=element=>{
    if(!element)return {visible:false,rects:[]};
    const range=document.createRange();range.selectNodeContents(element);
    const rects=[...range.getClientRects()].filter(r=>r.width>0&&r.height>0);
    return {
      text:element.textContent.trim().replace(/\s+/g,' '),
      rects:rects.map(r=>({left:r.left,top:r.top,right:r.right,bottom:r.bottom})),
      visible:element.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&rects.length>0&&
        rects.every(r=>r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight&&
          [0.25,0.5,0.75].every(part=>element.contains(document.elementFromPoint(r.left+r.width*part,r.top+r.height/2)))),
    };
  };
  const heading=textState(target?.querySelector('h2'));
  const amount=textState(target?.querySelector('.period-evidence-head > strong,.period-report-row-main > strong'));
  return {href,hash:location.hash,exists:!!target,top:target?.getBoundingClientRect().top,
    scrollX,scrollY,heading,amount,
    ready:location.hash===href&&!!target&&heading.visible&&amount.visible};
}
async function visibleFragment(cdp,href) {
  let previous,stable=0,last;
  for(let i=0;i<120;i++) {
    last=await cdp.eval(`(${fragmentState})(${JSON.stringify(href)})`);
    const geometry=JSON.stringify([last.scrollX,last.scrollY,last.heading.rects,last.amount.rects]);
    stable=last.ready&&geometry===previous?stable+1:0;previous=geometry;
    if(last.ready&&stable>=3)return last;
    await pause(50);
  }
  throw Error('Native fragment did not settle visibly: '+JSON.stringify(last));
}
function moneyCents(text) {
  const match=/^(-?)\$([\d,]+)\.(\d{2})(?: CAD)?$/.exec(text);
  assert.ok(match,'Exact CAD amount required: '+text);
  return (match[1]?-1n:1n)*(BigInt(match[2].replaceAll(',',''))*100n+BigInt(match[3]));
}

async function main() {
  const root=fs.mkdtempSync(path.join(process.env.TMPDIR,'workspace-review-'));
  const artifacts=path.join(root,'artifacts');fs.mkdirSync(artifacts);console.log('ARTIFACTS '+artifacts);
  const port=await freePort(),debug=await freePort(),base=`http://127.0.0.1:${port}`;
  const code="import sys; from pathlib import Path; import uvicorn; from tests.browser.workspace_review_fixture import generated_review; uvicorn.run(generated_review(Path(sys.argv[1])),host='127.0.0.1',port=int(sys.argv[2]),lifespan='off',log_level='warning',access_log=False)";
  const server=spawn(path.resolve('.venv/bin/python'),['-c',code,path.join(root,'data'),String(port)],{
    env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root,STRICT_LOCAL_MODE:'true',PLAID_ENABLED:'false'},detached:true,stdio:['ignore','ignore','pipe']});
  let serverErrors='',chromeErrors='',spawnError;
  server.stderr.on('data',data=>serverErrors+=data);
  const chrome=spawn(process.env.CHROMIUM_BIN||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking',
    '--remote-debugging-address=127.0.0.1',`--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'],
    {detached:true,stdio:['ignore','ignore','pipe']});
  chrome.stderr.on('data',data=>chromeErrors+=data);
  for(const child of [server,chrome])child.on('error',error=>{spawnError=error;});
  let cdp,manifest,closing=false,cleanupPromise,timedOut=false,failure;
  const anchors=[],contributors=[],refusals=[],bottoms=[],errors=[],external=[],requests=[],inflight=new Set();
  const groupAlive=child=>{
    if(!child.pid)return false;
    try{process.kill(-child.pid,0);return true;}catch(error){
      if(error.code==='ESRCH')return false;
      if(error.code!=='EPERM')throw error;
      return execFileSync('/bin/ps',['-axo','pgid='],{encoding:'utf8'}).split(/\s+/).some(value=>Number(value)===child.pid);
    }
  };
  const signalGroup=(child,signal)=>{
    if(!child.pid)return;
    try{process.kill(-child.pid,signal);}catch(error){if(error.code!=='ESRCH'&&!(error.code==='EPERM'&&!groupAlive(child)))throw error;}
  };
  function save() {
    fs.writeFileSync(path.join(artifacts,'regression.json'),JSON.stringify({manifest,anchors,contributors,refusals,bottoms,errors,external,requests,timedOut,failure},null,2));
  }
  function cleanup() {
    if(cleanupPromise)return cleanupPromise;
    cleanupPromise=(async()=>{
      closing=true;if(cdp)cdp.ws.close();
      for(const child of [chrome,server])signalGroup(child,'SIGTERM');
      const deadline=Date.now()+3000;
      while([chrome,server].some(groupAlive)&&Date.now()<deadline)await pause(50);
      for(const child of [chrome,server])if(groupAlive(child))signalGroup(child,'SIGKILL');
      for(let i=0;i<40&&[chrome,server].some(groupAlive);i++)await pause(50);
      for(const child of [chrome,server])child.stderr.destroy();
      const serverGroupGone=!groupAlive(server),chromeGroupGone=!groupAlive(chrome);
      if(chromeGroupGone)fs.rmSync(path.join(root,'chrome'),{recursive:true,force:true});
      const probe=async p=>{try{await fetch(`http://127.0.0.1:${p}/`,{signal:AbortSignal.timeout(500)});return false;}catch{return true;}};
      const result={serverPid:server.pid,chromePid:chrome.pid,serverGroupGone,chromeGroupGone,
        serverListenerGone:await probe(port),chromeListenerGone:await probe(debug),profileExists:fs.existsSync(path.join(root,'chrome')),timedOut};
      fs.writeFileSync(path.join(artifacts,'cleanup.json'),JSON.stringify(result,null,2));
      if(serverErrors)fs.writeFileSync(path.join(artifacts,'server.log'),serverErrors);
      if(chromeErrors)fs.writeFileSync(path.join(artifacts,'chrome.log'),chromeErrors);
      assert.ok(result.serverGroupGone&&result.chromeGroupGone&&result.serverListenerGone&&result.chromeListenerGone&&!result.profileExists,'Owned runtime survived cleanup');
    })();return cleanupPromise;
  }
  // Leave cleanup time inside the unchanged Python wrapper's 120-second limit.
  const watchdog=setTimeout(()=>{timedOut=true;failure='105-second browser deadline';save();cleanup().catch(console.error).finally(()=>process.exit(1));},105000);
  const interrupted=()=>{failure='Interrupted';save();cleanup().catch(console.error).finally(()=>process.exit(1));};
  process.once('SIGTERM',interrupted);process.once('SIGINT',interrupted);
  try {
    let targets;
    for(let i=0;i<150;i++){
      try{targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`,{signal:AbortSignal.timeout(1000)})).json();manifest=await(await fetch(base+'/__review_fixture',{signal:AbortSignal.timeout(1000)})).json();break;}catch{}
      if(spawnError)throw spawnError;
      if(server.exitCode!==null)throw Error(serverErrors);if(chrome.exitCode!==null)throw Error(chromeErrors);await pause(100);
    }
    assert.ok(targets&&manifest,serverErrors);
    assert.equal(manifest.category,900);assert.equal(manifest.contribution_cents,-8300);
    const expectedIds=Object.entries(manifest.ids).filter(([key])=>/^accepted-\d+$/.test(key)).map(([,id])=>id).sort((a,b)=>a-b);
    assert.equal(expectedIds.length,55);
    cdp=new Cdp(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    cdp.listeners.push(m=>{
      if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails);
      if(m.method==='Runtime.consoleAPICalled'&&m.params.type==='error')errors.push(m.params.args.map(n=>n.value||n.description).join(' '));
      if(m.method==='Fetch.requestPaused'){
        const r=m.params,foreign=/^https?:/.test(r.request.url)&&new URL(r.request.url).origin!==base;
        if(foreign)external.push(r.request.url);
        else if(/^https?:/.test(r.request.url))requests.push({url:r.request.url.slice(base.length),method:r.request.method,
          htmx:Object.entries(r.request.headers).some(([k,v])=>k.toLowerCase()==='hx-request'&&v==='true')});
        const operation=cdp.call(foreign?'Fetch.failRequest':'Fetch.continueRequest',foreign?{requestId:r.requestId,errorReason:'BlockedByClient'}:{requestId:r.requestId});
        inflight.add(operation);operation.catch(e=>{if(!closing)errors.push(e.message);}).finally(()=>inflight.delete(operation));
      }
    });
    await cdp.call('Page.enable');await cdp.call('Runtime.enable');await cdp.call('Network.enable');
    await cdp.call('Network.setBypassServiceWorker',{bypass:true});await cdp.call('Network.setCacheDisabled',{cacheDisabled:true});
    await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    async function visit(route) {
      const url=new URL(base+route),navigation=await cdp.call('Page.navigate',{url:url.href});assert.ok(!navigation.errorText,navigation.errorText);
      await cdp.until(`location.pathname===${JSON.stringify(url.pathname)}&&location.search===${JSON.stringify(url.search)}&&document.readyState==='complete'&&!!document.querySelector('h1')&&typeof htmx==='object'`);
      assert.equal(await cdp.eval("!!document.querySelector('.fn-header .fn-nav')&&!!document.querySelector('main.fn-shell')&&!document.querySelector('link[href=\"/static/app.css\"]')&&getComputedStyle(document.querySelector('.fn-header')).position==='static'"),true,'Shared in-flow shell required');
    }
    async function theme(value) {
      await cdp.eval(`(()=>{const n=document.querySelector('[data-theme-select]');n.value=${JSON.stringify(value)};n.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await cdp.until(`document.documentElement.dataset.theme===${JSON.stringify(value)}&&document.documentElement.dataset.bsTheme===${JSON.stringify(value)}`);
    }
    async function settled(){await cdp.until("!document.querySelector('.htmx-request,.htmx-settling,.htmx-swapping')&&!document.querySelector('#tx[aria-busy=true]')");}
    async function pointer(selector) {
      await settled();
      const p=await cdp.eval(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n?.checkVisibility())throw Error('Invisible pointer target');n.scrollIntoView({block:'center',behavior:'instant'});const r=n.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,hit:n.contains(document.elementFromPoint(x,y))};})()`);
      assert.ok(p.hit,'pointer target occluded: '+selector);delete p.hit;
      for(const type of ['mousePressed','mouseReleased'])await cdp.call('Input.dispatchMouseEvent',{type,button:'left',clickCount:1,...p});
    }
    async function screenshot(name) {
      const image=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(artifacts,name+'.png'),Buffer.from(image.data,'base64'));
    }
    function scopedUrl(route,search='') {
      const u=new URL(route,base);assert.equal(u.searchParams.get('category_id'),'900');
      assert.equal(u.searchParams.get('category_resolution'),'accepted');assert.equal(u.searchParams.get('date_from'),'2026-06-01');assert.equal(u.searchParams.get('date_to'),'2026-06-30');
      assert.equal(u.searchParams.get('q')||'',search);return u;
    }
    async function rowState() {
      return cdp.eval("[...document.querySelectorAll('.tx-row')].map(n=>({id:Number(n.id.slice(12)),description:n.querySelector('.tx-desc').textContent.trim(),amount:n.querySelector('.tx-amount').textContent.trim(),category:n.querySelector('.tx-cats').textContent.trim()}))");
    }
    function verifyRows(rows,complete) {
      assert.equal(rows.length,complete?55:50);assert.equal(new Set(rows.map(r=>r.id)).size,rows.length);
      assert.ok(!rows.some(r=>[manifest.ids.unknown,manifest.ids.unaccepted].includes(r.id)));
      let total=0n;
      for(const row of rows) {
        assert.ok(expectedIds.includes(row.id),'Unexpected contributor '+row.id);
        const key=Object.keys(manifest.ids).find(key=>manifest.ids[key]===row.id),index=Number(key.slice(9));
        assert.equal(row.description,'Generated contributor '+String(index).padStart(3,'0'));
        const contributions=[...row.category.matchAll(/Category contribution (-?\$[\d,]+\.\d{2} CAD)/g)];assert.equal(contributions.length,1);
        const contribution=moneyCents(contributions[0][1]);assert.equal(contribution,index===0?-2200n:index===1?-800n:-100n);
        assert.equal(moneyCents(row.amount),index===0?-2200n:index===1?-1200n:-100n);
        assert.ok(row.category.includes('Generated accepted category'));
        assert.equal(row.category.includes('Generated sibling category'),index===1);total+=contribution;
      }
      if(complete){assert.deepEqual(rows.map(r=>r.id).sort((a,b)=>a-b),expectedIds);assert.equal(total,-8300n);}
      return String(total);
    }
    async function finalActivityRow() {
      await cdp.eval('scrollTo({top:document.documentElement.scrollHeight,behavior:"instant"})');
      const result=await cdp.eval("(()=>{const row=[...document.querySelectorAll('.tx-row')].at(-1),n=row.querySelector('.tx-summary'),r=n.getBoundingClientRect();return {id:row.id,top:r.top,bottom:r.bottom,hit:[.1,.5,.9].every(p=>n.contains(document.elementFromPoint(r.left+r.width*p,r.top+r.height/2))),overflow:document.documentElement.scrollWidth>innerWidth+1};})()");
      assert.ok(result.top>=0&&result.bottom<=844&&result.hit&&!result.overflow,JSON.stringify(result));return result;
    }
    for(const appearance of ['light','dark'])for(const width of [1440,320,390]){
      const label=`${appearance}-${width}`;
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
      await visit('/statements?month=2026-06');await theme(appearance);
      const metric=await cdp.eval("(()=>{const n=document.querySelector('.period-total-link[href=\"#evidence-gross-money-out\"]');return {href:n.getAttribute('href'),heading:n.querySelector('.close-report-label').textContent.trim(),amount:n.querySelector('strong').textContent.trim()+' CAD',line:n.dataset.lineKey};})()");
      await pointer('.period-total-link[href="#evidence-gross-money-out"]');
      const evidence=await visibleFragment(cdp,metric.href);
      assert.equal(evidence.heading.text,metric.heading);assert.equal(evidence.amount.text,metric.amount);
      assert.equal(await cdp.eval("document.getElementById('evidence-gross-money-out').dataset.lineKey"),metric.line);
      anchors.push({appearance,width,kind:'metric',...evidence});await screenshot(`evidence-${label}`);save();
      // This generated transaction has two accepted splits. Follow its category-900
      // report link, not the first unrelated reference or its full $12 transaction.
      const row=await cdp.eval(`(()=>{const links=[...document.querySelectorAll('#evidence-gross-money-out .period-reference-links a')],link=links.find(a=>{const n=document.getElementById(a.hash.slice(1));return a.hash.startsWith('#report-row-txn:${manifest.ids['accepted-1']}:split:')&&n.querySelector('a[href="/categories#category-900"]');});return {href:link.getAttribute('href'),label:link.textContent.trim()};})()`);
      assert.equal(row.label,'2026-06-14 · Generated contributor 001');
      await pointer('#evidence-gross-money-out .period-reference-links a[href='+JSON.stringify(row.href)+']');
      const target=await visibleFragment(cdp,row.href);
      assert.equal(target.heading.text,'Generated contributor 001');assert.equal(target.amount.text,'-$8.00 CAD');
      anchors.push({appearance,width,kind:'row',...target});await screenshot(`row-${label}`);
      const wrongTarget=await cdp.eval(`(${fragmentState})(${JSON.stringify(metric.href)})`);
      assert.equal(wrongTarget.exists,true);assert.equal(wrongTarget.ready,false);
      const refused=await cdp.eval(`(()=>{const href=${JSON.stringify(row.href)},state=${fragmentState},amount=document.getElementById(href.slice(1)).querySelector('.period-report-row-main > strong'),old=amount.style.transform,cover=document.createElement('div');cover.style.cssText='position:fixed;inset:0;z-index:2147483647;background:white';try{document.body.append(cover);const covered=state(href).ready;cover.remove();amount.style.transform='translateY(-20000px)';return {covered,offscreen:state(href).ready};}finally{cover.remove();amount.style.transform=old;}})()`);
      assert.deepEqual(refused,{covered:false,offscreen:false});assert.equal((await visibleFragment(cdp,row.href)).ready,true);
      refusals.push({appearance,width,wrongTarget:wrongTarget.ready,...refused});
      // The final contribution row and final workflow actions must remain reachable.
      await cdp.eval("[...document.querySelectorAll('.period-report-row')].at(-1).scrollIntoView({block:'center',behavior:'instant'})");
      const finalReport=await cdp.eval("(()=>{const n=[...document.querySelectorAll('.period-report-row')].at(-1),r=n.getBoundingClientRect();return {id:n.id,top:r.top,bottom:r.bottom,hit:n.contains(document.elementFromPoint(r.left+r.width/2,r.bottom-2))};})()");
      assert.ok(finalReport.top>=0&&finalReport.bottom<=844&&finalReport.hit,JSON.stringify(finalReport));
      await cdp.eval('scrollTo({top:document.documentElement.scrollHeight,behavior:"instant"})');
      const bottom=await cdp.eval("(()=>{const n=document.querySelector('.period-next-actions'),r=n.getBoundingClientRect();return {top:r.top,bottom:r.bottom,controls:[...n.querySelectorAll('a')].map(a=>{const b=a.getBoundingClientRect();return {text:a.textContent.trim(),visible:b.top>=0&&b.bottom<=innerHeight&&a.contains(document.elementFromPoint(b.left+b.width/2,b.top+b.height/2))};})};})()");
      assert.ok(bottom.top>=0&&bottom.bottom<=844&&bottom.controls.length===3&&bottom.controls.every(n=>n.visible),JSON.stringify(bottom));
      bottoms.push({appearance,width,finalReport,bottom});
      if(process.argv.includes('--anchors-only'))continue;
      await visit('/?month=2026-06');await theme(appearance);
      await pointer('.home-categories > summary');assert.equal(await cdp.eval("document.querySelector('.home-categories').open"),true);
      const category=await cdp.eval("(()=>{const n=document.querySelector('.home-categories .category-row a[href*=\"category_id=900\"]');return {href:n.getAttribute('href'),heading:n.textContent.trim(),amount:n.closest('.category-row').querySelector('b').textContent.trim()};})()");
      assert.equal(category.heading,'Generated accepted category');assert.equal(category.amount,'-$83.00');scopedUrl(category.href);
      await pointer('.home-categories .category-row a[href*="category_id=900"]');
      await cdp.until("location.pathname==='/activity'&&document.readyState==='complete'&&!!document.querySelector('.tx-filters')&&typeof htmx==='object'");
      scopedUrl(await cdp.eval('location.href'));verifyRows(await rowState(),false);
      assert.equal(await cdp.eval("document.querySelector('.tx-filters [name=category_resolution]').value"),'accepted');
      const searchRequestStart=requests.length;
      await cdp.eval("document.querySelector('.tx-search').value='Generated contributor';document.querySelector('.tx-search').dispatchEvent(new Event('input',{bubbles:true}))");
      await cdp.until("document.querySelector('.load-more')?.getAttribute('hx-get').includes('q=Generated+contributor')");await settled();
      assert.ok(requests.slice(searchRequestStart).some(r=>r.url.startsWith('/transactions?')&&r.htmx&&scopedUrl(r.url,'Generated contributor')),'Real HTMX search required');
      verifyRows(await rowState(),false);
      const cursor=await cdp.eval("document.querySelector('.load-more').getAttribute('hx-get')");assert.ok(scopedUrl(cursor,'Generated contributor').searchParams.get('cursor'));
      await cdp.eval("window.reviewFirstRow=document.querySelector('.tx-row')");
      await pointer('.load-more');await cdp.until("document.querySelectorAll('.tx-row').length===55");await settled();
      assert.equal(await cdp.eval("document.querySelector('.tx-row')===window.reviewFirstRow&&window.reviewFirstRow.isConnected&&!document.querySelector('.load-more')&&document.querySelectorAll('#tx-list').length===1"),true);
      const rows=await rowState(),total=verifyRows(rows,true),finalRow=await finalActivityRow();await screenshot(`contributors-${label}`);
      // Exercise the real GET fallback, including its own fresh 50+5 cursor chain.
      const getRequestStart=requests.length;
      await cdp.eval("document.querySelector('.tx-filters').removeAttribute('hx-get');document.querySelector('.tx-filters').submit()");
      await cdp.until("location.search.includes('q=Generated+contributor')&&document.readyState==='complete'&&document.querySelectorAll('.tx-row').length===50");
      scopedUrl(await cdp.eval('location.href'),'Generated contributor');verifyRows(await rowState(),false);
      assert.ok(requests.slice(getRequestStart).some(r=>r.url.startsWith('/activity?')&&!r.htmx&&scopedUrl(r.url,'Generated contributor')),'Native form GET required');
      const getCursor=await cdp.eval("document.querySelector('.load-more').getAttribute('hx-get')");assert.ok(scopedUrl(getCursor,'Generated contributor').searchParams.get('cursor'));
      await pointer('.load-more');await cdp.until("document.querySelectorAll('.tx-row').length===55");await settled();
      const getRows=await rowState();assert.equal(verifyRows(getRows,true),total);assert.deepEqual(getRows,rows);await finalActivityRow();
      contributors.push({appearance,width,rows:rows.length,contributionCents:total,category,cursor,getCursor,multi:rows.find(r=>r.id===manifest.ids['accepted-1']),allRows:rows,normalGetRows:getRows.length,finalRow});save();
    }
    await cdp.call('Fetch.disable');while(inflight.size)await Promise.allSettled([...inflight]);
    assert.equal(anchors.length,12);assert.equal(refusals.length,6);assert.equal(bottoms.length,6);
    assert.equal(contributors.length,process.argv.includes('--anchors-only')?0:6);
    assert.ok(requests.every(r=>r.method==='GET'),'Generated read-only journey must not mutate');
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    console.log(JSON.stringify({passed:true,anchorCases:anchors.length,contributorCases:contributors.length,artifacts}));
  } catch(error) {
    failure=error.stack;
    if(cdp)try{const image=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(artifacts,'failure.png'),Buffer.from(image.data,'base64'));}catch{}
    throw error;
  } finally {
    clearTimeout(watchdog);process.removeListener('SIGTERM',interrupted);process.removeListener('SIGINT',interrupted);
    save();await cleanup();
  }
}
if(require.main===module)main().catch(e=>{console.error(e);process.exitCode=1;});
module.exports={fragmentState,visibleFragment,moneyCents};
