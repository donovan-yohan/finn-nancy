/* Owned headless Chrome and generated-only loopback fixture. No shared browser. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const os = require('node:os');
const root = path.resolve(__dirname, '../..');
const artifacts = path.resolve(process.argv[2] || path.join(os.tmpdir(), 'design-evidence'));
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'design-evidence-runtime-'));
const chrome = process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const pause = ms => new Promise(r => setTimeout(r, ms));
async function port() {
  const s = net.createServer(); await new Promise(r => s.listen(0,'127.0.0.1',r));
  const n = s.address().port; await new Promise(r => s.close(r)); return n;
}
async function poll(fn) {
  let last; for (let i=0;i<200;i++) {try {const result=await fn(); if(result)return result;} catch(e){last=e;} await pause(50);}
  throw last || new Error('Timed out');
}
class CDP {
  constructor(url) {
    this.id=0; this.pending=new Map(); this.errors=[]; this.socket=new WebSocket(url);
    this.ready=new Promise((res,rej)=>{this.socket.addEventListener('open',res);this.socket.addEventListener('error',rej);});
    this.socket.addEventListener('message',event=>{const m=JSON.parse(event.data); if(m.id){const p=this.pending.get(m.id);this.pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result);} else if(m.method==='Runtime.exceptionThrown')this.errors.push(m.params);});
  }
  async call(method,params={}) {await this.ready; const id=++this.id; const result=new Promise((resolve,reject)=>this.pending.set(id,{resolve,reject}));this.socket.send(JSON.stringify({id,method,params}));return result;}
  async eval(expression) {const r=await this.call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
  async go(url) {await this.call('Page.navigate',{url}); await poll(()=>this.eval(`location.href===${JSON.stringify(url)} && document.readyState==='complete'`));}
  async click(selector) {
    const pos=await this.eval(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}); if(!e)throw Error('Missing '+${JSON.stringify(selector)});e.scrollIntoView({block:'center',behavior:'instant'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await this.call('Input.dispatchMouseEvent',{type:'mousePressed',...pos,button:'left',clickCount:1});
    await this.call('Input.dispatchMouseEvent',{type:'mouseReleased',...pos,button:'left',clickCount:1});
  }
}
const fixture = String.raw`
import os,json
from pathlib import Path
root=Path(os.environ['EVIDENCE_RUNTIME'])
os.environ.update(DB_PATH=str(root/'fixture.sqlite'),DATA_DIR=str(root/'data'),STRICT_LOCAL_MODE='true',READ_ONLY='false',HOME_CURRENCY='CAD',API_TOKEN='',EMBEDDINGS_ENABLED='false',PLAID_ENABLED='false')
from app.config import Settings,get_settings
Settings.model_config['env_file']=None
get_settings.cache_clear()
from app.db import migrate,engine
migrate.init_db(os.environ['DB_PATH'])
from tests.test_design_evidence import seed_evidence
from tests.test_statement_reviews import _review,_parsed,_pdf
from tests.test_statement_source_view import _store,_good_pdf
ids=seed_evidence(os.environ['DB_PATH'])
blocked,unused=_review(os.environ['DB_PATH'],_parsed(),source_suffix='-blocked')
with engine.write_tx(os.environ['DB_PATH']) as conn:
 conn.execute("UPDATE accounts SET currency='USD' WHERE id=(SELECT account_id FROM statement_reviews WHERE source_document_id=?)",(blocked,))
(root/'data'/'blobs').mkdir(parents=True,exist_ok=True)
(root/'data'/'blobs'/'synthetic').write_bytes(_pdf())
(root/'data'/'blobs'/'synthetic-blocked').write_bytes(_pdf())
(root/'data'/'generated-receipt.txt').write_text('Generated receipt evidence')
ids['blocked']=blocked
ids['source']=_store(os.environ['DB_PATH'],_good_pdf(),name='generated-source.pdf')
from app.web.app import create_app
app=create_app()
@app.get('/__fixture')
def manifest():return ids
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['EVIDENCE_PORT']),lifespan='off',log_level='warning')
`;
(async()=>{
 fs.mkdirSync(artifacts,{recursive:true});
 const serverPort=await port(), debugPort=await port(), base=`http://127.0.0.1:${serverPort}`;
 let server,browser,cdp; const logs=[];const checks=[];
 try {
  server=spawn(path.join(root,'.venv/bin/python'),['-c',fixture],{cwd:root,env:{...process.env,EVIDENCE_RUNTIME:scratch,EVIDENCE_PORT:String(serverPort)},stdio:['ignore','pipe','pipe']});
  server.stdout.on('data',d=>logs.push(String(d)));server.stderr.on('data',d=>logs.push(String(d)));
  const ids=await poll(async()=>{const r=await fetch(base+'/__fixture');return r.ok&&r.json();});
  browser=spawn(chrome,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking',`--user-data-dir=${scratch}/chrome`,`--remote-debugging-port=${debugPort}`,'about:blank'],{stdio:'ignore'});
  const targets=await poll(async()=>{const r=await fetch(`http://127.0.0.1:${debugPort}/json/list`);return r.ok&&r.json();});
  cdp=new CDP(targets.find(t=>t.type==='page').webSocketDebuggerUrl); await cdp.call('Page.enable');await cdp.call('Runtime.enable');
  const routes=[['review','/review'],['statement',`/review/statement/${ids.statement}`],['blocked',`/review/statement/${ids.blocked}`],['recon','/recon?month=2026-06'],['source',`/review/pdf/${ids.source}`]];
  for(const width of [1440,390]) for(const theme of ['light','dark']) {
   await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
   for(const [name,route] of routes){
    await cdp.go(base+route);
    await cdp.eval(`document.documentElement.setAttribute('data-bs-theme','${theme}')`);
    await pause(200);
    const geometry=await cdp.eval(`(()=>{const main=document.querySelector('main'),h=document.querySelector('h1');return {width:innerWidth,scroll:document.documentElement.scrollWidth,main:main.getBoundingClientRect().left,heading:h.getBoundingClientRect().left,legacy:!!document.querySelector('link[href="/static/app.css"]'),controls:[...document.querySelectorAll('.evidence-workspace button,.evidence-workspace select,.evidence-workspace summary')].filter(e=>e.getBoundingClientRect().height).map(e=>({text:e.textContent.trim().slice(0,50),height:e.getBoundingClientRect().height})),labels:[...document.querySelectorAll('.evidence-workspace input:not([type=hidden]),.evidence-workspace select,.evidence-workspace textarea')].filter(e=>e.getBoundingClientRect().height).every(e=>e.labels.length>0)}})()`);
    if(geometry.scroll>width+1) { console.log(await cdp.eval('[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth && !e.closest("table")).map(e=>({tag:e.tagName,cls:e.className,x:e.getBoundingClientRect().x,right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width,overflow:getComputedStyle(e).overflowX,scroll:e.scrollWidth})).slice(0,40)')); console.log(await cdp.eval('[...document.querySelectorAll(".evidence-table-wrap")].map(e=>({w:e.clientWidth,s:e.scrollWidth,x:e.getBoundingClientRect().x,ov:getComputedStyle(e).overflowX}))')); const debug=await cdp.call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(artifacts,'overflow.png'),Buffer.from(debug.data,'base64')); }
    assert(geometry.scroll<=width+1,JSON.stringify({name,width,...geometry}));assert.equal(geometry.heading,geometry.main);assert.equal(geometry.legacy,false);assert.equal(geometry.labels,true);
    assert(geometry.controls.every(c=>c.height>=47),JSON.stringify(geometry.controls));
    const buttonColor=await cdp.eval('(()=>{const e=document.querySelector(".evidence-workspace .btn-outline-secondary");return e&&({color:getComputedStyle(e).color,opacity:getComputedStyle(e).opacity});})()');
    if(buttonColor)assert.equal(buttonColor.opacity,'1');
    const screenshot=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(artifacts,`${name}-${width}-${theme}.png`),Buffer.from(screenshot.data,'base64'));
    checks.push({name,width,theme,...geometry});
   }
  }
  // Scrolled working controls, not just page headers.
  for(const [name,route,selector] of [
    ['statement-editor',`/review/statement/${ids.statement}`,'.statement-metadata'],
    ['statement-row',`/review/statement/${ids.statement}`,'.statement-rows'],
    ['candidate','/recon?month=2026-06','.evidence-candidates'],
    ['receipt-editor','/review','.resolution-review-form']]) {
    await cdp.go(base+route);
    await cdp.eval(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'start',behavior:'instant'})`);
    const shot=await cdp.call('Page.captureScreenshot',{format:'png'});
    fs.writeFileSync(path.join(artifacts,`${name}-390.png`),Buffer.from(shot.data,'base64'));
  }
  for(const width of [390,320]) {
    await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
    await cdp.go(base+`/review/statement/${ids.statement}`);
    await cdp.eval('document.documentElement.style.fontSize="200%"');
    const geometry=await cdp.eval('({scroll:document.documentElement.scrollWidth,body:getComputedStyle(document.body).fontSize})');
    assert.equal(geometry.body,'30px');assert(geometry.scroll<=width+1,JSON.stringify({width,...geometry}));
  }
  await cdp.call('Emulation.setDeviceMetricsOverride',{width:390,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp.go(base+`/review/statement/${ids.statement}`);
  await cdp.click('.statement-row-tools summary');assert(await cdp.eval('document.querySelector(".statement-row-tools details").open'));
  assert(await cdp.eval('document.querySelector(".statement-row-form [name=expected_line_revision]").value === "1"'));
  await cdp.go(base+`/review/statement/${ids.blocked}`);assert(await cdp.eval('document.querySelector(".statement-approval-form button").disabled'));
  await cdp.go(base+`/review/pdf/${ids.source}#row-2`);assert(await cdp.eval('document.querySelector("#row-2").classList.contains("is-active")'));
  await cdp.click('#row-1');assert(await cdp.eval('document.querySelector("#region-1-0").classList.contains("is-active")'));
  await cdp.call('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowDown',code:'ArrowDown'});await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowDown',code:'ArrowDown'});
  assert(await cdp.eval('document.activeElement.id === "row-2"'));
  await cdp.go(base+'/recon?month=2026-06');
  await cdp.click(`form[action="/recon/line/${ids.negative_line}/confirm"] button`);
  await poll(()=>cdp.eval(`document.readyState==='complete' && !document.querySelector('form[action="/recon/line/${ids.negative_line}/confirm"]')`));
  // Use the real native POST form with its server-supplied month and flow guard.
  await cdp.go(base+'/recon?month=2026-06');
  await cdp.click(`form[action="/recon/line/${ids.positive_line}/promote"] button`);
  await poll(()=>cdp.eval('document.readyState === "complete" && !!document.querySelector("[action$=accept-classification]")'));
  const action=await cdp.eval('document.querySelector("[action$=accept-classification]").getAttribute("action")');
  await cdp.click(`form[action="${action}"] button`);
  await poll(()=>cdp.eval('document.readyState === "complete" && !!document.querySelector("[action$=undo]")'));
  assert(await cdp.eval('document.querySelector("[action$=undo] [name=evidence_fingerprint]").value.length > 0'));
  // Readback proves acceptance. Exact Undo POST is covered in Python using the rendered authority.
  assert.equal(cdp.errors.length,0,JSON.stringify(cdp.errors));
  const report={checks,interactions:['edit-row disclosure','hard-blocked approval disabled','source deep link','row/source paired pointer selection','ArrowDown source navigation','native candidate confirmation','native unknown-credit promotion','native accounting acceptance and resolved readback'],scaledText:{widths:[390,320],body:'30px',pageOverflow:false},runtimeErrors:cdp.errors};
  fs.writeFileSync(path.join(artifacts,'browser-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({renders:checks.length,interactions:8,runtimeErrors:0}));
 } finally {
  if(cdp)cdp.socket.close();
  for(const process of [browser,server]) if(process && process.exitCode===null){process.kill('SIGTERM');await Promise.race([new Promise(r=>process.once('exit',r)),pause(3000)]);if(process.exitCode===null)process.kill('SIGKILL');}
  fs.writeFileSync(path.join(artifacts,'fixture.log'),logs.join(''));
  fs.rmSync(scratch,{recursive:true,force:true});
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
