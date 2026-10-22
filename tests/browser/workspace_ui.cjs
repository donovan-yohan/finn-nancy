/* Canonical generated acceptance using raw CDP; no browser npm dependency. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function freePort() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}
class Cdp {
  constructor(url) {
    this.id = 0; this.pending = new Map(); this.listeners = [];
    this.ws = new WebSocket(url);
    this.ready = new Promise((resolve,reject) => {this.ws.onopen=resolve;this.ws.onerror=reject;});
    this.ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id); this.pending.delete(message.id);
        if (!pending) return; // Ignore a late response after a bounded call expired.
        message.error ? pending.reject(Error(message.error.message)) : pending.resolve(message.result);
      } else for (const listener of this.listeners) listener(message);
    };
  }
  async call(method,params={}) {
    await this.ready; const id = ++this.id;
    return new Promise((resolve,reject) => {
      const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('CDP deadline: '+method));},15000);
      this.pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});
      this.ws.send(JSON.stringify({id,method,params}));
    });
  }
  async eval(expression) {
    let result;
    try { result = await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true}); }
    catch (error) { throw Error(error.message+' while evaluating '+expression.slice(0,160)); }
    if (result.exceptionDetails) throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async until(expression) {
    for (let i=0;i<120;i++) {if(await this.eval(expression))return;await pause(50);}
    throw Error('Timeout: '+expression+' '+await this.eval('document.body.innerText.slice(0,800)'));
  }
}
async function main() {
  const root = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(),'workspace-ui-'));
  const artifacts = path.join(root,'artifacts'); fs.mkdirSync(artifacts);
  console.log('ARTIFACTS '+artifacts);
  const port = await freePort(), debug = await freePort(), base = `http://127.0.0.1:${port}`;
  const code = "import sys; from pathlib import Path; import uvicorn; from tests.browser.workspace_ui_fixture import generated_workspace; uvicorn.run(generated_workspace(Path(sys.argv[1])),host='127.0.0.1',port=int(sys.argv[2]),lifespan='off',log_level='warning',access_log=False)";
  const server = spawn(path.resolve('.venv/bin/python'),['-c',code,path.join(root,'data'),String(port)],{
    env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root,STRICT_LOCAL_MODE:'true'},stdio:['ignore','ignore','pipe']});
  let serverErrors='';server.stderr.on('data',data=>serverErrors+=data);
  const chrome = spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-background-networking',`--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'
  ],{stdio:'ignore'});
  let cdp; const cases=[],issues=[],errors=[],external=[];
  try {
    let targets, manifest;
    for(let i=0;i<150;i++) {
      try {targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();manifest=await(await fetch(base+'/__fixture')).json();break;}catch{}
      if(server.exitCode!==null)throw Error(serverErrors);
      await pause(100);
    }
    assert.ok(targets&&manifest,serverErrors);
    cdp=new Cdp(targets.find(target=>target.type==='page').webSocketDebuggerUrl);
    await cdp.call('Page.enable'); await cdp.call('Runtime.enable'); await cdp.call('Network.enable');
    await cdp.call('Network.setCacheDisabled',{cacheDisabled:true});
    await cdp.call('Network.setBypassServiceWorker',{bypass:true});
    cdp.listeners.push(message=>{
      if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);
      if(message.method==='Fetch.requestPaused') {
        const request=message.params;
        if(/^https?:/.test(request.request.url)&&!request.request.url.startsWith(base+'/')) {
          external.push(request.request.url);
          cdp.call('Fetch.failRequest',{requestId:request.requestId,errorReason:'BlockedByClient'});
        }else cdp.call('Fetch.continueRequest',{requestId:request.requestId});
      }
    });
    await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    async function visit(route) {
      await cdp.call('Page.navigate',{url:base+route});
      await cdp.until(`location.pathname===${JSON.stringify(route.split('?')[0])}&&document.readyState==='complete'&&!!document.querySelector('h1')`);
    }
    async function theme(value) {
      await cdp.eval(`(()=>{const s=document.querySelector('[data-theme-select]');s.value=${JSON.stringify(value)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    }
    async function screenshot(name) {
      await cdp.eval('scrollTo(0,0)');
      await cdp.call('Page.bringToFront');
      const image=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(artifacts,name+'.png'),Buffer.from(image.data,'base64'));
    }
    // Every canonical destination and populated detail/form route, both appearances.
    for(const appearance of (process.argv.includes('--interactions-only')?[]:['dark','light'])) {
      for(const width of [320,390,768,1440]) {
        await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
        for(const route of manifest.routes) {
          if(process.env.WORKSPACE_VERBOSE)console.log(appearance,width,route);
          await visit(route); await theme(appearance);
          const result=await cdp.eval(`(()=>{
            const visible=n=>n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden';
            const r=n=>{const b=n.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height};};
            const controls=[...document.querySelectorAll('button,summary,input:not([type=hidden]):not([type=checkbox]),select,textarea,a.link,a.ghost,.tab')].filter(visible);
            const overflow=[...document.querySelectorAll('body *')].filter(visible).filter(n=>{const b=n.getBoundingClientRect();return b.right>innerWidth+1||b.left< -1;}).filter(n=>!n.closest('.table-wrap')&&!n.matches('.skip-link')).slice(0,8).map(n=>[n.tagName,n.className,r(n)]);
            return {title:document.querySelector('h1').textContent,overflow:document.documentElement.scrollWidth>innerWidth,
              offenders:overflow,background:getComputedStyle(document.body).backgroundColor,
              current:document.querySelectorAll('.tabbar [aria-current=page]').length,
              targets:controls.filter(n=>{const b=r(n);return b.height<43.9||b.width<43.9;}).map(n=>[n.tagName,n.name||n.textContent.slice(0,40),r(n)]),
              rows:document.querySelectorAll('.tx-row').length};})()`);
          const entry={appearance,width,route,...result};cases.push(entry);
          if(result.overflow||result.targets.length||result.current!==1)issues.push(entry);
          assert.equal(result.background,appearance==='dark'?'rgb(0, 0, 0)':'rgb(255, 255, 255)');
          assert.equal(await cdp.eval("document.querySelectorAll('main').length"),1,route+' main landmark');
          if(width===390||width===1440)await screenshot(route==='/'?`home-${appearance}-${width}`:`${route.split('?')[0].replaceAll('/','-').slice(1)}-${appearance}-${width}`);
          await cdp.eval("document.querySelectorAll('.app-main details').forEach(n=>n.open=true)");
          const expanded=await cdp.eval("({overflow:document.documentElement.scrollWidth>innerWidth,targets:[...document.querySelectorAll('.app-main button,.app-main input:not([type=hidden]):not([type=checkbox]),.app-main select,.app-main summary')].filter(n=>n.getClientRects().length).filter(n=>n.getBoundingClientRect().height<43.9).map(n=>n.name||n.textContent.slice(0,50))})");
          if(expanded.overflow||expanded.targets.length)issues.push({appearance,width,route,expanded});
        }
      }
    }
    fs.writeFileSync(path.join(artifacts,'workspace-browser.json'),JSON.stringify({kind:'generated Chromium canonical workspace',cases,issues,errors,external},null,2));
    assert.deepEqual(issues,[],'geometry/current navigation failures; see workspace-browser.json');
    console.log('Canonical route sweep passed: '+cases.length);
    // Retire the document/PDF renderer before the interaction acceptance wave.
    const oldTarget=targets.find(target=>target.type==='page').id;
    const next=await cdp.call('Target.createTarget',{url:'about:blank'});
    cdp.ws.close();
    targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();
    const listeners=cdp.listeners;
    cdp=new Cdp(targets.find(target=>target.id===next.targetId).webSocketDebuggerUrl);
    cdp.listeners=listeners;
    await cdp.call('Page.enable');await cdp.call('Runtime.enable');await cdp.call('Network.enable');
    await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    await cdp.call('Target.closeTarget',{targetId:oldTarget});
    // Single-DOM density, exact money, native keyboard disclosure, filters and cursor.
    const density=[];
    for(const appearance of ['dark','light'])for(const width of [320,390,768,1440]) {
      console.log(`Density ${appearance}/${width}`);
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
      await visit('/activity?q=Generated%20market');await theme(appearance);
      const metrics=await cdp.eval(`(()=>{const rows=[...document.querySelectorAll('.tx-summary')],nav=document.querySelector('.tabbar').getBoundingClientRect();return {
        first:rows[0].getBoundingClientRect().top,heights:rows.slice(0,8).map(n=>n.getBoundingClientRect().height),
        visible:rows.filter(n=>{const r=n.getBoundingClientRect();return r.top>=0&&r.bottom<Math.min(760,nav.top||844)}).length,
        count:rows.length,amounts:[...document.querySelectorAll('.tx-amount')].map(n=>n.textContent),
        clipped:[...document.querySelectorAll('.tx-amount')].some(n=>n.scrollWidth>n.clientWidth+1||getComputedStyle(n).textOverflow==='ellipsis')};})()`);
      assert.equal(metrics.count,50);assert.equal(metrics.clipped,false);
      assert.ok(metrics.heights.every(h=>h>=44&&h<=(width>900?50:74)),JSON.stringify(metrics));
      density.push({appearance,width,...metrics});
      await cdp.eval("document.querySelector('.tx-summary').focus()");
      await cdp.call('Input.dispatchKeyEvent',{type:'keyDown',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:49,text:' ',unmodifiedText:' '});
      await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:49});
      await cdp.until("document.querySelector('.tx-row').open");
      assert.equal(await cdp.eval("document.querySelector('.tx-row').innerText.includes('Accounting flow')"),true);
      assert.equal(await cdp.eval('document.documentElement.scrollWidth<=innerWidth'),true);
      await cdp.eval("document.querySelector('.tx-row').open=false;document.querySelector('.load-more').click()");
      await cdp.until("document.querySelectorAll('.tx-row').length===63");
      assert.equal(await cdp.eval("new Set([...document.querySelectorAll('.tx-row')].map(n=>n.id)).size"),63);
      await cdp.eval('scrollTo(0,document.body.scrollHeight)');
      assert.equal(await cdp.eval("(()=>{const r=[...document.querySelectorAll('.tx-row')].at(-1).getBoundingClientRect();const n=document.querySelector('.tabbar').getBoundingClientRect();return r.bottom<=(n.top>innerHeight/2?n.top:innerHeight)})()"),true,'last row clears fixed navigation');
      await visit('/activity?q=Generated%20market%20000');
      assert.equal(await cdp.eval("document.querySelector('.tx-amount').textContent.trim()"),'-$90,071,992,547,409.91 CAD');
      assert.equal(await cdp.eval("document.querySelector('.tx-amount').scrollWidth<=document.querySelector('.tx-amount').clientWidth+1"),true);
      await screenshot(`extreme-${appearance}-${width}`);
    }
    fs.writeFileSync(path.join(artifacts,'workspace-density.json'),JSON.stringify(density,null,2));
    await visit('/activity');
    for(const appearance of ['dark','light']) {
      await theme(appearance);
      await cdp.call('Page.bringToFront');
      await cdp.eval("document.querySelector('.tx-summary').focus()");
      await cdp.call('Input.dispatchKeyEvent',{type:'rawKeyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9,nativeVirtualKeyCode:48});
      await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9,nativeVirtualKeyCode:48});
      const contrast=await cdp.eval(`(()=>{
        const active=document.activeElement,s=getComputedStyle(active),root=getComputedStyle(document.documentElement);
        function rgb(value){const p=document.createElement('span');p.style.color=value;document.body.append(p);const v=getComputedStyle(p).color.match(/[\\d.]+/g).slice(0,3).map(Number);p.remove();return v;}
        function lum(c){return c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);}
        function ratio(a,b){const v=[lum(rgb(a)),lum(rgb(b))].sort((a,b)=>a-b);return(v[1]+.05)/(v[0]+.05);}
        const bg=root.getPropertyValue('--bg').trim(),panel=root.getPropertyValue('--panel-2').trim();
        return {focus:active.matches(':focus-visible'),outline:s.outlineStyle,width:parseFloat(s.outlineWidth),focusContrast:ratio(s.outlineColor,bg),text:Object.fromEntries(['--finn','--nancy','--muted'].map(key=>[key,Math.min(ratio(root.getPropertyValue(key).trim(),bg),ratio(root.getPropertyValue(key).trim(),panel))]))};})()`);
      assert.equal(contrast.focus,true);assert.notEqual(contrast.outline,'none');assert.ok(contrast.width>=2&&contrast.focusContrast>=3);
      assert.ok(Object.values(contrast.text).every(value=>value>=4.5));
      fs.writeFileSync(path.join(artifacts,`contrast-${appearance}.json`),JSON.stringify(contrast,null,2));
    }
    await cdp.eval("document.querySelector('.tx-search').value='weekly groceries';document.querySelector('.tx-search').dispatchEvent(new Event('change',{bubbles:true}))");
    await cdp.until("document.querySelectorAll('.tx-row').length===3");
    await visit('/activity?account_id=2&category_id=8&date_from=2026-01-01&date_to=2026-01-31');
    assert.equal(await cdp.eval("document.querySelectorAll('.tx-row').length"),1);
    assert.equal(await cdp.eval("document.querySelector('.tx-filter-details').open"),true);
    await visit('/activity?q=absent-generated-merchant');
    assert.equal(await cdp.eval("document.querySelector('.tx-empty').textContent.includes('no transactions match')"),true);
    await visit('/');
    await cdp.eval("document.querySelector('#home-month').value='2026-01';document.querySelector('.home-month').requestSubmit()");
    await cdp.until("location.search==='?month=2026-01'&&document.readyState==='complete'");
    assert.ok(await cdp.eval("document.querySelector('.home-recent a.link').href.includes('date_from=2026-01-01')"));
    await cdp.eval("document.querySelector('.more > summary').click()");
    assert.equal(await cdp.eval("[...document.querySelectorAll('.more-menu a')].every(n=>n.getBoundingClientRect().height>=44)"),true);
    const ax=await cdp.call('Accessibility.getFullAXTree');
    assert.ok(ax.nodes.some(n=>n.role?.value==='navigation'&&n.name?.value==='primary'));
    assert.ok(ax.nodes.some(n=>n.name?.value==='Appearance'));
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    console.log(JSON.stringify({passed:true,routeCases:cases.length,densityCases:density.length,artifacts,errors,external}));
  } finally {
    if(cdp)cdp.ws.close();
    chrome.kill('SIGTERM');server.kill('SIGTERM');
    await Promise.all([chrome,server].map(child=>child.exitCode!==null?Promise.resolve():new Promise(resolve=>{
      const deadline=setTimeout(()=>{child.kill('SIGKILL');resolve();},3000);
      child.once('exit',()=>{clearTimeout(deadline);resolve();});
    })));
    if(serverErrors)fs.writeFileSync(path.join(artifacts,'server.log'),serverErrors);
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
