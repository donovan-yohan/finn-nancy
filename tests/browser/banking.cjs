/* Generated canonical banking journey; actual app factory, no provider egress. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),net=require('node:net');
const {spawn}=require('node:child_process');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function port(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
class CDP{
 constructor(url){this.id=0;this.pending=new Map();this.listeners=[];this.ws=new WebSocket(url);this.ready=new Promise((r,j)=>{this.ws.onopen=r;this.ws.onerror=j;});this.ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=this.pending.get(m.id);this.pending.delete(m.id);if(!p)return;m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}else this.listeners.forEach(f=>f(m));};}
 async call(method,params={}){await this.ready;const id=++this.id;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('CDP deadline '+method));},15000);this.pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});this.ws.send(JSON.stringify({id,method,params}));});}
 close(){for(const p of this.pending.values())p.reject(Error('Harness closing'));this.pending.clear();this.ws.close();}
 async eval(expression){const r=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
 async until(expression){for(let i=0;i<160;i++){if(await this.eval(expression))return;await pause(50);}throw Error('Timeout '+expression+' '+await this.eval('document.body.innerText'));}
}
async function main(){
 const root=fs.mkdtempSync(path.join(process.env.TMPDIR||os.tmpdir(),'banking-browser-'));console.log('ARTIFACTS '+root);
 const p=await port(),debug=await port(),base=`http://127.0.0.1:${p}`;
 const code='import sys;from pathlib import Path;import uvicorn;from tests.browser.banking_fixture import generated_banking;uvicorn.run(generated_banking(Path(sys.argv[1]),sys.argv[2]),host="127.0.0.1",port=int(sys.argv[3]),lifespan="off",log_level="warning",access_log=False)';
 const server=spawn(path.resolve('.venv/bin/python'),['-c',code,path.join(root,'data'),base,String(p)],{env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root,STRICT_LOCAL_MODE:'true'},stdio:['ignore','ignore','pipe']});let stderr='';server.stderr.on('data',d=>stderr+=d);
 const chrome=spawn(process.env.CHROMIUM_BIN||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking',`--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'],{stdio:'ignore'});
 let c,hostedCdp;const cases=[],external=[],errors=[],interceptionErrors=[],interceptions=new Set();
 function intercept(client,method,params){const work=client.call(method,params).catch(e=>interceptionErrors.push({method,error:e.message})).finally(()=>interceptions.delete(work));interceptions.add(work);}
 async function drain(client){await client.call('Fetch.disable');await Promise.all([...interceptions]);}
 try{
  let targets;
  for(let i=0;i<150;i++){try{targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();await fetch(base+'/__banking_fixture').then(r=>r.json());break;}catch{}if(server.exitCode!==null)throw Error(stderr);await pause(100);}
  assert.ok(targets,stderr);c=new CDP(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
  await c.call('Page.enable');await c.call('Runtime.enable');await c.call('Network.enable');await c.call('Network.setBypassServiceWorker',{bypass:true});
  c.listeners.push(m=>{if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);if(m.method==='Fetch.requestPaused'){const q=m.params,url=q.request.url;if(/^https?:/.test(url)&&!url.startsWith(base+'/')){external.push(url);intercept(c,'Fetch.failRequest',{requestId:q.requestId,errorReason:'BlockedByClient'});}else intercept(c,'Fetch.continueRequest',{requestId:q.requestId});}});
  await c.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
  async function visit(route){await c.call('Page.navigate',{url:base+route});await c.until(`location.pathname===${JSON.stringify(route.split('?')[0])}&&document.readyState==='complete'&&!!document.querySelector('h1')`);}
  async function click(selector){await c.eval(`document.querySelector(${JSON.stringify(selector)}).click()`);}
  async function set(selector,value){await c.eval(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);}
  await visit('/connections');
  for(const name of ['Generated Alex','Generated Blair']){await set('#banking-member input',name);await click('#banking-member button');await c.until(`document.readyState==='complete'&&!!document.querySelector('[data-banking-ready="true"]')&&document.querySelector('#banking-member input')?.value===''&&document.body.innerText.includes(${JSON.stringify(name)})`);}
  assert.equal((await(await fetch(base+'/__banking_fixture')).json()).members,2);cases.push('two members through canonical browser setup');
  for(let member=1;member<=2;member++){
   await click(`[data-member="${member}"] summary`);await click(`[data-consent="${member}"]`);await c.until(`document.readyState==='complete'&&!!document.querySelector('[data-banking-ready="true"] [data-withdraw="${member}"]')`);
   await click(`[data-link="${member}"]`);await c.until("!document.getElementById('banking-link').hidden&&!!document.getElementById('banking-hosted').getAttribute('href')");
   const hosted=await c.eval("document.getElementById('banking-hosted').href");assert.equal(hosted,`https://secure.plaid.com/hl/generated-${member}`);
   // Navigate the generated, intercepted Hosted UI then return without tokens.
   const hostedTarget=await c.call('Target.createTarget',{url:'about:blank'});
   const allTargets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();
   hostedCdp=new CDP(allTargets.find(t=>t.id===hostedTarget.targetId).webSocketDebuggerUrl);
   await hostedCdp.call('Page.enable');await hostedCdp.call('Runtime.enable');
   hostedCdp.listeners.push(m=>{if(m.method==='Fetch.requestPaused'){const q=m.params;if(q.resourceType==='Document'&&q.request.url===hosted)intercept(hostedCdp,'Fetch.fulfillRequest',{requestId:q.requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:'text/html'}],body:Buffer.from('<html><head><link rel="icon" href="data:,"></head><body><h1>Generated Hosted Link</h1><button id="authorize" onclick="document.body.dataset.completed=1">Authorize generated institution</button></body></html>').toString('base64')});else intercept(hostedCdp,'Fetch.failRequest',{requestId:q.requestId,errorReason:'BlockedByClient'});}});
   await hostedCdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
   await hostedCdp.call('Page.navigate',{url:hosted});await hostedCdp.until("document.querySelector('#authorize')!==null");
   await hostedCdp.eval("document.querySelector('#authorize').click()");await hostedCdp.until("document.body.dataset.completed==='1'");
   await drain(hostedCdp);hostedCdp.close();hostedCdp=null;await c.call('Target.closeTarget',{targetId:hostedTarget.targetId});
   await fetch(base+`/__banking_fixture/hosted/${member}`,{method:'POST'});
   await visit('/connections');await click('[data-finish-session]');
   await c.until(`document.readyState==='complete'&&!!document.querySelector('[data-banking-ready="true"] [data-connection="${member}"]')`);
   const form=`.banking-binding[data-connection="${member}"]`;
   await click(`[data-connection="${member}"] .banking-account summary`);
   if(member===1){await set(form+' [name=new_name]','Generated household chequing');}
   else{await set(form+' [name=account_id]','1');await set(form+' [name=role]','secondary');}
   await click(form+' [name=included]');await click(form+' [type=submit]');
   await c.until(`document.readyState==='complete'&&!!document.querySelector('[data-banking-ready="true"] .banking-binding[data-connection="${member}"][data-revision="1"]')`);
   await click(`[data-sync="${member}"]`);
   for(let attempt=0;attempt<100;attempt++){const state=await(await fetch(base+'/__banking_fixture')).json();if(state.dispatched.filter(p=>p==='/transactions/sync').length>=member)break;await pause(50);if(attempt===99)throw Error('Sync not dispatched '+await c.eval('document.body.innerText'));}
   await c.until(`document.readyState==='complete'&&!!document.querySelector('[data-banking-ready="true"] [data-sync="${member}"]:not(:disabled)')`);
   cases.push(`member ${member} consent → hosted server verification → explicit ${member===1?'primary new account':'secondary joint binding'} → sync`);
  }
  await visit('/bank-feed/1');await click('[name=action][value=new]');await set('[name=flow_kind]','purchase');await set('[name=reason]','Reviewed generated daily expense');await click('#banking-decision [type=submit]');await c.until("location.pathname==='/bank-feed'&&document.readyState==='complete'");
  await visit('/bank-feed/2');await click('[name=action][value="match:1"]');await set('[name=reason]','Reviewed same shared charge');await click('#banking-decision [type=submit]');await c.until("location.pathname==='/bank-feed'&&document.readyState==='complete'");
  const evidence=await(await fetch(base+'/__banking_fixture')).json();assert.equal(evidence.transactions,1);assert.equal(evidence.associations,2);cases.push('primary explicit purchase + secondary explicit match = one canonical booking');
  await visit('/?month=2026-06');assert.ok((await c.eval('document.body.innerText')).includes('12.34'));cases.push('canonical dashboard includes reviewed bank purchase');
  const transactionsResponse=await fetch(base+'/api/finance/transactions?limit=20',{headers:{Authorization:'Bearer generated-browser-finance'}});assert.equal(transactionsResponse.status,200);
  const transactions=await transactionsResponse.json();assert.equal(transactions.rows.length,1);assert.equal(transactions.rows[0].source,'bank_feed');assert.equal(transactions.rows[0].amount_cents,-1234);
  fs.writeFileSync(path.join(root,'finance-api.json'),JSON.stringify(transactions,null,2));cases.push('canonical Namako finance API readback');
  for(const theme of ['light','dark'])for(const width of [320,390,768,1440]){
   await c.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
   for(const route of ['/connections','/bank-feed','/bank-feed/1']){
    await visit(route);await c.eval(`(()=>{const s=document.querySelector('[data-theme-select]');s.value=${JSON.stringify(theme)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    const geometry=await c.eval('({scroll:document.documentElement.scrollWidth,width:innerWidth,title:document.querySelector("h1").innerText})');assert.ok(geometry.scroll<=geometry.width+1,JSON.stringify({theme,width,route,geometry}));
    const image=await c.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(root,`${theme}-${width}-${route.replaceAll('/','_')}.png`),Buffer.from(image.data,'base64'));
   }
  }
  await drain(c);assert.deepEqual(interceptionErrors,[]);assert.deepEqual(external,[]);assert.deepEqual(errors,[]);cases.push('320/390/768/1440 both themes no horizontal overflow; no browser egress/runtime exceptions');
  fs.writeFileSync(path.join(root,'evidence.json'),JSON.stringify({cases,evidence,external,errors,fixture:'canonical create_app + app.state.banking_service.transport MockTransport'},null,2));console.log(JSON.stringify({passed:cases.length,cases,artifacts:root}));
 }finally{if(hostedCdp)hostedCdp.close();if(c)c.close();await Promise.all([...interceptions]);chrome.kill('SIGTERM');server.kill('SIGTERM');await Promise.all([chrome,server].map(child=>child.exitCode!==null?Promise.resolve():new Promise(resolve=>{const timer=setTimeout(()=>{child.kill('SIGKILL');resolve();},3000);child.once('exit',()=>{clearTimeout(timer);resolve();});})));fs.writeFileSync(path.join(root,'server-errors.txt'),stderr);fs.writeFileSync(path.join(root,'interception-errors.json'),JSON.stringify(interceptionErrors,null,2));}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
