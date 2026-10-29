/* Generated-only, visible pointer/keyboard onboarding acceptance. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const pause = ms => new Promise(r => setTimeout(r, ms));
async function freePort() {const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
async function stopChild(child) {
  if(child.exitCode!==null||child.signalCode!==null)return;
  await new Promise((resolve,reject)=>{
    let timer;
    const onExit=()=>{clearTimeout(timer);resolve();};
    child.once('exit',onExit);
    timer=setTimeout(()=>{
      child.kill('SIGKILL');
      timer=setTimeout(()=>{child.removeListener('exit',onExit);reject(Error('Owned child did not exit'));},1500);
    },1500);
    child.kill('SIGTERM');
  });
}
class Cdp {
  constructor(url) {this.id=0;this.pending=new Map();this.listeners=[];this.ws=new WebSocket(url);this.ready=new Promise((r,j)=>{this.ws.onopen=r;this.ws.onerror=j;});this.ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id){const p=this.pending.get(m.id);if(!p)return;this.pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result);}else this.listeners.forEach(f=>f(m));};}
  async call(method,params={}) {await this.ready;const id=++this.id;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('CDP timeout '+method+' '+JSON.stringify(params)));},8000);this.pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});this.ws.send(JSON.stringify({id,method,params}));});}
  async eval(expression) {const r=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
  async until(expression) {for(let i=0;i<100;i++){if(await this.eval(expression))return;await pause(40);}throw Error(expression+' '+await this.eval('JSON.stringify({body:document.body.innerText.slice(0,2400),focus:document.activeElement.outerHTML.slice(0,300),keys:window.generatedKeys})'));}
  async pointer(selector) {
    // Keep native smooth scrolling. A layout box immediately after scrollIntoView
    // is the departure point, not a safe pointer destination.
    return this.eval(`new Promise((resolve,reject)=>{
      const selector=${JSON.stringify(selector)},n=document.querySelector(selector);
      const visible=()=>{
        if(!n?.isConnected||!n.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return false;
        for(let p=n.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS'&&!p.open&&!p.querySelector(':scope > summary')?.contains(n))return false;
        return true;
      };
      if(!visible()){reject(Error('Not visible: '+selector));return;}
      n.scrollIntoView({block:'center'});
      const start=performance.now();let changed=start,previous,rearmed=false;
      function frame(){
        if(document.querySelector(selector)!==n||!visible()){reject(Error('Pointer target changed or hidden: '+selector));return;}
        const r=n.getBoundingClientRect(),position=[r.x,r.y,r.width,r.height,scrollX,scrollY,innerWidth,innerHeight];
        for(let p=n.parentElement;p;p=p.parentElement)position.push(p.scrollLeft,p.scrollTop);
        const now=performance.now();
        if(now-start>=4000){reject(Error('Pointer target did not settle in viewport: '+selector));return;}
        if(!previous||position.some((v,i)=>Math.abs(v-previous[i])>.1))changed=now;
        previous=position;
        const inViewport=r.width>0&&r.height>0&&r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;
        if(now-changed>=120){
          if(inViewport){resolve({x:r.x+r.width/2,y:r.y+r.height/2});return;}
          // A competing native scroll can cancel the initial smooth request.
          // Offscreen quiescence is not arrival: re-arm once, never click/retry
          // or extend the original deadline. Persistent nonarrival still fails.
          if(!rearmed){rearmed=true;changed=now;n.scrollIntoView({block:'center'});}
        }
        requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    })`);
  }
  async verifyPointer(selector,point) {
    return this.eval(`(()=>{
      const selector=${JSON.stringify(selector)},n=document.querySelector(selector),p=${JSON.stringify(point)};
      if(!n?.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))throw Error('Not visible: '+selector);
      const r=n.getBoundingClientRect();
      if(r.width<=0||r.height<=0||r.left<0||r.top<0||r.right>innerWidth||r.bottom>innerHeight)throw Error('Pointer target outside viewport: '+selector+' '+JSON.stringify({point:p,rect:r.toJSON(),width:innerWidth,height:innerHeight}));
      if(Math.abs(p.x-(r.x+r.width/2))>.5||Math.abs(p.y-(r.y+r.height/2))>.5)throw Error('Wrong or moving pointer target: '+selector+' '+JSON.stringify({point:p,rect:r.toJSON(),focus:document.activeElement.outerHTML.slice(0,200)}));
      if(!n.contains(document.elementFromPoint(p.x,p.y)))throw Error('Pointer target covered: '+selector);
      return p;
    })()`);
  }
  async click(selector) {
    const p=await this.verifyPointer(selector,await this.pointer(selector));
    await this.call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...p});
    await this.call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...p});
  }
  // Printable Space needs the same native keyDown/text envelope as Enter;
  // rawKeyDown alone stalls macOS headless Chromium even on a bare checkbox.
  async key(key,code,vk,native=vk) {const text=key==='Enter'?'\r':key===' '?' ':null;await this.call('Input.dispatchKeyEvent',{type:text===null?'rawKeyDown':'keyDown',key,code,windowsVirtualKeyCode:vk,nativeVirtualKeyCode:native,...(text===null?{}:{text,unmodifiedText:text})});await this.call('Input.dispatchKeyEvent',{type:'keyUp',key,code,windowsVirtualKeyCode:vk,nativeVirtualKeyCode:native});}
  async type(selector,text) {await this.click(selector);await this.call('Input.insertText',{text});}
}
async function firstRun(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await c.eval("document.querySelector('#setup-details').open"),true,'first missing prerequisite is exposed');
  assert.equal(await c.eval("document.querySelector('#members-details').open"),false);
  assert.equal(await c.eval("document.querySelector('#sandbox-attempts').parentElement.hidden"),true,'no empty recovery notice');
  assert.equal(await c.eval("document.querySelector('#sandbox-connections').closest('section').hidden"),true,'no empty connection card before first add');
  assert.equal(await c.eval("document.querySelector('#sandbox-disable').hidden"),true,'do not offer Disable while disabled');
  await geometryChecks(c,'first-run');
  await c.click('#sandbox-file-consent');await c.click('#sandbox-file-setup button');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && document.querySelector('#members-details').open");
  assert.equal(await c.eval("document.activeElement.id"),'member-name','next action receives focus');
  assert.equal((await control()).counts.link||0,0);
  await control({fail:'member'});await c.type('#member-name','Generated Alpha');await c.click('#sandbox-member button');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval("document.querySelector('#member-name').value"),'Generated Alpha','failed member draft retained');
  assert.equal(await c.eval("document.activeElement.id"),'member-name');
  await control({fail:''});await c.click('#sandbox-member button');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && document.querySelector('#link-owner').options.length===1");
  assert.equal(await c.eval("document.querySelector('.sandbox-connect-panel').open"),true);
  assert.equal(await c.eval("document.activeElement.id"),'link-owner');
  assert.equal(await c.eval("document.querySelector('#sandbox-link').disabled"),true,'fresh consent required');
  assert.equal((await control()).counts.link||0,0);
  assert.equal(await c.eval("!!document.querySelector('script[src^=\"https://cdn.plaid.com\"]')"),false);
  await c.click('#link-consent');await c.click('#sandbox-link');
  await c.until("!!document.querySelector('#generated-exit')");
  assert.equal(await c.eval("[...document.querySelectorAll('.sandbox-page button,.sandbox-page input,.sandbox-page select')].filter(n=>n.id!=='sandbox-retry').every(n=>n.disabled)"),true,'actual controls locked during Link');
  await c.click('#generated-exit');await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval("document.querySelector('#link-consent').checked"),false);
  assert.equal(await c.eval("document.querySelector('#sandbox-link').disabled"),true);
  assert.equal(await c.eval("document.activeElement.id"),'link-consent');
  await c.click('#link-consent');await c.click('#sandbox-link');await c.until("!!document.querySelector('#generated-success')");await c.click('#generated-success');
  await c.until("document.querySelectorAll('.sandbox-connection').length===1 && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval("document.querySelector('#sandbox-message a')?.textContent"),'Review accounts');
  assert.equal(await c.eval("document.activeElement===document.querySelector('#sandbox-message a')"),true,'successful add focuses Review accounts');
  await c.click('#sandbox-message a');await c.until("location.pathname==='/accounts' && document.querySelectorAll('.sandbox-account').length===1");
  assert.equal(await c.eval("document.querySelector('.sandbox-account').innerText.includes('Unassigned')"),true,'connecting member does not infer mapping');
  assert.deepEqual((await control()).counts,{'enable-private-file':1,member:2,link:2,exchange:1,sync:1});
}
async function accountJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await c.eval("document.querySelectorAll('.sandbox-account form').length"),0,'mapping begins compact, not open forms');
  await geometryChecks(c,'accounts-compact');
  assert.deepEqual(await c.eval("[...document.querySelectorAll('.sandbox-account-group h2')].map(n=>n.textContent)"),['Connection 1 · Generated Alpha','Connection 2 · Generated Beta']);
  const a='.sandbox-account-group:nth-child(1) .sandbox-account', b='.sandbox-account-group:nth-child(2) .sandbox-account';
  await c.click(a+' [data-action=edit-account]');
  assert.equal(await c.eval("document.activeElement.tagName"),'SELECT');
  assert.equal(await c.eval(`document.querySelector('${a} button[type=submit]').disabled`),true,'unchanged mapping cannot save');
  await c.click(a+' input');await c.click(a+' [data-action=cancel-account]');
  assert.equal(await c.eval(`document.activeElement===document.querySelector('${a} [data-action=edit-account]')`),true,'Cancel returns focus');
  assert.deepEqual((await control()).counts,{});
  await c.key('Enter','Enter',13,36);await c.until(`!!document.querySelector('${a} form')`);
  await c.click(a+' input');
  await c.click(b+' [data-action=edit-account]');await c.click(b+' input');
  await c.eval(`(()=>{const owner=document.querySelector('${b} select');owner.value='shared';owner.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await geometryChecks(c,'accounts-editing');
  await control({fail:'mapping',hold:'mapping'});
  await c.click(a+' button[type=submit]');
  await c.until("fetch('/__fixture').then(r=>r.json()).then(s=>s.waiting==='mapping')");
  assert.equal(await c.eval("[...document.querySelectorAll('.sandbox-page button,.sandbox-page input,.sandbox-page select')].filter(n=>n.id!=='sandbox-retry').every(n=>n.disabled)"),true);
  await c.click(a+' button[type=submit]');
  assert.equal((await control()).counts.mapping,1,'repeated action never duplicates while request is held');
  await control({release:true});
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal((await control()).counts.mapping,1,'repeated action never duplicates');
  assert.equal(await c.eval(`document.querySelector('${a} input').checked`),false,'failed draft retained');
  assert.equal(await c.eval(`document.querySelector('${b} input').checked`),false,'unrelated draft retained');
  assert.equal(await c.eval(`document.querySelector('${b} select').value`),'shared','owner draft survives unrelated failure');
  assert.equal(await c.eval(`document.activeElement===document.querySelector('${a} button[type=submit]')`),true,'failed Save keeps editing focus');
  await control({fail:'',hold:'overview'});await c.click(a+' button[type=submit]');
  await c.until("fetch('/__fixture').then(r=>r.json()).then(s=>s.waiting==='overview')");
  assert.equal(await c.eval(`document.querySelector('${b} input').disabled`),true,'busy lock includes authoritative refresh');
  await control({release:true});
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval(`document.activeElement===document.querySelector('${a} [data-action=edit-account]')`),true,'Save returns focus after DOM replacement');
  assert.equal(await c.eval(`document.querySelector('${a} form')===null`),true);
  assert.equal(await c.eval(`document.querySelector('${b} input').checked`),false);
  const snapshot=await c.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.accounts.map(a=>[a.selected,a.ownership]))");
  assert.equal(Boolean(snapshot[0][0]),false);assert.equal(Boolean(snapshot[1][0]),true,'duplicate account IDs stay connection scoped');
  await control({fail_overview:true});await c.click(b+' button[type=submit]');
  await c.until("document.querySelector('.sandbox-page').dataset.loaded==='false'");
  assert.equal(await c.eval("[...document.querySelectorAll('.sandbox-page button,.sandbox-page input,.sandbox-page select')].filter(n=>n.id!=='sandbox-retry').every(n=>n.disabled)"),true,'refresh failure fails closed');
  await control({fail_overview:false});await c.click('#sandbox-retry');await c.until("document.querySelector('.sandbox-page').dataset.loaded==='true'");
  assert.equal((await control()).counts.mapping,3);
  assert.equal(await c.eval(`document.activeElement===document.querySelector('${b} [data-action=edit-account]')`),true,'refresh retry restores deferred Save focus');
  // Local mappings remain available with network access disabled.
  await c.call('Page.navigate',{url:base+'/connections'});await c.until("location.pathname==='/connections' && document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  await c.click('#link-heading');await c.click('#setup-heading');await c.click('#sandbox-disable');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && document.querySelector('#sandbox-status').textContent.includes('Disabled')");
  await c.call('Page.navigate',{url:base+'/accounts'});await c.until("location.pathname==='/accounts' && document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  await c.click(a+' [data-action=edit-account]');await c.click(a+' input');
  assert.equal(await c.eval(`document.querySelector('${a} button[type=submit]').disabled`),false);
  await c.click(a+' button[type=submit]');await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal((await control()).counts.mapping,4);
  const mappings=await c.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.accounts.map(a=>[a.selected,a.ownership]))");
  assert.equal(mappings[1][1],'shared','Account owner explicitly saved');
}
async function draftJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  const a='.sandbox-account-group:nth-child(1) .sandbox-account', b='.sandbox-account-group:nth-child(2) .sandbox-account';
  const saved=()=>c.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.accounts.map(a=>({selected:!!a.selected,ownership:a.ownership})))");
  const refreshThroughB=async()=>{
    await c.click(b+' [data-action=edit-account]');await c.click(b+' input');
    await control({hold:'overview'});await c.click(b+' button[type=submit]');
    await c.until("fetch('/__fixture').then(r=>r.json()).then(s=>s.waiting==='overview')");
    await control({release:true});await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  };
  await c.click(a+' [data-action=edit-account]');
  await control({external_mapping:{index:0,selected:false,ownership:'shared'}});
  await refreshThroughB();
  assert.deepEqual(await c.eval(`({selected:document.querySelector('${a} input').checked,ownership:document.querySelector('${a} select').value,disabled:document.querySelector('${a} button[type=submit]').disabled})`),{selected:false,ownership:'shared',disabled:true},'pristine open editor rebases without inventing user changes');
  await c.click(a+' [data-action=cancel-account]');
  assert.deepEqual((await saved())[0],{selected:false,ownership:'shared'},'Cancel does not overwrite externally saved mapping');
  await c.click(a+' [data-action=edit-account]');await c.click(a+' input');
  await control({external_mapping:{index:0,ownership:'unassigned'}});await refreshThroughB();
  assert.deepEqual(await c.eval(`({selected:document.querySelector('${a} input').checked,ownership:document.querySelector('${a} select').value})`),{selected:true,ownership:'unassigned'},'only actually changed inclusion survives fresh owner mapping');
  await c.click(a+' button[type=submit]');await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.deepEqual((await saved())[0],{selected:true,ownership:'unassigned'},'Save merges dirty inclusion with external owner');
  await c.click(a+' [data-action=edit-account]');
  await c.eval(`(()=>{const n=document.querySelector('${a} select');n.value='shared';n.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  // Touched then restored is pristine, not a dirty field.
  await c.click(a+' input');await c.click(a+' input');
  await control({external_mapping:{index:0,selected:false}});await refreshThroughB();
  assert.equal(await c.eval(`document.querySelector('${a} input').checked`),false,'restored inclusion rebases while owner remains dirty');
  await c.click(a+' button[type=submit]');await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.deepEqual((await saved())[0],{selected:false,ownership:'shared'},'Save merges dirty owner with external inclusion');
  assert.deepEqual((await control()).counts,{mapping:5});
}
async function identityJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  const tree=await c.call('Accessibility.getFullAXTree');
  const names=tree.nodes.filter(n=>!n.ignored&&n.role?.value==='button'&&n.name?.value.startsWith('Edit ')).map(n=>n.name.value);
  assert.deepEqual(names,[
    'Edit Example Checking · Ending 0000 · Account 1 · Connection 1',
    'Edit Example Checking · Ending 1111 · Connection 1',
    'Edit Example Checking · Account 3 · Connection 1',
    'Edit Example Checking · Account 4 · Connection 1',
    'Edit Example Checking · Ending 0000 · Account 5 · Connection 1',
    'Edit Example Checking · Ending 0000 · Connection 2'
  ],'same-name Edit controls expose suffix or stable ordinal in the accessibility tree');
  assert.deepEqual((await control()).counts,{});
}
async function keyboardJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  const a='.sandbox-account-group:nth-child(1) .sandbox-account';
  await c.eval("window.generatedKeys=[];for(const type of ['keydown','keyup','focusin'])document.addEventListener(type,e=>{window.generatedKeys.push({type,key:e.key,code:e.code,target:e.target.tagName,text:e.target.textContent.slice(0,60)});if(window.generatedKeys.length>40)window.generatedKeys.shift();},true)");
  await c.click(a+' [data-action=edit-account]');await c.key('Tab','Tab',9,48);await c.key('Tab','Tab',9,48);
  await assertVisibleFocus(c,a+' [data-action=cancel-account]','Tab skips unchanged disabled Save');await c.key('Enter','Enter',13,36);
  for(let i=0;i<10;i++){
    await assertVisibleFocus(c,a+' [data-action=edit-account]','keyboard Cancel returns to Edit '+i);
    await c.key('Enter','Enter',13,36);await c.until(`!!document.querySelector('${a} form')`);
    await assertVisibleFocus(c,a+' select','Enter opens editor and focuses native select '+i);
    await c.key('Tab','Tab',9,48);await assertVisibleFocus(c,a+' input','Tab leaves native select for inclusion '+i);
    await c.key(' ','Space',32,49);await c.until(`!document.querySelector('${a} button[type=submit]').disabled`);
    await c.key('Tab','Tab',9,48);await assertVisibleFocus(c,a+' button[type=submit]','Tab reaches dirty Save '+i);
    await c.key('Tab','Tab',9,48);await assertVisibleFocus(c,a+' [data-action=cancel-account]','Tab reaches Cancel '+i);
    await c.key('Enter','Enter',13,36);await c.until(`!document.querySelector('${a} form')`);
  }
  await assertVisibleFocus(c,a+' [data-action=edit-account]','final keyboard Cancel returns visible focus');
  assert.deepEqual((await control()).counts,{},'keyboard cancellation never writes');
}
async function emptyJourney(c,base,control,mode) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  const id=await c.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.connections[0].id)");
  const check=async()=>{
    const text=await c.eval("document.querySelector('#sandbox-accounts').innerText");
    assert.equal(/Sync/.test(text),mode==='empty-healthy','empty-state Sync advice matches current eligibility: '+mode+' '+text);
    if(mode==='empty-login')assert.match(text,/repair/i);
    if(mode==='empty-disabled')assert.match(text,/network access.*disabled/i);
    if(mode==='empty-disconnected')assert.match(text,/disconnected/i);
    if(mode==='empty-unconfirmed')assert.match(text,/revocation.*unconfirmed/i);
    assert.equal(await c.eval("document.querySelector('#sandbox-accounts a[href=\"/connections\"]')?.textContent"),'Manage connections');
  };
  await c.call('Page.navigate',{url:base+'/accounts?connection_id='+encodeURIComponent(id)});await c.until("location.search!=='' && document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await c.eval("document.querySelectorAll('.sandbox-account').length"),0,'scope does not show another connection accounts');await check();
  await control({clear_accounts:true});await c.call('Page.navigate',{url:base+'/accounts'});await c.until("location.search==='' && document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  await check();assert.deepEqual((await control()).counts,{});
}
async function lifecycleJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.deepEqual(await c.eval("[...document.querySelectorAll('.sandbox-connection h3')].map(n=>n.textContent)"),['Connection 1 · Generated Alpha','Connection 2 · Generated Beta']);
  assert.equal(await c.eval("document.querySelector('.sandbox-connection [data-action=sync]').disabled"),true,'login repair takes priority over Sync');
  assert.equal(await c.eval("document.querySelector('.sandbox-connection [data-repair-consent]').getClientRects().length>0"),true,'login repair is not buried');
  assert.equal(await c.eval("document.querySelector('.sandbox-connection [data-action=repair]').disabled"),true,'repair needs fresh consent');
  assert.equal(await c.eval("document.querySelectorAll('.sandbox-connection')[1].querySelector('[data-action=sync]').disabled"),false,'generic sync failure is not login repair');
  assert.equal(await c.eval("document.querySelector('.sandbox-connection a').textContent"),'View accounts');
  assert.equal(await c.eval("document.querySelector('#link-heading').compareDocumentPosition(document.querySelector('.sandbox-connection')) & Node.DOCUMENT_POSITION_FOLLOWING"),4);
  assert.equal(await c.eval("document.querySelector('.sandbox-connect-panel').open"),false,'populated screen has one compact Add bank entry');
  await geometryChecks(c,'connections-populated');
  await c.click('#link-heading');await c.click('#link-consent');
  assert.equal(await c.eval("document.querySelector('#sandbox-link').disabled"),false,'unaffected owner can link');
  // Headless macOS does not reliably drive native select popups. Only select
  // values use DOM change; all submissions/disclosures remain pointer/keyboard.
  await c.eval("const owner=document.querySelector('#link-owner');owner.selectedIndex=1;owner.dispatchEvent(new Event('change',{bubbles:true}))");
  assert.equal(await c.eval("document.querySelector('#link-consent').checked"),false);
  await c.click('#link-consent');assert.equal(await c.eval("document.querySelector('#sandbox-link').disabled"),true,'only uncertain owner blocked');
  await c.click('#sandbox-attempts input');await c.click('#sandbox-attempts [data-action=acknowledge-exchange]');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval("document.querySelector('#link-owner').selectedIndex"),1,'selected owner survives recovery refresh');
  assert.equal(await c.eval("document.querySelector('#link-consent').checked"),false);
  assert.equal(await c.eval("document.querySelector('#sandbox-link').disabled"),true);
  assert.equal(await c.eval("document.activeElement.id"),'link-consent','recovery returns to fresh consent without opening Link');
  assert.deepEqual((await control()).counts,{'acknowledge-exchange':1});
  assert.equal(await c.eval("!!document.querySelector('script[src^=\"https://cdn.plaid.com\"]')"),false,'acknowledgement never loads Link');
  await c.click('.sandbox-connection [data-repair-consent]');await c.click('.sandbox-connection [data-action=repair]');
  await c.until("!!document.querySelector('#generated-exit')");await c.click('#generated-exit');await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval("document.querySelector('.sandbox-connection [data-repair-consent]').checked"),false);
  assert.equal(await c.eval("document.querySelector('.sandbox-connection [data-action=repair]').disabled"),true);
  assert.equal(await c.eval("document.querySelectorAll('.sandbox-connection').length"),2);
  await c.click('#link-consent');await control({fail:'sync'});await c.click('#sandbox-link');await c.until("!!document.querySelector('#generated-success')");await c.click('#generated-success');
  await c.until("document.querySelectorAll('.sandbox-connection').length===3 && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval("document.querySelector('#sandbox-message').textContent.includes('do not relink')"),true);
  assert.equal(await c.eval("document.querySelector('#sandbox-message a').textContent"),'Review accounts');
  assert.deepEqual((await control()).counts,{'acknowledge-exchange':1,link:2,exchange:1,sync:1});
  const target=await c.eval("document.querySelectorAll('.sandbox-connection')[2].querySelector('a').href");
  await c.call('Page.navigate',{url:target});await c.until("location.pathname==='/accounts' && document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await c.eval("document.querySelectorAll('.sandbox-account-group').length"),1,'View accounts lands on the chosen connection');
}
async function assertVisibleFocus(c,selector,label) {
  assert.equal(await c.eval(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});if(!n||document.activeElement!==n||n.disabled||!n.getClientRects().length)return false;for(let p=n.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS'&&!p.open)return false;return getComputedStyle(n).visibility!=='hidden';})()`),true,label+' (active: '+await c.eval("document.activeElement.outerHTML.slice(0,400)")+')');
}
async function repairFocusJourney(c,base,control,complete) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  const connection='.sandbox-connection:first-child';
  if(!complete){
    await c.click(connection+' > details > summary');await c.click(connection+' details details > summary');
  }
  await c.click(connection+' [data-repair-consent]');await c.click(connection+' [data-action=repair]');
  await c.until("!!document.querySelector('#generated-exit')");await c.click(complete?'#generated-success':'#generated-exit');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  await assertVisibleFocus(c,connection+' [data-repair-consent]',complete?'successful login repair focuses visible fresh consent after becoming healthy':'healthy nested repair exit opens every ancestor before focus');
  assert.equal(await c.eval(`document.querySelector('${connection} [data-repair-consent]').checked`),false);
  const status=await c.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.connections[0])");
  assert.equal(status.status,'connected');assert.equal(status.error,null);
  assert.deepEqual((await control()).counts,complete?{link:1,'repair-complete':1}:{link:1});
}
async function syncFocusJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  const first='.sandbox-connection:first-child',second='.sandbox-connection:nth-child(2)';
  const sync=async(connection,outcome)=>{
    const before=(await control()).counts.sync||0;
    await control({sync_state:outcome,hold:'sync'});await c.click(connection+' [data-action=sync]');
    await c.until("fetch('/__fixture').then(r=>r.json()).then(s=>s.waiting==='sync')");
    assert.equal((await control()).counts.sync,before+1);await control({release:true});
    await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  };
  await sync(first,'ITEM_LOGIN_REQUIRED');
  await assertVisibleFocus(c,first+' [data-repair-consent]','Sync becoming login-required focuses eligible repair consent');
  assert.equal(await c.eval(`document.querySelector('${first} [data-action=sync]').disabled`),true);
  assert.equal(await c.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.connections[0].error)"),'ITEM_LOGIN_REQUIRED');
  await sync(second,'INVALID_UPDATE_WINDOW');await assertVisibleFocus(c,second+' [data-action=sync]','generic failure returns to eligible Sync');
  await sync(second,'disconnected');await assertVisibleFocus(c,second+' a','disconnected Sync returns to visible local accounts link');
  // The first connection still has eligible repair but no Sync; recover it first.
  await c.click(first+' [data-repair-consent]');await c.click(first+' [data-action=repair]');await c.until("!!document.querySelector('#generated-success')");await c.click('#generated-success');await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  await sync(first,'disabled');await assertVisibleFocus(c,first+' a','network-disabled Sync returns to visible local accounts link');
  assert.equal(await c.eval("fetch('/api/sandbox/overview').then(r=>r.json()).then(s=>s.enabled)"),false);
  assert.deepEqual((await control()).counts,{sync:4,link:1,'repair-complete':1});
}
async function manualJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await c.eval("document.querySelector('#setup-details').open"),true,'configured but disabled manual setup still needs credentials');
  assert.equal(await c.eval("document.querySelector('#sandbox-link-form').hidden"),true);
  assert.equal(await c.eval("document.querySelector('#sandbox-client').type"),'password');
  await c.type('#sandbox-client','generated-client');await c.type('#sandbox-secret','generated-secret');await c.click('#sandbox-enable-consent');await c.click('#sandbox-setup button');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && document.querySelector('#members-details').open");
  assert.deepEqual((await control()).counts,{setup:1});
  assert.equal(await c.eval("document.querySelector('#sandbox-secret').value"),'');
}
async function exchangeJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  await c.click('#sandbox-file-consent');await c.click('#sandbox-file-setup button');
  await c.until("document.querySelector('#members-details').open && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  await c.type('#member-name','Generated Alpha');await c.click('#sandbox-member button');
  await c.until("document.querySelector('#link-owner').options.length===1 && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  await control({fail_discover:true});await c.click('#link-consent');await c.click('#sandbox-link');await c.until("!!document.querySelector('#generated-success')");await c.click('#generated-success');
  await c.until("document.querySelectorAll('.sandbox-connection').length===1 && !document.querySelector('.sandbox-page').hasAttribute('aria-busy')");
  assert.equal(await c.eval("document.querySelector('#sandbox-message').textContent.includes('Do not relink')"),true,'exchange failure may already have committed a connection');
  assert.equal(await c.eval("document.querySelector('.sandbox-connection [data-action=sync]').disabled"),false,'committed connection offers Sync, not blind relink');
  await c.click('.sandbox-connection a');await c.until("location.pathname==='/accounts' && document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await c.eval("document.body.innerText.includes('Connect a test bank to begin.')"),false,'missing discovery does not recommend relinking');
  assert.deepEqual((await control()).counts,{'enable-private-file':1,member:1,link:1,exchange:1});
}
async function revocationJourney(c,base,control) {
  await c.until("document.querySelector('.sandbox-page')?.dataset.loaded==='true'");
  assert.equal(await c.eval("document.querySelector('#sandbox-connections').innerText.includes('Try Sync')"),false,'revocation is not a sync failure');
  assert.equal(await c.eval("document.querySelector('.sandbox-connection > p').textContent.startsWith('Revocation uncertain')"),true);
  assert.equal(await c.eval("document.querySelectorAll('.sandbox-connection')[1].querySelector('p').textContent.startsWith('Access invalid')"),true);
  assert.equal(await c.eval("[...document.querySelectorAll('[data-action=sync],[data-action=repair]')].every(n=>n.disabled)"),true);
  c.listeners.push(m=>{if(m.method==='Page.javascriptDialogOpening')c.call('Page.handleJavaScriptDialog',{accept:true});});
  await c.click('.sandbox-connection:first-child [data-action=disconnect]');
  await c.until("!document.querySelector('.sandbox-page').hasAttribute('aria-busy') && document.querySelector('.sandbox-connection > p').textContent.startsWith('Disconnected')");
  assert.deepEqual((await control()).counts,{disconnect:1});
}
async function geometryChecks(c,label) {
  for(const theme of ['dark','light']) {
    await c.eval(`(()=>{const appearance=document.querySelector('[data-theme-select]');appearance.value=${JSON.stringify(theme)};appearance.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    for(const width of [320,390,768,1440]) {
      await c.call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:width<500});
      assert.equal(await c.eval('document.documentElement.scrollWidth<=innerWidth'),true,`${label}/${theme}/${width} overflow`);
      assert.deepEqual(await c.eval("[...document.querySelectorAll('.sandbox-page button,.sandbox-page a,.sandbox-page summary,.sandbox-page input:not([type=checkbox]),.sandbox-page select,.sandbox-check')].filter(n=>n.getClientRects().length&&getComputedStyle(n).visibility!=='hidden').filter(n=>{const r=n.getBoundingClientRect();return r.height<43.9||r.width<43.9}).map(n=>n.textContent)"),[],`${label}/${theme}/${width} targets`);
      const shot=await c.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
      if(process.env.ONBOARDING_ARTIFACTS){fs.mkdirSync(process.env.ONBOARDING_ARTIFACTS,{recursive:true});fs.writeFileSync(path.join(process.env.ONBOARDING_ARTIFACTS,`${label}-${theme}-${width}.png`),Buffer.from(shot.data,'base64'));}
    }
  }
  await c.call('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:1,mobile:false});
}
async function main() {
  const mode=process.argv[2]||'first';const scenario=mode==='first'?'first':mode;
  const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'fn-onboarding-')));
  const port=await freePort(),debug=await freePort(),base=`http://127.0.0.1:${port}`;
  const server=spawn(path.resolve('.venv/bin/python'),['-c','import sys; from pathlib import Path; import uvicorn; from tests.browser.plaid_onboarding_fixture import onboarding_app; uvicorn.run(onboarding_app(Path(sys.argv[1]),int(sys.argv[2]),sys.argv[3]),host="127.0.0.1",port=int(sys.argv[2]),log_level="critical")',root,String(port),scenario],{env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root},stdio:['ignore','ignore','inherit']});
  const chrome=spawn(process.env.CHROMIUM_BIN||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking',`--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'],{stdio:'ignore'});
  let c;const external=[],errors=[];
  const control=async fields=>{const response=await fetch(base+'/__fixture',fields?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(fields)}:{});return response.json();};
  try {
    let targets;for(let i=0;i<100;i++){try{targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();if((await fetch(base+'/healthz')).ok)break;}catch{}await pause(100);}assert.ok(targets);c=new Cdp(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
    await c.call('Page.enable');await c.call('Runtime.enable');await c.call('Network.enable');
    const stub="window.Plaid={create(o){let box;return {open(){box=document.createElement('div');box.style='position:fixed;inset:0;z-index:9999;background:white;color:black;padding:40px';for(const [id,label,fn] of [['generated-success','Generated complete',()=>{const token='public-sandbox-callback-'+o.token.split('-').at(-1);o.onSuccess(token);o.onSuccess(token);o.onExit();}],['generated-exit','Generated exit',()=>o.onExit()]]){const b=document.createElement('button');b.id=id;b.textContent=label;b.onclick=fn;box.append(b);}document.body.append(box);},destroy(){box.remove();}}}}";
    c.listeners.push(m=>{if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);if(m.method==='Network.requestWillBeSent'&&/^https?:/.test(m.params.request.url)&&!m.params.request.url.startsWith(base))external.push(m.params.request.url);if(m.method==='Fetch.requestPaused'){assert.equal(m.params.request.url,'https://cdn.plaid.com/link/v2/stable/link-initialize.js');c.call('Fetch.fulfillRequest',{requestId:m.params.requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:'application/javascript'}],body:Buffer.from(stub).toString('base64')});}});
    await c.call('Fetch.enable',{patterns:[{urlPattern:'https://*',requestStage:'Request'}]});
    await c.call('Page.navigate',{url:base+(['accounts','drafts','identity','keyboard'].includes(mode)?'/accounts':'/connections')});
    if(mode==='first')await firstRun(c,base,control);
    if(mode==='accounts')await accountJourney(c,base,control);
    if(mode==='drafts')await draftJourney(c,base,control);
    if(mode==='identity')await identityJourney(c,base,control);
    if(mode==='keyboard')await keyboardJourney(c,base,control);
    if(mode.startsWith('empty-'))await emptyJourney(c,base,control,mode);
    if(mode==='lifecycle')await lifecycleJourney(c,base,control);
    if(mode==='repair-exit'||mode==='repair-success')await repairFocusJourney(c,base,control,mode==='repair-success');
    if(mode==='sync-focus')await syncFocusJourney(c,base,control);
    if(mode==='manual')await manualJourney(c,base,control);
    if(mode==='exchange')await exchangeJourney(c,base,control);
    if(mode==='revocation')await revocationJourney(c,base,control);
    assert.deepEqual(await c.eval("(async()=>({local:localStorage.length,session:sessionStorage.length,caches:(await caches.keys()).length,workers:(await navigator.serviceWorker.getRegistrations()).length}))()"),{local:0,session:0,caches:0,workers:0});
    assert.deepEqual(errors,[]);assert.ok(external.every(url=>url==='https://cdn.plaid.com/link/v2/stable/link-initialize.js'));
    console.log(JSON.stringify({mode,root,external,errors,counts:(await control()).counts}));
  } finally {if(c)c.ws.close();await Promise.all([stopChild(chrome),stopChild(server)]);}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
