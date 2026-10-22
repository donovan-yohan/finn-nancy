/* Focused generated acceptance: native evidence anchors and accepted contributors. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function freePort() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve)); return port;
}
class Cdp {
  constructor(url) {
    this.id=0;this.pending=new Map();this.listeners=[];this.ws=new WebSocket(url);
    this.ready=new Promise((resolve,reject)=>{this.ws.onopen=resolve;this.ws.onerror=reject;});
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
async function main() {
  const root=fs.mkdtempSync(path.join(process.env.TMPDIR,'workspace-review-'));
  const artifacts=path.join(root,'artifacts');fs.mkdirSync(artifacts);console.log('ARTIFACTS '+artifacts);
  const port=await freePort(),debug=await freePort(),base=`http://127.0.0.1:${port}`;
  const code="import sys; from pathlib import Path; import uvicorn; from tests.browser.workspace_review_fixture import generated_review; uvicorn.run(generated_review(Path(sys.argv[1])),host='127.0.0.1',port=int(sys.argv[2]),lifespan='off',log_level='warning',access_log=False)";
  const server=spawn(path.resolve('.venv/bin/python'),['-c',code,path.join(root,'data'),String(port)],{
    env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root,STRICT_LOCAL_MODE:'true'},stdio:['ignore','ignore','pipe']});
  let serverErrors='';server.stderr.on('data',data=>serverErrors+=data);
  const chrome=spawn(process.env.CHROMIUM_BIN||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking',
    `--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'],{stdio:'ignore'});
  let cdp,closing=false;const anchors=[],contributors=[],issues=[],errors=[],external=[];
  try {
    let targets,manifest;
    for(let i=0;i<150;i++){
      try{targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();manifest=await(await fetch(base+'/__review_fixture')).json();break;}catch{}
      if(server.exitCode!==null)throw Error(serverErrors);await pause(100);
    }
    assert.ok(targets&&manifest,serverErrors);
    cdp=new Cdp(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    await cdp.call('Page.enable');await cdp.call('Runtime.enable');await cdp.call('Network.enable');
    await cdp.call('Network.setBypassServiceWorker',{bypass:true});
    cdp.listeners.push(m=>{
      if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);
      if(m.method==='Fetch.requestPaused'){
        const r=m.params;
        if(/^https?:/.test(r.request.url)&&!r.request.url.startsWith(base+'/')){
          external.push(r.request.url);cdp.call('Fetch.failRequest',{requestId:r.requestId,errorReason:'BlockedByClient'}).catch(e=>{if(!closing)errors.push(e.message);});
        }else cdp.call('Fetch.continueRequest',{requestId:r.requestId}).catch(e=>{if(!closing)errors.push(e.message);});
      }
    });
    await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    async function visit(route) {
      await cdp.call('Page.navigate',{url:base+route});
      await cdp.until(`location.pathname===${JSON.stringify(route.split('?')[0])}&&document.readyState==='complete'&&!!document.querySelector('h1')`);
    }
    async function theme(value) {
      await cdp.eval(`(()=>{const n=document.querySelector('[data-theme-select]');n.value=${JSON.stringify(value)};n.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    }
    async function pointer(selector) {
      await cdp.until("!document.querySelector('.htmx-request,.htmx-settling,.htmx-swapping')");
      const p=await cdp.eval(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,hit:n.contains(document.elementFromPoint(x,y))};})()`);
      assert.ok(p.hit,'pointer target occluded: '+selector);delete p.hit;
      await cdp.call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...p});
      await cdp.call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...p});
    }
    for(const appearance of ['light','dark'])for(const width of [1440,320,390]){
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
      await visit('/statements?month=2026-06');await theme(appearance);
      await pointer('.period-total-link[href="#evidence-income"]');
      await cdp.until("location.hash==='#evidence-income'");
      await pause(100);
      async function geometry(kind) {
        const g=await cdp.eval(`(()=>{const t=document.getElementById(decodeURIComponent(location.hash.slice(1))),n=document.querySelector('.tabbar').getBoundingClientRect();return {
          hash:location.hash,targetTop:t.getBoundingClientRect().top,navTop:n.top,navBottom:n.bottom,
          margin:parseFloat(getComputedStyle(t).scrollMarginTop),
          parts:[...t.querySelectorAll('h2,.period-evidence-head > strong,.period-report-row-main > strong')].map(e=>{const r=e.getBoundingClientRect();return {text:e.textContent.trim(),top:r.top,bottom:r.bottom,unobscured:e.contains(document.elementFromPoint(r.left+Math.min(5,r.width/2),r.top+Math.min(5,r.height/2)))}})};})()`);
        const result={appearance,width,kind,...g};anchors.push(result);
        if(!g.parts.length||g.parts.some(p=>!p.unobscured||p.top<(width===1440?g.navBottom:0)||p.bottom>(width===1440?844:g.navTop)))issues.push(result);
        if(width<500&&g.margin!==16)issues.push({mobileMargin:g.margin});
      }
      await geometry('metric');
      await pointer('.period-reference-links a[href^="#report-row-"]');
      await cdp.until("location.hash.startsWith('#report-row-')");await pause(100);await geometry('row');
      await cdp.eval('scrollTo(0,document.body.scrollHeight)');
      const bottom=await cdp.eval("(()=>{const r=document.querySelector('.period-next-actions').getBoundingClientRect(),n=document.querySelector('.tabbar').getBoundingClientRect();return {bottom:r.bottom,limit:n.top>innerHeight/2?n.top:innerHeight};})()");
      if(bottom.bottom>bottom.limit+1)issues.push({appearance,width,kind:'final-bottom',...bottom});
      if(process.argv.includes('--anchors-only'))continue;
      await visit('/?month=2026-06');await theme(appearance);
      await cdp.eval("document.querySelector('.category-list').closest('details').open=true");
      assert.equal(await cdp.eval("document.querySelector('.category-list a[href*=\"category_id=900\"]').parentElement.querySelector('b').textContent.trim()"),'-$83.00');
      await pointer('.category-list a[href*="category_id=900"]');
      await cdp.until("location.pathname==='/activity'&&document.readyState==='complete'&&!!document.querySelector('.tx-filters')");
      assert.ok(await cdp.eval("location.search.includes('category_resolution=accepted')"));
      await cdp.eval("document.querySelector('.tx-search').value='Generated contributor';document.querySelector('.tx-search').dispatchEvent(new Event('change',{bubbles:true}))");
      await cdp.until("document.querySelector('.load-more')?.getAttribute('hx-get').includes('q=Generated+contributor')");
      const cursor=await cdp.eval("document.querySelector('.load-more').getAttribute('hx-get')");
      assert.ok(cursor.includes('category_resolution=accepted')&&cursor.includes('date_from=2026-06-01')&&cursor.includes('date_to=2026-06-30'));
      await pointer('.load-more');await cdp.until("document.querySelectorAll('.tx-row').length===55");
      const rows=await cdp.eval("[...document.querySelectorAll('.tx-row')].map(n=>({id:Number(n.id.slice(12)),amount:n.querySelector('.tx-amount').textContent.trim(),category:n.querySelector('.tx-cats').textContent.trim()}))");
      assert.equal(new Set(rows.map(r=>r.id)).size,55);
      assert.ok(!rows.some(r=>[manifest.ids.unknown,manifest.ids.unaccepted].includes(r.id)));
      const multi=rows.find(r=>r.id===manifest.ids['accepted-1']);
      assert.equal(multi.amount,'-$12.00 CAD');assert.ok(multi.category.includes('Category contribution -$8.00 CAD'));
      assert.ok(rows.find(r=>r.id===manifest.ids['accepted-0']).category.includes('Category contribution -$22.00 CAD'));
      const total=rows.reduce((sum,r)=>{const m=r.category.match(/Category contribution (-?)\$([\d,]+)\.(\d{2}) CAD/);assert.ok(m);return sum+(m[1]?-1n:1n)*(BigInt(m[2].replaceAll(',',''))*100n+BigInt(m[3]));},0n);
      assert.equal(total,BigInt(manifest.contribution_cents));
      assert.equal(await cdp.eval('document.documentElement.scrollWidth<=innerWidth'),true);
      // The normal form GET must carry exactly the same accepted/date/search state.
      await cdp.eval("document.querySelector('.tx-filters').removeAttribute('hx-get');document.querySelector('.tx-filters').submit()");
      await cdp.until("location.search.includes('q=Generated+contributor')&&document.readyState==='complete'");
      assert.ok(await cdp.eval("location.search.includes('category_resolution=accepted')&&location.search.includes('date_from=2026-06-01')"));
      contributors.push({appearance,width,rows:rows.length,contributionCents:String(total),cursor,multi});
    }
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    assert.deepEqual(issues,[],'native anchor geometry; see regression.json');
    console.log(JSON.stringify({passed:true,anchorCases:anchors.length,contributorCases:contributors.length,artifacts}));
  } finally {
    closing=true;
    if(cdp){await cdp.call('Fetch.disable').catch(()=>{});for(const p of cdp.pending.values())p.reject(Error('Harness closing'));cdp.pending.clear();}
    fs.writeFileSync(path.join(artifacts,'regression.json'),JSON.stringify({anchors,contributors,issues,errors,external},null,2));
    if(cdp)cdp.ws.close();chrome.kill('SIGTERM');server.kill('SIGTERM');
    await Promise.all([chrome,server].map(child=>child.exitCode!==null?Promise.resolve():new Promise(resolve=>{
      const timer=setTimeout(()=>{child.kill('SIGKILL');resolve();},3000);child.once('exit',()=>{clearTimeout(timer);resolve();});
    })));
    if(serverErrors)fs.writeFileSync(path.join(artifacts,'server.log'),serverErrors);
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
