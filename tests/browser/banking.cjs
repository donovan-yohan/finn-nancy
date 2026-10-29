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
 let c,hostedCdp;const cases=[],external=[],errors=[],httpErrors=[],interceptionErrors=[],interceptions=new Set();
 function intercept(client,method,params){const work=client.call(method,params).catch(e=>interceptionErrors.push({method,error:e.message})).finally(()=>interceptions.delete(work));interceptions.add(work);}
 async function drain(client){await client.call('Fetch.disable');await Promise.all([...interceptions]);}
 try{
  let targets;
  for(let i=0;i<150;i++){try{targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();await fetch(base+'/__banking_fixture').then(r=>r.json());break;}catch{}if(server.exitCode!==null)throw Error(stderr);await pause(100);}
  assert.ok(targets,stderr);c=new CDP(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
  await c.call('Page.enable');await c.call('Runtime.enable');await c.call('Network.enable');await c.call('Network.setBypassServiceWorker',{bypass:true});
  c.listeners.push(m=>{if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);if(m.method==='Network.responseReceived'&&m.params.response.status>=400)httpErrors.push({requestId:m.params.requestId,status:m.params.response.status,url:m.params.response.url});if(m.method==='Fetch.requestPaused'){const q=m.params,url=q.request.url;if(/^https?:/.test(url)&&!url.startsWith(base+'/')){external.push(url);intercept(c,'Fetch.failRequest',{requestId:q.requestId,errorReason:'BlockedByClient'});}else intercept(c,'Fetch.continueRequest',{requestId:q.requestId});}});
  await c.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
  async function visit(route){await c.call('Page.navigate',{url:base+route});await c.until(`location.pathname===${JSON.stringify(route.split('?')[0])}&&document.readyState==='complete'&&!!document.querySelector('h1')`);}
  async function click(selector){
   await c.eval(`new Promise(resolve=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)throw Error('Missing control');e.scrollIntoView({block:'center',behavior:'instant'});requestAnimationFrame(()=>requestAnimationFrame(resolve));})`);
   const box=await c.eval(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});const r=e.getBoundingClientRect();if(r.width===0||r.height===0)throw Error('Hidden control');const x=r.x+r.width/2,y=r.y+r.height/2;const hit=document.elementFromPoint(x,y);if(!e.contains(hit))throw Error('Control covered');return{x,y};})()`);
   await c.call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...box});
   await c.call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...box});
  }
  async function set(selector,value){await c.eval(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);}
  await fetch(base+'/__banking_fixture/setup/local_only',{method:'POST'});
  await visit('/connections');

  const onboarding='[data-household-onboarding]';
  async function settled(){await c.until(`document.querySelector('${onboarding}')?.dataset.lastOperation==='settled'&&!document.querySelector('${onboarding}').hasAttribute('aria-busy')`);}
  async function capture(name){const image=await c.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(root,name+'.png'),Buffer.from(image.data,'base64'));}
  const empty=await c.eval('document.body.innerText');assert.ok(empty.includes('Add yourself'));assert.ok(!empty.includes('Sample Member'));assert.equal((await(await fetch(base+'/__banking_fixture')).json()).members,0);
  for(const theme of ['light','dark'])for(const width of [320,390,768,1440]){
   await c.call('Emulation.setDeviceMetricsOverride',{width,height:844,deviceScaleFactor:1,mobile:width<500});
   await set('[data-theme-select]',theme);
   const geometry=await c.eval('({scroll:document.documentElement.scrollWidth,width:innerWidth})');assert.ok(geometry.scroll<=width+1,JSON.stringify(geometry));await capture(`empty-${width}-${theme}`);
  }
  for(const name of ['Generated Alex','Generated Blair']){await set('#member-name',name);await click('form[data-form="member-add"] button');await c.until(`document.readyState==='complete'&&!!document.querySelector('[data-onboarding-ready="true"]')&&document.querySelector('#member-name')?.value===''&&document.body.innerText.includes(${JSON.stringify(name)})&&document.querySelector('[data-household-onboarding]').dataset.lastOperation==='settled'`);}
  const household=await(await fetch(base+'/__banking_fixture')).json();assert.equal(household.members,2);assert.deepEqual(household.dispatched,[]);cases.push('default local-only household with no Plaid origin → two named members without provider dispatch');
  await fetch(base+'/__banking_fixture/setup/ready',{method:'POST'});await visit('/connections');
  for(const [index,person] of household.people.entries()){
   const member=person.id,ordinal=index+1;
   await click(`[data-disclosure-key="consent-${member}"] > summary`);await click(`[data-action="consent"][data-member-id="${member}"]`);await settled();
   assert.equal((await(await fetch(base+'/__banking_fixture')).json()).dispatched.length,index*5);
   await click(`[data-action="link"][data-member-id="${member}"]`);await settled();for(const failure of httpErrors.filter(x=>x.url.includes('/connections/data/'))){const body=await c.call('Network.getResponseBody',{requestId:failure.requestId});console.log('generated request failure',failure.status,new URL(failure.url).pathname,body.body);}assert.equal(httpErrors.filter(x=>x.url.includes('/connections/data/')).length,0);await c.until("!!document.querySelector('a[data-focus-key=\"hosted-link\"]')");
   const hosted=await c.eval("document.querySelector('a[data-focus-key=\"hosted-link\"]').href");assert.equal(hosted,`https://secure.plaid.com/hl/generated-${ordinal}`);
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
   await fetch(base+`/__banking_fixture/hosted/${ordinal}`,{method:'POST'});
   await visit('/connections');await click(`[data-member="${member}"] [data-action="finish"]`);await settled();
   await c.until(`!!document.querySelector('[data-onboarding-ready="true"] [data-connection="${ordinal}"]')`);
   const form=`form[data-form="binding"][data-connection-id="${ordinal}"]`;
   await click(`[data-connection="${ordinal}"] [data-editor] > summary`);
   assert.deepEqual(await c.eval(`['account_id','owner','included','role'].map(n=>document.querySelector(${JSON.stringify(form)}).elements[n].value)`),['','','','']);
   if(ordinal===1){await set(form+' [name=account_id]','new');await set(form+' [name=new_name]','Generated household chequing');await set(form+' [name=role]','primary');}
   else{await set(form+' [name=account_id]','1');await set(form+' [name=role]','secondary');}
   await set(form+' [name=owner]','shared');await set(form+' [name=included]','include');await click(form+' [type=submit]');await settled();
   assert.equal(await c.eval(`document.querySelector(${JSON.stringify(form)}).dataset.revision`),'1');
   await click(`[data-action="sync"][data-connection-id="${ordinal}"]`);await settled();
   cases.push(`member ${ordinal} consent → hosted server verification → explicit ${ordinal===1?'primary new account':'secondary joint binding'} → sync`);
  }
  await c.call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await visit('/connections');
  const first='form[data-form="binding"][data-connection-id="1"]',second='form[data-form="binding"][data-connection-id="2"]';
  for(const connection of [1,2])await click(`[data-connection="${connection}"] [data-editor] > summary`);
  await set(first+' [name=owner]',String(household.people[0].id));
  await set(second+' [name=included]','exclude');
  await fetch(base+'/__banking_fixture/failure/bind',{method:'POST'});
  await click(first+' [type=submit]');await settled();
  assert.equal(await c.eval(`document.querySelector(${JSON.stringify(first)}).elements.owner.value`),String(household.people[0].id));
  assert.equal(await c.eval(`document.querySelector(${JSON.stringify(second)}).elements.included.value`),'exclude');
  assert.equal((await(await fetch(base+'/__banking_fixture')).json()).bindings[0].ownership,'shared');
  await fetch(base+'/__banking_fixture/failure/read',{method:'POST'});
  await fetch(base+'/__banking_fixture/failure/hold_read',{method:'POST'});
  await click(first+' [type=submit]');
  for(let attempt=0;attempt<120;attempt++){if((await(await fetch(base+'/__banking_fixture')).json()).failures.read_waiting)break;if(attempt===119)throw Error('Held read boundary not reached');await pause(25);}
  assert.ok(await c.eval(`[...document.querySelectorAll('[data-household-onboarding] input,[data-household-onboarding] select,[data-household-onboarding] button:not([data-action="refresh"])')].every(e=>e.disabled)`));
  const committed=(await(await fetch(base+'/__banking_fixture')).json()).bindings[0];assert.equal(committed.ownership,'personal');assert.equal(committed.owner_member_id,household.people[0].id);
  await fetch(base+'/__banking_fixture/failure/release_read',{method:'POST'});
  await c.until(`document.querySelector('[data-household-onboarding]').dataset.lastOperation==='read-failed'`);
  assert.ok(await c.eval(`[...document.querySelectorAll('[data-household-onboarding] input,[data-household-onboarding] select,[data-household-onboarding] button:not([data-action="refresh"])')].every(e=>e.disabled)`));
  await click('[data-action="refresh"]');await settled();
  assert.equal(await c.eval(`document.querySelector(${JSON.stringify(second)}).elements.included.value`),'exclude');
  assert.equal(await c.eval(`document.querySelector(${JSON.stringify(first)}).dataset.revision`),'2');
  await click(second+' [type=reset]');assert.equal(await c.eval(`document.querySelector(${JSON.stringify(second)}).elements.included.value`),'include');
  assert.equal(await c.eval(`document.activeElement.dataset.focusKey`),'edit-account-2-2');
  await set(first+' [name=owner]','shared');await click(first+' [type=submit]');await settled();
  for(const state of ['disabled','strict_local','unconfigured','read_only']){
   await fetch(base+'/__banking_fixture/setup/'+state,{method:'POST'});await visit('/connections');
   assert.ok(await c.eval(`[...document.querySelectorAll('[data-action="link"],[data-action="accounts"],[data-action="sync"],[data-action="disconnect"]')].every(e=>e.disabled)`));
   assert.equal(await c.eval('document.querySelector("#member-name").disabled'),state==='read_only');
   await capture(`setup-${state}-390`);
  }
  await fetch(base+'/__banking_fixture/setup/ready',{method:'POST'});await visit('/connections');
  for(const theme of ['light','dark']){
   await set('[data-theme-select]',theme);await c.eval('document.documentElement.style.fontSize="200%"');
   await click('[data-connection="1"] [data-editor] > summary');
   const geometry=await c.eval(`({width:innerWidth,scroll:document.documentElement.scrollWidth,font:getComputedStyle(document.querySelector('#member-name')).fontSize,targets:[...document.querySelectorAll('button,input,select,summary')].filter(e=>e.getClientRects().length).map(e=>{const r=e.getBoundingClientRect();return{id:e.id||e.textContent.trim().slice(0,30),w:r.width,h:r.height}})})`);
   assert.ok(geometry.scroll<=geometry.width+1,JSON.stringify(geometry));assert.equal(geometry.font,'30px');assert.ok(geometry.targets.every(t=>t.w>=44&&t.h>=44),JSON.stringify(geometry.targets));
   await capture(`enlarged-${theme}-390`);await c.eval('document.documentElement.style.fontSize=""');await click('[data-connection="1"] [data-editor] > summary');
  }
  cases.push('draft rejection and held committed-write/read-failure recovery preserve identity; blocked policies and enlarged phone controls');
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
  fs.writeFileSync(path.join(root,'evidence.json'),JSON.stringify({cases,evidence,external,errors,httpErrors,fixture:'canonical create_app + app.state.banking_service.transport MockTransport'},null,2));console.log(JSON.stringify({passed:cases.length,cases,artifacts:root}));
 }finally{if(hostedCdp)hostedCdp.close();if(c)c.close();await Promise.all([...interceptions]);chrome.kill('SIGTERM');server.kill('SIGTERM');await Promise.all([chrome,server].map(child=>child.exitCode!==null?Promise.resolve():new Promise(resolve=>{const timer=setTimeout(()=>{child.kill('SIGKILL');resolve();},3000);child.once('exit',()=>{clearTimeout(timer);resolve();});})));fs.writeFileSync(path.join(root,'server-errors.txt'),stderr);fs.writeFileSync(path.join(root,'interception-errors.json'),JSON.stringify(interceptionErrors,null,2));}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
