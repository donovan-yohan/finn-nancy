/* One owned headless browser. Generated ledger, real routes, no lifespan/workers. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const os = require('node:os');
const root = path.resolve(__dirname, '../..');
const artifacts = path.resolve(process.argv[2] || path.join(os.tmpdir(), 'approval-consequence'));
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'approval-consequence-runtime-'));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function port() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const value = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return value;
}
async function poll(read) {
  const deadline = Date.now() + 15000;
  let last;
  while (Date.now() < deadline) {
    try { const value = await read(); if (value) return value; } catch (error) { last = error; }
    await pause(50);
  }
  throw last || Error('Readiness deadline exceeded');
}
class CDP {
  constructor(url) {
    this.id = 0; this.pending = new Map(); this.errors = [];
    this.socket = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener('open', resolve);
      this.socket.addEventListener('error', reject);
    });
    this.socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id); clearTimeout(pending.timer);
        message.error ? pending.reject(message.error) : pending.resolve(message.result);
      } else if (message.method === 'Runtime.exceptionThrown') this.errors.push(message.params);
      else if (message.method === 'Page.javascriptDialogOpening') {
        this.call('Page.handleJavaScriptDialog', {accept:true}).catch(error => this.errors.push(error));
      }
    });
  }
  async call(method, params = {}) {
    await this.ready;
    const id = ++this.id;
    const result = new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(Error('CDP deadline ' + method)); }, 15000);
      this.pending.set(id, {resolve, reject, timer});
    });
    this.socket.send(JSON.stringify({id, method, params}));
    return result;
  }
  async eval(expression) {
    const result = await this.call('Runtime.evaluate', {expression, returnByValue:true, awaitPromise:true});
    assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  async go(url) {
    await this.call('Page.navigate', {url});
    await poll(() => this.eval(`location.href===${JSON.stringify(url)} && document.readyState==='complete' && !!document.querySelector('h1')`));
  }
  async click(selector) {
    const position = await this.eval(`(() => {
      const e=document.querySelector(${JSON.stringify(selector)}); if(!e)throw Error('Missing control');
      e.scrollIntoView({block:'center',behavior:'instant'});
      const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
      if(!e.contains(document.elementFromPoint(x,y)))throw Error('Control masked');
      return {x,y};
    })()`);
    await this.call('Input.dispatchMouseEvent', {type:'mousePressed', ...position, button:'left', clickCount:1});
    await this.call('Input.dispatchMouseEvent', {type:'mouseReleased', ...position, button:'left', clickCount:1});
  }
}
const fixture = String.raw`
import json,os,sys
from pathlib import Path
runtime=Path(os.environ['APPROVAL_RUNTIME'])
db=runtime/'generated.sqlite'
os.environ.update(DB_PATH=str(db),DATA_DIR=str(runtime/'data'),STRICT_LOCAL_MODE='true',PLAID_ENABLED='false',READ_ONLY='false',API_TOKEN='',EMBEDDINGS_ENABLED='false',HOME_CURRENCY='CAD')
from app.config import Settings,get_settings
Settings.model_config['env_file']=None
get_settings.cache_clear()
def audit(event,args):
 if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
  name=Path(os.fsdecode(args[0])).name
  assert not(name=='.env' or name.startswith('.env.')), 'dotenv forbidden'
 if event=='sqlite3.connect':assert Path(os.fsdecode(args[0])).resolve()==db.resolve(), 'generated DB only'
sys.addaudithook(audit)
from app.db import migrate,engine,repo_actions,repo_ledger
migrate.seed_sample(str(db))
with engine.write_tx(db) as conn:
 conn.execute("UPDATE transactions SET description='Generated Cedar Market household groceries' WHERE id=3")
 tx=repo_ledger.insert_transaction(conn,account_id=1,posted_on='2026-01-06',description='Generated split household purchase',counterparty='Generated Cedar Market',amount_cents=-16243,source='manual',external_id='approval-browser-split',source_document_id=None,source_confidence=1,flow_kind='purchase')
 split=repo_ledger.insert_split(conn,transaction_id=tx,category_id=4,amount_cents=-10000)
 sibling=repo_ledger.insert_split(conn,transaction_id=tx,category_id=6,amount_cents=-6243)
 def enqueue(payload):return repo_actions.enqueue_proposal(conn,kind='recategorization',payload=payload,evidence={'transaction_ids':[payload['transaction_id']]},confidence=.91,rationale='Review category',agent_run_id='test:browser-consequence')
 missing=enqueue({'transaction_id':3,'to_category_id':5})
 conn.execute('UPDATE proposed_actions SET payload_json=? WHERE id=?',(json.dumps({'transaction_id':999999,'to_category_id':5}),missing))
 selected=enqueue({'transaction_id':tx,'split_id':split,'to_category_id':5})
 single=enqueue({'transaction_id':3,'to_category_id':5})
 names={r['id']:r['name'] for r in conn.execute('SELECT id,name FROM categories')}
ids={'single':single,'selected':selected,'missing':missing,'transaction':tx,'split':split,'sibling':sibling,'names':names}
from app.web.app import create_app
app=create_app()
@app.get('/__fixture')
def state():
 with engine.read_conn(db,read_only=True) as conn:
  return {**ids,'splits':[dict(r) for r in conn.execute('SELECT id,transaction_id,category_id,amount_cents FROM transaction_splits ORDER BY id')],'proposals':{str(i):repo_actions.get_proposal(conn,i) for i in (single,selected,missing)}}
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['APPROVAL_PORT']),lifespan='off',log_level='warning')
`;
(async () => {
  fs.mkdirSync(artifacts, {recursive:true});
  const serverPort = await port(), debugPort = await port(), base = `http://127.0.0.1:${serverPort}`;
  let server, browser, cdp; const logs = [], checks = [];
  const state = async () => { const response = await fetch(base + '/__fixture'); assert(response.ok); return response.json(); };
  try {
    server = spawn(path.join(root,'.venv/bin/python'), ['-c',fixture], {cwd:root, env:{...process.env, APPROVAL_RUNTIME:scratch, APPROVAL_PORT:String(serverPort)}, stdio:['ignore','pipe','pipe']});
    server.stdout.on('data', data => logs.push(String(data))); server.stderr.on('data', data => logs.push(String(data)));
    const ids = await poll(state);
    browser = spawn(process.env.CHROMIUM_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', [
      '--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking',
      '--disable-component-update','--disable-sync',`--user-data-dir=${scratch}/chrome`,
      `--remote-debugging-port=${debugPort}`, 'about:blank'], {stdio:'ignore'});
    const targets = await poll(async () => (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json());
    cdp = new CDP(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
    await cdp.call('Page.enable'); await cdp.call('Runtime.enable');
    await cdp.call('Network.enable');
    await cdp.call('Network.setBlockedURLs', {urlPatterns:[{urlPattern:base+'/*',block:false},{urlPattern:'http://*/*',block:true},{urlPattern:'https://*/*',block:true}]});
    // Default deny all non-loopback renderer fetches, independent of app settings.
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', {source:`window.__approvalTest=true;`});
    const before = JSON.stringify(await state());
    for (const width of [1440,390]) for (const theme of ['light','dark']) {
      await cdp.call('Emulation.setDeviceMetricsOverride', {width,height:1000,deviceScaleFactor:1,mobile:false});
      await cdp.call('Network.setCookie', {name:'finn_nancy_theme',value:theme,url:base,path:'/'});
      await cdp.go(base+'/actions');
      await cdp.eval('document.fonts.ready');
      for (const kind of ['single','selected','missing']) {
        const id=ids[kind];
        const geometry=await cdp.eval(`(() => {
          const article=document.getElementById('proposal-${id}'), summary=article.querySelector('.manage-consequence');
          const button=article.querySelector('form[action$="/approve"] button');
          summary.scrollIntoView({block:'center',behavior:'instant'});
          const r=summary.getBoundingClientRect(),b=button.getBoundingClientRect();
          const controls=[...article.querySelectorAll('button,summary')].filter(e=>e.checkVisibility()).map(e=>{const c=e.getBoundingClientRect();return {text:e.textContent.trim(),height:c.height};});
          const hit=(x,y,e)=>e.contains(document.elementFromPoint(x,y));
          return {theme:document.documentElement.dataset.theme,scroll:document.documentElement.scrollWidth,width:innerWidth,
            text:summary.textContent.replace(/\\s+/g,' ').trim(),visible:summary.checkVisibility(),insideDisclosure:!!summary.closest('details'),
            disclosuresClosed:[...article.querySelectorAll('details')].every(e=>!e.open),
            summaryRect:{top:r.top,bottom:r.bottom,left:r.left,right:r.right},buttonRect:{top:b.top,bottom:b.bottom},
            summaryHit:hit(r.x+r.width/2,r.y+r.height/2,summary),buttonHit:hit(b.x+b.width/2,b.y+b.height/2,button),controls};
        })()`);
        assert.equal(geometry.theme,theme); assert(geometry.scroll<=width+1,JSON.stringify(geometry));
        assert(geometry.visible && !geometry.insideDisclosure && geometry.disclosuresClosed);
        assert(geometry.summaryRect.top>=0 && geometry.summaryRect.bottom<=1000,JSON.stringify(geometry));
        assert(geometry.summaryRect.bottom<geometry.buttonRect.top);
        assert(geometry.buttonRect.bottom<=1000 && geometry.buttonHit && geometry.summaryHit,JSON.stringify(geometry));
        assert.equal(geometry.controls.length,3); assert(geometry.controls.every(control=>control.height>=47),JSON.stringify(geometry));
        if(kind==='missing')assert.match(geometry.text,/Transaction unavailable/);
        else {
          assert.match(geometry.text,/2026-01-0[56]/); assert.match(geometry.text,/-\$162\.43 CAD/);
          assert(geometry.text.includes(`${ids.names[4]} → ${ids.names[5]}`));
          if(kind==='selected')assert.match(geometry.text,/Selected split amount -\$100\.00 CAD/);
        }
        const screenshot=await cdp.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
        fs.writeFileSync(path.join(artifacts,`${kind}-${width}-${theme}.png`),Buffer.from(screenshot.data,'base64'));
        checks.push({kind,width,theme,...geometry});
      }
    }
    assert.equal(JSON.stringify(await state()),before,'GET changed durable state');
    // The browser submits the unchanged real primary form, not an API replacement.
    await cdp.go(base+'/actions');
    await cdp.click(`#proposal-${ids.single} form[action$="/approve"] button`);
    await poll(async () => (await state()).proposals[ids.single].status==='approved');
    const approved=await state();
    assert.equal(approved.splits.find(row=>row.transaction_id===3).category_id,5);
    // Expose technical data by native keyboard without requiring it for the consequence.
    await cdp.go(base+'/actions');
    const selector=`#proposal-${ids.selected} details > summary`;
    await cdp.eval(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    await cdp.call('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,nativeVirtualKeyCode:36,text:'\r',unmodifiedText:'\r'});
    await cdp.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,nativeVirtualKeyCode:36});
    assert(await cdp.eval(`document.querySelector('#proposal-${ids.selected} details').open`));
    await cdp.click(`#proposal-${ids.selected} details:nth-of-type(2)>summary`);
    await cdp.eval(`document.querySelector('#target-category-${ids.selected}').value='6'`);
    await cdp.click(`#proposal-${ids.selected} details:nth-of-type(2) form[action$="/approve"] button`);
    await poll(async () => (await state()).proposals[ids.selected].status==='edited_approved');
    const edited=await state();
    assert.equal(edited.splits.find(row=>row.id===ids.split).category_id,6);
    assert.equal(edited.splits.find(row=>row.id===ids.sibling).category_id,6);
    assert.equal(edited.splits.find(row=>row.id===ids.sibling).amount_cents,-6243);
    await cdp.go(base+'/actions');
    await cdp.click(`#proposal-${ids.missing} details:nth-of-type(2)>summary`);
    await cdp.eval(`document.querySelector('#reject-feedback-${ids.missing}').value='Generated missing-reference rejection'`);
    await cdp.click(`#proposal-${ids.missing} form[action$="/reject"] button`);
    await poll(async () => (await state()).proposals[ids.missing].status==='rejected');
    assert.equal(cdp.errors.length,0,JSON.stringify(cdp.errors));
    fs.writeFileSync(path.join(artifacts,'browser-report.json'),JSON.stringify({checks,getDumpEqual:true,interactions:['native primary approve','Enter opens technical disclosure','native edited approval preserves sibling','native reject with confirmation'],runtimeErrors:cdp.errors},null,2));
    console.log(JSON.stringify({renders:checks.length,getStateEqual:true,interactions:4,runtimeErrors:0}));
  } finally {
    if(cdp)cdp.socket.close();
    const cleanup=[];
    for(const child of [browser,server]) if(child) {
      if(child.exitCode===null && child.signalCode===null) {
        child.kill('SIGTERM'); await Promise.race([new Promise(resolve=>child.once('exit',resolve)),pause(3000)]);
        if(child.exitCode===null && child.signalCode===null) {child.kill('SIGKILL'); await new Promise(resolve=>child.once('exit',resolve));}
      }
      cleanup.push({pid:child.pid,exitCode:child.exitCode,signalCode:child.signalCode});
    }
    fs.writeFileSync(path.join(artifacts,'fixture.log'),logs.join(''));
    fs.rmSync(scratch,{recursive:true,force:true});
    fs.writeFileSync(path.join(artifacts,'cleanup.json'),JSON.stringify({children:cleanup,scratchRemoved:!fs.existsSync(scratch)},null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
