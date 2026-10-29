/* Canonical generated acceptance using raw CDP; no browser npm dependency. */
const assert = require('node:assert/strict');
const {spawn,execFileSync} = require('node:child_process');
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
    this.ready = new Promise((resolve,reject) => {
      const timer=setTimeout(()=>reject(Error('CDP connection deadline')),15000);
      this.ws.onopen=()=>{clearTimeout(timer);resolve();};
      this.ws.onerror=error=>{clearTimeout(timer);reject(error);};
    });
    this.ws.onclose = () => {
      for(const pending of this.pending.values())pending.reject(Error('CDP connection closed'));
      this.pending.clear();
    };
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

// Returns a serializable case observation, never a substitute document/fixture.
function inspectPage() {
  const visible=n=>n.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const rect=n=>{const b=n.getBoundingClientRect();return {x:b.x,y:b.y,width:b.width,height:b.height};};
  const named=n=>Boolean(n.getAttribute('aria-label')?.trim() ||
    n.getAttribute('aria-labelledby')?.split(/\s+/).some(id=>document.getElementById(id)?.textContent.trim()));
  const localSelector='.fn-table-wrap,.table-responsive,.table-wrap,.fn-capture-table,.manage-table-wrap,.evidence-table-wrap,.lens-pane';
  const containedScroller=n=>{
    for(let p=n.parentElement;p&&p!==document.body;p=p.parentElement) {
      const b=p.getBoundingClientRect();
      if(p.matches(localSelector)&&/^(auto|scroll)$/.test(getComputedStyle(p).overflowX)&&
        b.left>=-1&&b.right<=innerWidth+1&&named(p)&&p.tabIndex>=0)return true;
    }
    return false;
  };
  const controls=[...document.querySelectorAll('button,summary,input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea,a.btn,a.link,a.ghost,.fn-nav a')].filter(visible);
  const offenders=[...document.querySelectorAll('body *')].filter(visible).filter(n=>{
    const b=n.getBoundingClientRect();return b.right>innerWidth+1||b.left< -1;
  }).filter(n=>!containedScroller(n)&&!n.matches('.skip-link')).slice(0,8).map(n=>[n.tagName,n.className,rect(n)]);
  const primary=[...document.querySelectorAll('.fn-nav > a[aria-current=page],.fn-nav > .fn-more > summary.is-active')];
  return {
    title:document.querySelector('h1').textContent,
    overflow:document.documentElement.scrollWidth>innerWidth+1,offenders,
    background:getComputedStyle(document.body).backgroundColor,
    current:primary.filter(visible).length,
    targets:controls.filter(n=>{const b=rect(n);return b.height<43.9||b.width<43.9;}).map(n=>[n.tagName,n.name||n.textContent.slice(0,40),rect(n)]),
    smallFoundation:controls.filter(n=>n.matches('.btn,.form-control,.form-select')&&rect(n).height<47.9).map(n=>[n.tagName,n.name||n.textContent.slice(0,40),rect(n)]),
    unnamedScrollers:[...document.querySelectorAll(localSelector)].filter(visible).filter(n=>n.scrollWidth>n.clientWidth+1&&(!named(n)||n.tabIndex<0)).map(n=>[n.className,rect(n)]),
    rows:document.querySelectorAll('.tx-row').length,
    main:document.querySelectorAll('main').length,
    headings:document.querySelectorAll('h1').length,
    shell:!!document.querySelector('.fn-header .fn-nav')&&!!document.querySelector('main.fn-shell'),
    legacy:[...document.styleSheets].some(s=>s.href&&new URL(s.href).pathname==='/static/app.css'),
    appearance:document.querySelector('[data-theme-select]')?.value,
    theme:document.documentElement.dataset.theme,bootstrapTheme:document.documentElement.dataset.bsTheme,
  };
}

