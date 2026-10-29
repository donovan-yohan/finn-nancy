/* Generated-only real-route browser proof. No shared browser or provider I/O. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function freePort() {
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  return port;
}
class Cdp {
  constructor(url) {
    this.id = 0; this.pending = new Map(); this.listeners = [];
    this.ws = new WebSocket(url);
    this.ready = new Promise((resolve,reject) => {this.ws.onopen=resolve;this.ws.onerror=reject;});
    this.ws.onmessage = event => {
      const message = JSON.parse(event.data);
      const pending = this.pending.get(message.id);
      if (pending) {
        this.pending.delete(message.id); clearTimeout(pending.timer);
        message.error ? pending.reject(Error(message.error.message)) : pending.resolve(message.result);
      } else if (!message.id) for (const listener of this.listeners) listener(message);
    };
  }
  async call(method,params={}) {
    await this.ready; const id=++this.id;
    return new Promise((resolve,reject) => {
      const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('CDP deadline: '+method));},15000);
      this.pending.set(id,{resolve,reject,timer});
      this.ws.send(JSON.stringify({id,method,params}));
    });
  }
  async eval(expression) {
    const result=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async until(expression) {
    for(let i=0;i<150;i++){if(await this.eval(expression))return;await pause(50);}
    throw Error('Condition not met: '+expression);
  }
}
async function main() {
  assert.ok(process.env.TMPDIR, 'Supply the task-owned scratch directory');
  const root=fs.mkdtempSync(path.join(process.env.TMPDIR,'design-activity-'));
  const artifacts=process.env.ACTIVITY_ARTIFACTS || path.join(root,'artifacts');
  fs.mkdirSync(artifacts,{recursive:true});
  const port=await freePort(),debug=await freePort(),base=`http://127.0.0.1:${port}`;
  const code=`import sys
from pathlib import Path
import uvicorn
from tests.browser.workspace_ui_fixture import generated_workspace
app=generated_workspace(Path(sys.argv[1]))
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[2]),lifespan='off',log_level='warning',access_log=False)`;
  const server=spawn(path.resolve('.venv/bin/python'),['-c',code,path.join(root,'data'),String(port)],{
    env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root,STRICT_LOCAL_MODE:'true',PLAID_ENABLED:'false'},stdio:['ignore','ignore','pipe']});
  let serverErrors='';server.stderr.on('data',data=>serverErrors+=data);
  const chrome=spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking',
    `--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'],{stdio:'ignore'});
  let cdp,failNext=false; const cases=[],errors=[],external=[],requests=[];
  try {
    let targets;
    for(let i=0;i<180;i++) {
      try {targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json(); if((await fetch(base+'/__fixture')).ok)break;}catch{}
      if(server.exitCode!==null)throw Error(serverErrors);
      await pause(100);
    }
    assert.ok(targets,serverErrors);
    cdp=new Cdp(targets.find(target=>target.type==='page').webSocketDebuggerUrl);
    await cdp.call('Page.enable');await cdp.call('Runtime.enable');await cdp.call('Network.enable');
    await cdp.call('Network.setBypassServiceWorker',{bypass:true});
    cdp.listeners.push(message=>{
      if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);
      if(message.method==='Fetch.requestPaused') {
        const req=message.params,url=req.request.url;
        if(/^https?:/.test(url)&&!url.startsWith(base+'/')) {external.push(url);cdp.call('Fetch.failRequest',{requestId:req.requestId,errorReason:'BlockedByClient'});}
        else if(url.startsWith(base+'/transactions')) {
          requests.push(url);
          if(failNext){failNext=false;cdp.call('Fetch.fulfillRequest',{requestId:req.requestId,responseCode:503,responseHeaders:[{name:'Content-Type',value:'text/plain'}],body:Buffer.from('Generated read failure').toString('base64')});}
          else cdp.call('Fetch.continueRequest',{requestId:req.requestId});
        } else cdp.call('Fetch.continueRequest',{requestId:req.requestId});
      }
    });
    await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    async function visit(route) {
      await cdp.call('Page.navigate',{url:base+route});
      await cdp.until(`location.pathname===${JSON.stringify(route.split('?')[0])}&&location.search===${JSON.stringify(route.includes('?')?'?'+route.split('?')[1]:'')}&&document.readyState==='complete'&&!!document.querySelector('h1')`);
    }
    async function settled(){await cdp.until("!document.querySelector('.htmx-request,.htmx-settling,.htmx-swapping')");}
    async function click(selector) {
      await settled();
      const pos=await cdp.eval(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});n.scrollIntoView({block:'center',behavior:'instant'});const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
      await cdp.call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...pos});
      await cdp.call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...pos});
    }
    for(const theme of ['light','dark']) {
      await cdp.call('Network.setCookie',{url:base,name:'finn_nancy_theme',value:theme,path:'/'});
      for(const width of [390,1440]) {
        await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
        for(const route of ['/','/activity','/txn/3/edit']) {
          await visit(route);
          const result=await cdp.eval(`({route:location.pathname,width:innerWidth,theme:document.documentElement.dataset.theme,
            overflow:document.documentElement.scrollWidth>innerWidth+1,
            legacy:[...document.styleSheets].some(s=>s.href?.endsWith('/app.css')),
            heading:document.querySelector('h1').textContent,
            smallControls:[...document.querySelectorAll('main .btn,main .form-control,main .form-select,main summary')].filter(n=>n.getClientRects().length&&n.getBoundingClientRect().height<47).map(n=>n.outerHTML.slice(0,150))})`);
          assert.equal(result.overflow,false,JSON.stringify(result));assert.equal(result.legacy,false);assert.deepEqual(result.smallControls,[]);
          cases.push(result);
          if(route==='/activity'||(theme==='light'&&width===1440)) {
            const image=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
            fs.writeFileSync(path.join(artifacts,`${route==='/'?'home':route.includes('edit')?'editor':'activity'}-${theme}-${width}.png`),Buffer.from(image.data,'base64'));
          }
        }
      }
    }
    await visit('/activity');
    // Native keyboard disclosure, not a replacement click controller.
    await cdp.eval("document.querySelector('.tx-summary').focus()");
    for(const type of ['keyDown','keyUp'])await cdp.call('Input.dispatchKeyEvent',{type,key:'Enter',code:'Enter',windowsVirtualKeyCode:13,nativeVirtualKeyCode:36,text:type==='keyDown'?'\r':'',unmodifiedText:type==='keyDown'?'\r':''});
    await cdp.until("document.querySelector('.tx-row').open");
    const expanded=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    fs.writeFileSync(path.join(artifacts,'activity-expanded-dark-1440.png'),Buffer.from(expanded.data,'base64'));
    const firstId=await cdp.eval("document.querySelector('.tx-row').id");
    const oldCount=await cdp.eval("document.querySelectorAll('.tx-row').length");
    await click('.load-more');
    await cdp.until(`document.querySelectorAll('.tx-row').length>${oldCount}`);await settled();
    assert.equal(await cdp.eval(`document.getElementById(${JSON.stringify(firstId)}).open`),true);
    assert.equal(await cdp.eval("document.activeElement.matches('.tx-summary')"),true);
    const ids=await cdp.eval("[...document.querySelectorAll('.tx-row')].map(n=>n.id)");
    assert.equal(new Set(ids).size,ids.length);
    // Input fires one read; search blur must not replace the pointer target.
    await cdp.eval("document.querySelector('.tx-search').value='absent-generated-merchant';document.querySelector('.tx-search').dispatchEvent(new Event('input',{bubbles:true}))");
    await cdp.until("document.querySelector('.tx-empty')?.textContent.includes('No matching activity.')");await settled();
    await click('.tx-empty a');await cdp.until("location.search===''&&document.querySelectorAll('.tx-row').length===50");
    // Failed reads keep rows, expose Retry, and recover the exact failed page.
    await cdp.eval("window.activityTrace=[];for(const type of ['htmx:beforeRequest','htmx:afterRequest','htmx:responseError'])document.body.addEventListener(type,e=>activityTrace.push({type,target:e.detail.target?.outerHTML?.slice(0,100),elt:e.detail.elt?.outerHTML?.slice(0,100),failed:e.detail.failed,successful:e.detail.successful,status:e.detail.xhr?.status,errorHidden:document.getElementById('activity-read-error').hidden}))");
    failNext=true;
    await click('.load-more');await cdp.until("!document.getElementById('activity-read-error').hidden");
    const errorVisible=await cdp.eval("(()=>{const r=document.getElementById('activity-read-error').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;})()");
    assert.equal(errorVisible,true,'Read failure and Retry must be in the viewport');
    const errorImage=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
    fs.writeFileSync(path.join(artifacts,'activity-read-error.png'),Buffer.from(errorImage.data,'base64'));
    assert.equal(await cdp.eval("document.querySelectorAll('.tx-row').length"),50);
    const failedURL=requests.at(-1);
    await click('#activity-retry');await cdp.until("document.querySelectorAll('.tx-row').length>50");await settled();
    assert.equal(requests.at(-1),failedURL);
    assert.equal(await cdp.eval("document.getElementById('activity-read-error').hidden"),true);
    // Invalid range never issues a successful-looking empty read.
    const before=requests.length;
    await cdp.eval("document.querySelector('.tx-filter-details').open=true;const f=document.querySelector('.tx-filters');f.elements.date_from.value='2026-07-01';f.elements.date_to.value='2026-06-01';f.elements.date_to.dispatchEvent(new Event('change',{bubbles:true}))");
    await pause(650);
    assert.equal(requests.length,before);
    assert.equal(await cdp.eval("document.getElementById('activity-date-error').hidden"),false);
    // Actual filter submission and cursor append preserve every query field.
    await visit('/activity?q=Generated+market&account_id=1&category_id=4&flow_kind=purchase&date_from=2026-06-01&date_to=2026-06-30');
    await click('.tx-toolbar button');await settled();
    assert.equal(await cdp.eval("document.querySelectorAll('.tx-row').length"),50);
    await click('.load-more');await cdp.until("document.querySelectorAll('.tx-row').length===63");await settled();
    assert.equal(await cdp.eval("document.getElementById('activity-loading').hidden"),true);
    const filteredURL=new URL(requests.at(-1));
    for(const [key,value] of Object.entries({q:'Generated market',account_id:'1',category_id:'4',flow_kind:'purchase',date_from:'2026-06-01',date_to:'2026-06-30'})) assert.equal(filteredURL.searchParams.get(key),value);
    // Exact cents remain intact under narrow and enlarged rendering.
    for(const width of [320,390,768]) {
      await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
      await visit('/activity?q=Generated+market+000');
      await cdp.eval("document.documentElement.style.fontSize='200%'");
      assert.equal(await cdp.eval('document.documentElement.scrollWidth>innerWidth+1'),false,`enlarged overflow ${width}`);
      assert.ok((await cdp.eval("document.querySelector('.tx-amount').textContent")).includes('90,071,992,547,409.91'));
    }
    for(const route of ['/','/txn/3/edit']) {
      await cdp.call('Emulation.setDeviceMetricsOverride',{width:320,height:1000,deviceScaleFactor:1,mobile:false});
      await visit(route);await cdp.eval("document.documentElement.style.fontSize='200%'");
      assert.equal(await cdp.eval('document.documentElement.scrollWidth>innerWidth+1'),false,`enlarged ${route}`);
      assert.ok(await cdp.eval('parseFloat(getComputedStyle(document.body).fontSize)>=30'));
    }
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(artifacts,'browser-report.json'),JSON.stringify({passed:true,cases,interactions:['native Enter disclosure','append retains open row','unique cursor rows','focus after append','search input and pointer Clear filters','503 keeps rows','retry exact cursor','invalid dates block request','composed filters retained in cursor','200% extreme cents'],errors,external,ownedRoot:root},null,2));
    console.log(JSON.stringify({passed:true,renderCases:cases.length,interactions:10,artifacts,ownedRoot:root}));
  } catch (error) {
    console.error(JSON.stringify({errors,external,requests, state:cdp?await cdp.eval("({overflow:[...document.querySelectorAll('main *')].filter(n=>n.getClientRects().length&&n.getBoundingClientRect().right>innerWidth+1).map(n=>({tag:n.tagName,cls:n.className,parent:n.parentElement.outerHTML.slice(0,600),text:n.textContent.slice(0,80),width:n.getBoundingClientRect().width})).slice(0,20),trace:window.activityTrace,path:location.href,error:document.getElementById('activity-read-error')?.outerHTML,loading:document.getElementById('activity-loading')?.outerHTML,rows:document.querySelectorAll('.tx-row').length,buttons:[...document.querySelectorAll('.load-more')].map(n=>n.outerHTML)})"):null}));
    throw error;
  } finally {
    if(cdp)cdp.ws.close();
    await Promise.all([chrome,server].map(child=>new Promise(resolve=>{
      if(child.exitCode!==null){resolve();return;}
      child.once('exit',resolve);child.kill('SIGTERM');
      setTimeout(()=>{if(child.exitCode===null)child.kill('SIGKILL');},3000).unref();
    })));
    if(serverErrors)fs.writeFileSync(path.join(artifacts,'server.log'),serverErrors);
    fs.writeFileSync(path.join(artifacts,'cleanup.json'),JSON.stringify({serverExited:server.exitCode!==null||server.signalCode!==null,chromeExited:chrome.exitCode!==null||chrome.signalCode!==null}));
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
