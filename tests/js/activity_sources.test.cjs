/* FNDS-01 generated-only native disclosure and real HTMX append acceptance. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
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
    this.ready = new Promise((resolve, reject) => {this.ws.onopen=resolve; this.ws.onerror=reject;});
    this.ws.onmessage = event => {
      const message=JSON.parse(event.data), pending=this.pending.get(message.id);
      if(pending) {
        this.pending.delete(message.id); clearTimeout(pending.timer);
        message.error ? pending.reject(Error(message.error.message)) : pending.resolve(message.result);
      } else if(!message.id) for(const listener of this.listeners) listener(message);
    };
  }
  async call(method, params={}) {
    await this.ready; const id=++this.id;
    return new Promise((resolve,reject) => {
      const timer=setTimeout(()=>{this.pending.delete(id);reject(Error('CDP deadline: '+method));},15000);
      this.pending.set(id,{resolve,reject,timer}); this.ws.send(JSON.stringify({id,method,params}));
    });
  }
  async eval(expression) {
    const result=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(result.exceptionDetails)throw Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async until(expression) {
    for(let i=0;i<160;i++){if(await this.eval(expression))return;await pause(50);}
    throw Error('Condition not met: '+expression);
  }
}
const fixture = `import sys, os, socket, ipaddress, hashlib
from pathlib import Path
import uvicorn
from app.config import Settings, get_settings
Settings.model_config['env_file']=None
root=Path(sys.argv[1]); root.mkdir(parents=True)
os.environ.update(DB_PATH=str(root/'generated.sqlite'),DATA_DIR=str(root),STRICT_LOCAL_MODE='true',PLAID_ENABLED='false',READ_ONLY='false')
get_settings.cache_clear()
from app.db import engine, migrate
from tests.test_activity_sources import _linked_pair, _document
migrate.seed_sample(str(root/'generated.sqlite'))
with engine.write_tx(root/'generated.sqlite') as conn:
    receipt, statement, line = _linked_pair(conn, root)
    only = _document(conn,root,'receipt','Generated one receipt.txt','Generated single original')
    conn.execute('UPDATE transactions SET source_document_id=? WHERE id=6',(only,))
    for index in range(55):
        txn=conn.execute(\"INSERT INTO transactions(account_id,posted_on,description,amount_cents,source,flow_kind,external_id) VALUES (1,'2026-05-01','Generated cursor row',-123,'manual','purchase',?)\",(f'generated-browser-{index}',)).lastrowid
        conn.execute('INSERT INTO transaction_splits(transaction_id,category_id,amount_cents) VALUES (?,4,-123)',(txn,))
from app.web.app import create_app
app=create_app()
@app.get('/__fixture')
def manifest():
    return dict(receipt=receipt,statement=statement,line=line,only=only)
@app.get('/__proof')
def proof():
    with engine.read_conn(root/'generated.sqlite') as conn:
        dump='\\n'.join(conn.iterdump())
        return dict(count=conn.execute('SELECT count(*) FROM transactions').fetchone()[0],
          splits=conn.execute('SELECT sum(amount_cents) FROM transaction_splits WHERE transaction_id=3').fetchone()[0],
          matched=conn.execute('SELECT matched_transaction_id FROM statement_lines WHERE id=?',(line,)).fetchone()[0],
          dump=hashlib.sha256(dump.encode()).hexdigest())
original_connect=socket.socket.connect
def local_connect(sock,address):
    if sock.family in (socket.AF_INET,socket.AF_INET6):
        try: allowed=ipaddress.ip_address(address[0]).is_loopback
        except ValueError: allowed=address[0]=='localhost'
        if not allowed: raise RuntimeError('External connection forbidden in generated source proof')
    return original_connect(sock,address)
socket.socket.connect=local_connect
uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[2]),lifespan='off',log_level='warning',access_log=False)
`;
async function main() {
  assert.ok(process.env.TMPDIR,'Supply a task-owned TMPDIR');
  const root=fs.mkdtempSync(path.join(process.env.TMPDIR,'activity-sources-'));
  const artifacts=process.env.ACTIVITY_SOURCES_ARTIFACTS || path.join(root,'artifacts');
  fs.mkdirSync(artifacts,{recursive:true});
  const port=await freePort(), debug=await freePort(), base=`http://127.0.0.1:${port}`;
  const server=spawn(path.resolve('.venv/bin/python'),['-c',fixture,path.join(root,'data'),String(port)],{
    env:{PATH:process.env.PATH,PYTHONPATH:process.cwd(),TMPDIR:root,PYTHONDONTWRITEBYTECODE:'1'},stdio:['ignore','ignore','pipe']});
  let serverErrors=''; server.stderr.on('data',data=>serverErrors+=data);
  const chrome=spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking',
    `--remote-debugging-port=${debug}`,`--user-data-dir=${path.join(root,'chrome')}`,'about:blank'],{stdio:'ignore'});
  let cdp; const cases=[], errors=[], external=[], cursorReads=[], originalNavigations=[];
  try {
    let targets, manifest;
    for(let i=0;i<180;i++) {
      try {
        targets=await(await fetch(`http://127.0.0.1:${debug}/json/list`)).json();
        const response=await fetch(base+'/__fixture'); if(response.ok){manifest=await response.json();break;}
      } catch {}
      if(server.exitCode!==null)throw Error(serverErrors);
      await pause(100);
    }
    assert.ok(manifest,serverErrors);
    cdp=new Cdp(targets.find(target=>target.type==='page').webSocketDebuggerUrl);
    await cdp.call('Page.enable'); await cdp.call('Runtime.enable'); await cdp.call('Network.enable');
    await cdp.call('Network.setBypassServiceWorker',{bypass:true});
    cdp.listeners.push(message=>{
      if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);
      if(message.method==='Fetch.requestPaused') {
        const {requestId,request}=message.params;
        if(/^https?:/.test(request.url)&&!request.url.startsWith(base+'/')) {
          external.push(request.url); cdp.call('Fetch.failRequest',{requestId,errorReason:'BlockedByClient'});
        } else {
          if(request.url.includes('/transactions?cursor='))cursorReads.push(request.url);
          cdp.call('Fetch.continueRequest',{requestId});
        }
      }
    });
    await cdp.call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    async function visit(route) {
      await cdp.call('Page.navigate',{url:base+route});
      await cdp.until(`location.pathname===${JSON.stringify(route.split('?')[0])}&&location.search===${JSON.stringify(route.includes('?')?'?'+route.split('?')[1]:'')}&&document.readyState==='complete'`);
    }
    async function key(name) {
      const values=name==='Enter'?{key:'Enter',code:'Enter',windowsVirtualKeyCode:13,nativeVirtualKeyCode:36,text:'\r',unmodifiedText:'\r'}:
        name==='Space'?{key:' ',code:'Space',windowsVirtualKeyCode:32,nativeVirtualKeyCode:49,text:' ',unmodifiedText:' '}:
        {key:'Tab',code:'Tab',windowsVirtualKeyCode:9,nativeVirtualKeyCode:48};
      await cdp.call('Input.dispatchKeyEvent',{type:'keyDown',...values});
      await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',...values,text:'',unmodifiedText:''});
    }
    async function open(id) {
      const selector=`#transaction-${id}`;
      assert.equal(await cdp.eval(`document.querySelector(${JSON.stringify(selector)}).open`),false);
      assert.equal(await cdp.eval(`document.querySelector(${JSON.stringify(selector+' .tx-originals')}).checkVisibility({contentVisibilityAuto:true})`),false);
      await cdp.eval(`document.querySelector(${JSON.stringify(selector+' summary')}).focus()`);
      await key('Enter'); await cdp.until(`document.querySelector(${JSON.stringify(selector)}).open`);
    }
    async function screenshot(name) {
      const image=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      fs.writeFileSync(path.join(artifacts,name+'.png'),Buffer.from(image.data,'base64'));
    }
    const initial=await(await fetch(base+'/__proof')).json();
    await visit('/recon?month=2026-01');
    const button=`form[action="/recon/line/${manifest.line}/confirm"] button`;
    await cdp.until(`!!document.querySelector(${JSON.stringify(button)})`);
    await cdp.eval(`document.querySelector(${JSON.stringify(button)}).focus()`); await key('Enter');
    await cdp.until("document.readyState==='complete'&&!document.querySelector('form[action$=\"/confirm\"]')");
    const matched=await(await fetch(base+'/__proof')).json();
    assert.equal(matched.matched,3); assert.equal(matched.count,initial.count); assert.equal(matched.splits,-16243);
    const multiRoutes=['/activity?date_from=2026-01-01&date_to=2026-01-31','/?month=2026-01'];
    for(const theme of ['light','dark']) {
      await cdp.call('Network.setCookie',{url:base,name:'finn_nancy_theme',value:theme,path:'/'});
      for(const width of [1440,390]) {
        await cdp.call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
        for(const route of multiRoutes) {
          await visit(route);
          assert.equal(await cdp.eval('document.documentElement.dataset.theme'),theme);
          // Zero and one originals are real rendered states, not a source-string assertion.
          await open(5);
          assert.equal(await cdp.eval("document.querySelector('#transaction-5 .tx-originals').textContent.includes('No linked originals.')"),true);
          assert.equal(await cdp.eval("document.querySelectorAll('#transaction-5 .tx-originals a').length"),0);
          await key('Space'); await cdp.until("!document.querySelector('#transaction-5').open");
          await open(6); await key('Tab');
          assert.equal(await cdp.eval("document.activeElement.getAttribute('href')"),`/review/doc/${manifest.only}/file`);
          await key('Enter'); await cdp.until(`location.pathname==='/review/doc/${manifest.only}/file'&&document.readyState==='complete'`);
          assert.ok((await cdp.eval('document.body.textContent')).includes('Generated single original'));
          originalNavigations.push({route,theme,width,doc:manifest.only});
          // Enter the native summary, Tab to each source, then activate its real file GET.
          for(const [index,doc] of [manifest.receipt,manifest.statement].entries()) {
            await visit(route); await open(3);
            const links=await cdp.eval("[...document.querySelectorAll('#transaction-3 .tx-originals a')].map(n=>({href:n.getAttribute('href'),text:n.textContent}))");
            assert.deepEqual(links.map(n=>n.href),[`/review/doc/${manifest.receipt}/file`,`/review/doc/${manifest.statement}/file`]);
            assert.ok(links[0].text.includes('Receipt: Generated linked receipt.txt'));
            assert.ok(links[1].text.includes('Statement: Generated linked statement.csv'));
            for(let tab=0;tab<=index;tab++)await key('Tab');
            assert.equal(await cdp.eval("document.activeElement.getAttribute('href')"),`/review/doc/${doc}/file`);
            await cdp.until("(()=>{const r=document.activeElement.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;})()");
            if(index===0) {
              // Capture the whole evidence block after native focus navigation is proven.
              await cdp.eval("document.querySelector('#transaction-3 .tx-originals').scrollIntoView({block:'center',behavior:'instant'})");
              await cdp.until("[...document.querySelectorAll('#transaction-3 .tx-originals a')].every(n=>{const r=n.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;})");
              assert.equal(await cdp.eval('document.documentElement.scrollWidth>innerWidth+1'),false);
              const geometry=await cdp.eval("[...document.querySelectorAll('#transaction-3 .tx-originals a')].map(n=>{const r=n.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,height:r.height};})");
              assert.ok(geometry.every(r=>r.left>=0&&r.right<=width+1&&r.height>=48),JSON.stringify(geometry));
              await screenshot(`${route.startsWith('/activity')?'activity':'home'}-${theme}-${width}`);
              cases.push({route,theme,width,originals:links,zero:0,one:1,geometry,canvas:await cdp.eval('getComputedStyle(document.body).backgroundColor')});
            }
            await key('Enter'); await cdp.until(`location.pathname==='/review/doc/${doc}/file'&&document.readyState==='complete'`);
            assert.ok((await cdp.eval('document.body.textContent')).includes(doc===manifest.receipt?'Generated receipt original':'Generated statement original'));
            originalNavigations.push({route,theme,width,doc});
          }
        }
      }
    }
    // Actual HTMX cursor append; retained native disclosures and delegated row behavior.
    await visit('/activity');
    const oldIds=await cdp.eval("[...document.querySelectorAll('.tx-row')].map(n=>n.id)");
    assert.equal(oldIds.length,50); assert.ok(!oldIds.includes('transaction-3'));
    await open(oldIds[0].replace('transaction-',''));
    await cdp.eval("document.querySelector('.load-more').focus()"); await key('Enter');
    await cdp.until("!!document.getElementById('transaction-3')&&!document.querySelector('.htmx-request,.htmx-settling,.htmx-swapping')");
    assert.equal(await cdp.eval(`document.getElementById(${JSON.stringify(oldIds[0])}).open`),true);
    const ids=await cdp.eval("[...document.querySelectorAll('.tx-row')].map(n=>n.id)");
    assert.equal(new Set(ids).size,ids.length); assert.equal(ids.length,matched.count); assert.equal(cursorReads.length,1);
    await open(3); await key('Tab');
    assert.equal(await cdp.eval('document.activeElement.getAttribute("href")'),`/review/doc/${manifest.receipt}/file`);
    await key('Tab'); assert.equal(await cdp.eval('document.activeElement.getAttribute("href")'),`/review/doc/${manifest.statement}/file`);
    await cdp.until("(()=>{const r=document.activeElement.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;})()");
    await screenshot('htmx-appended-dark-390');
    await key('Enter'); await cdp.until(`location.pathname==='/review/doc/${manifest.statement}/file'&&document.readyState==='complete'`);
    assert.ok((await cdp.eval('document.body.textContent')).includes('Generated statement original'));
    originalNavigations.push({route:'HTMX append',doc:manifest.statement});
    const after=await(await fetch(base+'/__proof')).json(); assert.deepEqual(after,matched);
    assert.deepEqual(errors,[]); assert.deepEqual(external,[]);
    fs.writeFileSync(path.join(artifacts,'browser-report.json'),JSON.stringify({passed:true,cases,cursorReads,originalNavigations,initial,matched,after,errors,external,ownedRoot:root},null,2));
    console.log(JSON.stringify({passed:true,renderCases:cases.length,sourceStates:[0,1,2],nativeOriginalNavigations:originalNavigations.length,actualCursorAppends:cursorReads.length,fullDumpEqual:true,artifacts}));
  } catch(error) {
    fs.writeFileSync(path.join(artifacts,'browser-failure.json'),JSON.stringify({error:error.stack,errors,external,cursorReads,state:cdp?await cdp.eval('({url:location.href,focus:document.activeElement?.outerHTML,rows:document.querySelectorAll(".tx-row").length})'):null},null,2));
    throw error;
  } finally {
    if(cdp)cdp.ws.close();
    await Promise.all([chrome,server].map(child=>new Promise(resolve=>{
      if(child.exitCode!==null||child.signalCode!==null){resolve();return;}
      child.once('exit',resolve); child.kill('SIGTERM');
      setTimeout(()=>{if(child.exitCode===null&&child.signalCode===null)child.kill('SIGKILL');},3000).unref();
    })));
    if(serverErrors)fs.writeFileSync(path.join(artifacts,'server.log'),serverErrors);
    const cleanup={serverExited:server.exitCode!==null||server.signalCode!==null,chromeExited:chrome.exitCode!==null||chrome.signalCode!==null,ownedRoot:root};
    if(!artifacts.startsWith(root+path.sep)) {fs.rmSync(root,{recursive:true,force:true}); cleanup.ownedRootRemoved=!fs.existsSync(root);}
    fs.writeFileSync(path.join(artifacts,'cleanup.json'),JSON.stringify(cleanup,null,2));
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