async function main() {
  const interactionsOnly=process.argv.includes('--interactions-only');
  const sweepOnly=process.argv.includes('--sweep-only');
  assert.ok(!(interactionsOnly&&sweepOnly),'Choose interactions-only or sweep-only, not both');
  // Optional exact manifest routes, not substrings. Default always sweeps every route.
  const requested=process.env.WORKSPACE_ROUTES?JSON.parse(process.env.WORKSPACE_ROUTES):null;
  if(requested)assert.ok(Array.isArray(requested)&&requested.length&&requested.every(v=>typeof v==='string'),'WORKSPACE_ROUTES must be a nonempty JSON string array');
  const timeout=Number(process.env.WORKSPACE_TIMEOUT_MS||600000);
  assert.ok(Number.isSafeInteger(timeout)&&timeout>0,'WORKSPACE_TIMEOUT_MS must be a positive integer');
  const root = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(),'workspace-ui-'));
  const artifacts = path.join(root,'artifacts'); fs.mkdirSync(artifacts);
  console.log('ARTIFACTS '+artifacts);
  const port = await freePort(), debug = await freePort(), base = `http://127.0.0.1:${port}`;
  const code = "import sys; from pathlib import Path; import uvicorn; from tests.browser.workspace_ui_fixture import generated_workspace; uvicorn.run(generated_workspace(Path(sys.argv[1])),host='127.0.0.1',port=int(sys.argv[2]),lifespan='off',log_level='warning',access_log=False)";
  const server = spawn(path.resolve('.venv/bin/python'),['-c',code,path.join(root,'data'),String(port)],{
    env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root,STRICT_LOCAL_MODE:'true',PLAID_ENABLED:'false'},detached:true,stdio:['ignore','ignore','pipe']});
  let serverErrors='';server.stderr.on('data',data=>serverErrors+=data);
  const chrome = spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-background-networking',`--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'
  ],{detached:true,stdio:'ignore'});
  let cdp,manifest,spawnError,cleanupPromise,timedOut=false;
  const cases=[],issues=[],errors=[],external=[],density=[],interactions=[],inflight=new Set();
  for(const child of [server,chrome])child.on('error',error=>{spawnError=error;});
  const groupAlive=child=>{
    if(!child.pid)return false;
    try{process.kill(-child.pid,0);return true;}catch(error){
      if(error.code==='ESRCH')return false;
      if(error.code!=='EPERM')throw error;
      // macOS can report EPERM for a just-retired sandbox process group.
      // Confirm exact PGID membership, never infer absence from EPERM alone.
      return execFileSync('/bin/ps',['-axo','pgid='],{encoding:'utf8'}).split(/\s+/).some(value=>Number(value)===child.pid);
    }
  };
  const signalGroup=(child,signal)=>{
    if(!child.pid)return;
    try{process.kill(-child.pid,signal);}catch(error){
      if(error.code==='ESRCH'||(error.code==='EPERM'&&!groupAlive(child)))return;
      throw error;
    }
  };
  function save() {
    fs.writeFileSync(path.join(artifacts,'workspace-browser.json'),JSON.stringify({kind:'generated Chromium canonical workspace',mode:interactionsOnly?'interactions-only':sweepOnly?'sweep-only':'all',filtered:!!requested,manifestRoutes:manifest?.routes,selectedRoutes:requested||manifest?.routes,cases,issues,errors,external,timedOut},null,2));
    fs.writeFileSync(path.join(artifacts,'workspace-density.json'),JSON.stringify(density,null,2));
    fs.writeFileSync(path.join(artifacts,'workspace-interactions.json'),JSON.stringify(interactions,null,2));
  }
  function cleanup() {
    if(cleanupPromise)return cleanupPromise;
    cleanupPromise=(async()=>{
      if(cdp)cdp.ws.close();
      for(const child of [chrome,server])signalGroup(child,'SIGTERM');
      const deadline=Date.now()+3000;
      while([chrome,server].some(groupAlive)&&Date.now()<deadline)await pause(50);
      for(const child of [chrome,server])if(groupAlive(child))signalGroup(child,'SIGKILL');
      for(let i=0;i<40&&[chrome,server].some(groupAlive);i++)await pause(50);
      if(serverErrors)fs.writeFileSync(path.join(artifacts,'server.log'),serverErrors);
      const result={serverPid:server.pid,chromePid:chrome.pid,serverGroupGone:!groupAlive(server),chromeGroupGone:!groupAlive(chrome),timedOut};
      fs.writeFileSync(path.join(artifacts,'cleanup.json'),JSON.stringify(result,null,2));
      assert.ok(result.serverGroupGone&&result.chromeGroupGone,'Owned process groups survived cleanup');
    })();
    return cleanupPromise;
  }
  const watchdog=setTimeout(()=>{
    timedOut=true;save();console.error('Workspace acceptance deadline exceeded');
    cleanup().catch(console.error).finally(()=>process.exit(1));
  },timeout);
  const interrupted=()=>{timedOut=true;save();cleanup().catch(console.error).finally(()=>process.exit(1));};
  process.once('SIGTERM',interrupted);process.once('SIGINT',interrupted);
  try {
    let targets;
    for(let i=0;i<150;i++) {
      try {targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`,{signal:AbortSignal.timeout(1000)})).json();manifest=await(await fetch(base+'/__fixture',{signal:AbortSignal.timeout(1000)})).json();break;}catch{}
      if(spawnError)throw spawnError;
      if(server.exitCode!==null)throw Error(serverErrors);
      await pause(100);
    }
    assert.ok(targets&&manifest,serverErrors);
    assert.ok(Array.isArray(manifest.routes)&&manifest.routes.length,'Fixture must enumerate real routes');
    assert.equal(new Set(manifest.routes).size,manifest.routes.length,'Duplicate fixture routes');
    if(requested)for(const route of requested)assert.ok(manifest.routes.includes(route),'Unknown exact fixture route '+route);
    const routes=requested?[...new Set(requested)]:manifest.routes;
    const listener=message=>{
      if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails);
      if(message.method==='Runtime.consoleAPICalled'&&message.params.type==='error')errors.push(message.params.args.map(n=>n.value||n.description).join(' '));
      if(message.method==='Fetch.requestPaused') {
        const request=message.params;
        const foreign=/^https?:/.test(request.request.url)&&new URL(request.request.url).origin!==base;
        if(foreign)external.push(request.request.url);
        const operation=cdp.call(foreign?'Fetch.failRequest':'Fetch.continueRequest',foreign?{requestId:request.requestId,errorReason:'BlockedByClient'}:{requestId:request.requestId});
        inflight.add(operation);
        operation.catch(error=>errors.push('Request interception: '+error.message)).finally(()=>inflight.delete(operation));
      }
    };
    async function connect(target) {
      cdp=new Cdp(target.webSocketDebuggerUrl);cdp.listeners.push(listener);
      await cdp.call('Page.enable');await cdp.call('Runtime.enable');await cdp.call('Network.enable');
      await cdp.call('Network.setCacheDisabled',{cacheDisabled:true});
      await cdp.call('Network.setBypassServiceWorker',{bypass:true});
      await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    }
    async function drain() {
      await cdp.call('Fetch.disable');
      while(inflight.size)await Promise.allSettled([...inflight]);
    }
    await connect(targets.find(target=>target.type==='page'));
    async function visit(route) {
      const navigation=await cdp.call('Page.navigate',{url:base+route});
      assert.ok(!navigation.errorText,navigation.errorText);
      const url=new URL(base+route);
      await cdp.until(`location.pathname===${JSON.stringify(url.pathname)}&&location.search===${JSON.stringify(url.search)}&&document.readyState==='complete'&&!!document.querySelector('h1')&&!!document.querySelector('[data-theme-select]')&&typeof htmx==='object'`);
    }
    async function theme(value) {
      await cdp.eval(`(()=>{const s=document.querySelector('[data-theme-select]');s.value=${JSON.stringify(value)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await cdp.until(`document.documentElement.dataset.theme===${JSON.stringify(value)}&&document.documentElement.dataset.bsTheme===${JSON.stringify(value)}&&document.cookie.split(';').some(v=>v.trim()==='finn_nancy_theme='+${JSON.stringify(value)})`);
    }
    async function settled(){await cdp.until("!document.querySelector('.htmx-request,.htmx-settling,.htmx-swapping')");}
    async function click(selector) {
      await settled();
      const point=await cdp.eval(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n?.checkVisibility())throw Error('Invisible pointer target');n.scrollIntoView({block:'center',behavior:'instant'});const r=n.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,hit:n.contains(document.elementFromPoint(x,y))};})()`);
      assert.equal(point.hit,true,'Pointer target occluded: '+selector);
      for(const type of ['mousePressed','mouseReleased'])await cdp.call('Input.dispatchMouseEvent',{type,button:'left',clickCount:1,x:point.x,y:point.y});
    }
    async function screenshot(name) {
      if(process.env.WORKSPACE_SCREENSHOTS==='0')return;
      await cdp.eval('scrollTo(0,0)');
      const image=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(artifacts,name+'.png'),Buffer.from(image.data,'base64'));
    }
    function record(appearance,width,route,state,result) {
      const entry={appearance,width,route,state,...result};cases.push(entry);
      if(result.overflow||result.offenders.length||result.targets.length||result.smallFoundation.length||result.unnamedScrollers.length||result.current!==1||result.main!==1||result.headings!==1||!result.shell||result.legacy||result.appearance!==appearance||result.theme!==appearance||result.bootstrapTheme!==appearance||result.background!==(appearance==='dark'?'rgb(0, 0, 0)':'rgb(255, 255, 255)'))issues.push(entry);
    }
    // Every actual fixture destination, both appearances, all four viewports.
    for(const appearance of (interactionsOnly?[]:['dark','light'])) {
      for(const width of [320,390,768,1440]) {
        await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
        for(const [index,route] of routes.entries()) {
          if(process.env.WORKSPACE_VERBOSE)console.log(appearance,width,route);
          await visit(route);await theme(appearance);
          record(appearance,width,route,'collapsed',await cdp.eval(`(${inspectPage.toString()})()`));
          if(width===390||width===1440)await screenshot(`${index}-${route==='/'?'home':route.split('?')[0].replaceAll('/','-').slice(1)}-${appearance}-${width}`);
          await cdp.eval("document.querySelectorAll('main details').forEach(n=>n.open=true)");
          record(appearance,width,route,'expanded',await cdp.eval(`(${inspectPage.toString()})()`));
          save();
        }
      }
    }
    if(!interactionsOnly) {
      assert.equal(cases.length,routes.length*2*4*2,'Every selected route must have collapsed and expanded observations');
      assert.deepEqual(issues,[],'geometry/shell/navigation failures; see workspace-browser.json');
      console.log('Canonical route sweep passed: '+cases.length/2+' renders');
    }
    if(sweepOnly) {
      await drain();assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
      console.log(JSON.stringify({passed:true,routeCases:cases.length/2,artifacts,errors,external}));return;
    }
    // Retire the document/PDF renderer before the interaction acceptance wave.
    if(!interactionsOnly) {
      await drain();
      const oldTarget=targets.find(target=>target.type==='page').id;
      const next=await cdp.call('Target.createTarget',{url:'about:blank'});
      cdp.ws.close();
      targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();
      await connect(targets.find(target=>target.id===next.targetId));
      await cdp.call('Target.closeTarget',{targetId:oldTarget});
    }
    // One DOM, exact money, native disclosure, HTMX cursor and persistent theme.
    for(const appearance of ['dark','light'])for(const width of [320,390,768,1440]) {
      console.log(`Activity ${appearance}/${width}`);
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
      await visit('/activity?q=Generated%20market');await theme(appearance);
      const metrics=await cdp.eval(`(()=>{const rows=[...document.querySelectorAll('.tx-summary')],header=document.querySelector('.fn-header').getBoundingClientRect();return {
        first:rows[0].getBoundingClientRect().top,headerBottom:header.bottom,heights:rows.map(n=>n.getBoundingClientRect().height),
        visible:rows.filter(n=>{const r=n.getBoundingClientRect();return r.top>=Math.max(0,header.bottom)&&r.bottom<=innerHeight;}).length,
        count:rows.length,amounts:[...document.querySelectorAll('.tx-amount')].map(n=>n.textContent.trim()),
        clipped:[...document.querySelectorAll('.tx-amount')].some(n=>n.scrollWidth>n.clientWidth+1||n.scrollHeight>n.clientHeight+1||getComputedStyle(n).textOverflow==='ellipsis'),
        shell:!!document.querySelector('.fn-header .fn-nav')&&!!document.querySelector('main.fn-shell'),overflow:document.documentElement.scrollWidth>innerWidth+1};})()`);
      assert.equal(metrics.count,50);assert.equal(metrics.clipped,false);assert.equal(metrics.shell,true);assert.equal(metrics.overflow,false);
      assert.ok(metrics.heights.every(h=>h>=87.9),JSON.stringify(metrics));
      assert.ok(metrics.amounts.every(value=>value==='-$12.34 CAD'||value==='-$90,071,992,547,409.91 CAD'));
      density.push({appearance,width,...metrics});save();
      const firstId=await cdp.eval("window.workspaceFirstRow=document.querySelector('.tx-row');window.workspaceFirstRow.id");
      // Closed details may retain layout rectangles. Visibility and AX are authoritative.
      assert.equal(await cdp.eval("document.querySelector('.tx-evidence').checkVisibility()"),false);
      let ax=await cdp.call('Accessibility.getFullAXTree');
      assert.equal(ax.nodes.some(n=>!n.ignored&&n.role?.value==='link'&&n.name?.value==='Edit transaction'),false);
      await cdp.eval("document.querySelector('.tx-summary').focus()");
      await cdp.call('Input.dispatchKeyEvent',{type:'keyDown',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:49,text:' ',unmodifiedText:' '});
      await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:49});
      await cdp.until("document.querySelector('.tx-row').open");
      assert.equal(await cdp.eval("document.querySelector('.tx-evidence').checkVisibility()&&document.querySelector('.tx-row').innerText.includes('Accounting flow')"),true);
      ax=await cdp.call('Accessibility.getFullAXTree');
      assert.ok(ax.nodes.some(n=>!n.ignored&&n.role?.value==='link'&&n.name?.value==='Edit transaction'));
      assert.equal(await cdp.eval('document.documentElement.scrollWidth<=innerWidth+1'),true);
      // Enter closes the same native summary without a replacement controller.
      for(const type of ['keyDown','keyUp'])await cdp.call('Input.dispatchKeyEvent',{type,key:'Enter',code:'Enter',windowsVirtualKeyCode:13,nativeVirtualKeyCode:36,...(type==='keyDown'?{text:'\r',unmodifiedText:'\r'}:{})});
      await cdp.until("!document.querySelector('.tx-row').open");
      await click('.load-more');
      await cdp.until("document.querySelectorAll('.tx-row').length===63");await settled();
      assert.equal(await cdp.eval("new Set([...document.querySelectorAll('.tx-row')].map(n=>n.id)).size"),63);
      assert.equal(await cdp.eval(`document.getElementById(${JSON.stringify(firstId)})===window.workspaceFirstRow&&window.workspaceFirstRow.isConnected`),true,'Pagination must append to the same DOM rows');
      await cdp.eval('scrollTo(0,document.documentElement.scrollHeight)');
      assert.equal(await cdp.eval("(()=>{const n=[...document.querySelectorAll('.tx-row')].at(-1),r=n.getBoundingClientRect(),x=r.x+r.width/2,y=Math.min(innerHeight-1,r.bottom-2);return r.bottom<=innerHeight+1&&n.contains(document.elementFromPoint(x,y));})()"),true,'last row remains visible and hit-testable');
      await visit('/activity?q=Generated%20market%20000');
      assert.equal(await cdp.eval("document.querySelector('.tx-amount').textContent.trim()"),'-$90,071,992,547,409.91 CAD');
      assert.equal(await cdp.eval("document.querySelector('.tx-amount').scrollWidth<=document.querySelector('.tx-amount').clientWidth+1"),true);
      assert.equal(await cdp.eval(`document.documentElement.dataset.theme===${JSON.stringify(appearance)}&&document.querySelector('[data-theme-select]').value===${JSON.stringify(appearance)}`),true,'Appearance survives navigation');
      await screenshot(`extreme-${appearance}-${width}`);
      interactions.push({appearance,width,nativeSpace:true,nativeEnter:true,closedAX:true,expandedAX:true,cursorRows:63,exactMoney:'-$90,071,992,547,409.91 CAD',persistentAppearance:true});save();
    }
    await visit('/activity');
    for(const appearance of ['dark','light']) {
      await theme(appearance);
      await cdp.eval("document.querySelector('.tx-summary').focus()");
      await cdp.call('Input.dispatchKeyEvent',{type:'rawKeyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9,nativeVirtualKeyCode:48});
      await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9,nativeVirtualKeyCode:48});
      const contrast=await cdp.eval(`(()=>{
        const active=document.activeElement,s=getComputedStyle(active),root=getComputedStyle(document.documentElement);
        function rgb(value){const p=document.createElement('span');p.style.color=value;document.body.append(p);const v=getComputedStyle(p).color.match(/[\\d.]+/g).slice(0,3).map(Number);p.remove();return v;}
        function lum(c){return c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);}
        function ratio(a,b){const v=[lum(rgb(a)),lum(rgb(b))].sort((a,b)=>a-b);return(v[1]+.05)/(v[0]+.05);}
        const bg=root.getPropertyValue('--canvas').trim(),panel=root.getPropertyValue('--hover').trim();
        return {tag:active.tagName,className:active.className,focus:active.matches(':focus-visible'),outline:s.outlineStyle,width:parseFloat(s.outlineWidth),focusContrast:Math.min(ratio(s.outlineColor,bg),ratio(s.outlineColor,panel)),text:Object.fromEntries(['--finn','--nancy','--muted'].map(key=>[key,Math.min(ratio(root.getPropertyValue(key).trim(),bg),ratio(root.getPropertyValue(key).trim(),panel))]))};})()`);
      assert.equal(contrast.focus,true);assert.notEqual(contrast.outline,'none');assert.ok(contrast.width>=2&&contrast.focusContrast>=3,JSON.stringify(contrast));
      assert.ok(Object.values(contrast.text).every(value=>value>=4.5),JSON.stringify(contrast));
      fs.writeFileSync(path.join(artifacts,`contrast-${appearance}.json`),JSON.stringify(contrast,null,2));
    }
    await cdp.eval("document.querySelector('.tx-search').value='weekly groceries';document.querySelector('.tx-search').dispatchEvent(new Event('input',{bubbles:true}))");
    await cdp.until("document.querySelectorAll('.tx-row').length===3");await settled();
    await visit('/activity?account_id=2&category_id=8&date_from=2026-01-01&date_to=2026-01-31');
    assert.equal(await cdp.eval("document.querySelectorAll('.tx-row').length"),1);
    assert.equal(await cdp.eval("document.querySelector('.tx-filter-details').open"),true);
    await visit('/activity?q=absent-generated-merchant');
    assert.equal(await cdp.eval("document.querySelector('.tx-empty h2').textContent"),'No matching activity.');
    await click('.tx-empty a');await cdp.until("location.pathname==='/activity'&&location.search===''&&document.querySelectorAll('.tx-row').length===50");
    await visit('/');
    await cdp.eval("document.querySelector('#home-month').value='2026-01';document.querySelector('.home-month').requestSubmit()");
    await cdp.until("location.search==='?month=2026-01'&&document.readyState==='complete'&&document.querySelector('#home-month')?.value==='2026-01'");
    assert.ok(await cdp.eval("document.querySelector('.home-recent a.btn-link').href.includes('date_from=2026-01-01')"));
    await click('.fn-more > summary');
    assert.equal(await cdp.eval("document.querySelector('.fn-more').open&&[...document.querySelectorAll('.fn-more nav a')].every(n=>n.checkVisibility()&&n.getBoundingClientRect().height>=48)"),true);
    assert.equal(await cdp.eval('document.documentElement.scrollWidth<=innerWidth+1'),true);
    const ax=await cdp.call('Accessibility.getFullAXTree');
    assert.ok(ax.nodes.some(n=>!n.ignored&&n.role?.value==='navigation'&&n.name?.value==='primary'));
    assert.ok(ax.nodes.some(n=>!n.ignored&&n.role?.value==='combobox'&&n.name?.value==='Appearance'));
    await drain();assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    interactions.push({searchRows:3,composedFilterRows:1,clearFiltersRows:50,homeMonth:'2026-01',moreNavigation:true,appearanceAX:true});save();
    console.log(JSON.stringify({passed:true,routeCases:cases.length/2,densityCases:density.length,artifacts,errors,external}));
  } finally {
    clearTimeout(watchdog);process.removeListener('SIGTERM',interrupted);process.removeListener('SIGINT',interrupted);
    save();await cleanup();
  }
}
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={inspectPage};
