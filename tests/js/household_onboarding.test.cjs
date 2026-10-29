'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = path.resolve(__dirname, '../../app/web/static/household-onboarding.js');

test('unresolved member session has actionable recovery copy instead of a generic failure', () => {
  const vm = require('node:vm');
  const script = fs.readFileSync(source, 'utf8');
  const declaration = script.match(/const messages = (\{[\s\S]*?\n    \});/)[1];
  const messages = vm.runInNewContext('(' + declaration + ')');
  assert.match(messages.member_link_unresolved_check_existing_session || '', /browser that started it/);
  assert.match(messages.member_link_unresolved_check_existing_session || '', /Do not connect again/);
});

test('Hosted Link accepts only the exact HTTPS Plaid /hl/ destination', () => {
  assert.ok(fs.existsSync(source), 'Connections must mount its own onboarding controller');
  const { hostedLink } = require(source);
  assert.equal(hostedLink('https://secure.plaid.com/hl/synthetic'), 'https://secure.plaid.com/hl/synthetic');
  for (const url of ['http://secure.plaid.com/hl/x', 'https://secure.plaid.com.evil.test/hl/x',
    'https://secure.plaid.com/not-hl/x', 'https://' + 'user:pass' + '@' + 'secure.plaid.com/hl/x',
    'javascript:alert(1)', '//secure.plaid.com/hl/x']) {
    assert.throws(() => hostedLink(url), /Invalid Plaid destination/);
  }
});

test('Hosted Link whitespace and non-string values cannot become external destinations', () => {
  const {hostedLink} = require(source);
  assert.throws(() => hostedLink(' https://secure.plaid.com/hl/x'), /Invalid Plaid destination/);
  assert.throws(() => hostedLink({toString:() => 'https://secure.plaid.com/hl/x'}), /Invalid Plaid destination/);
});

test('unconfirmed transport outcomes require a state check rather than blind mutation replay', () => {
  const {uncertainError} = require(source);
  assert.equal(typeof uncertainError, 'function');
  const error = uncertainError();
  assert.equal(error.uncertain, true);
  assert.match(error.message,/not confirmed/);
});

test('binding payload requires a deliberate owner and ledger selection', () => {
  const api = require(source);
  assert.equal(typeof api.bindingPayload, 'function');
  const context = {connectionId:'4', providerAccount:'provider-synthetic', revision:'7'};
  assert.throws(() => api.bindingPayload(context, {account_id:'new', owner:'', role:'primary'}), /owner/i);
  assert.throws(() => api.bindingPayload(context, {account_id:'', owner:'shared', role:'primary'}), /account/i);
  assert.deepEqual(api.bindingPayload(context, {account_id:'12', owner:'shared', role:'secondary', included:true, new_name:'ignored', new_kind:'cash'}), {
    connection_id:4, provider_account_id:'provider-synthetic', expected_revision:7,
    account_id:12, ownership:'shared', owner_member_id:null, included:true,
    role:'secondary', new_name:null, new_kind:null
  });
  const result = api.bindingPayload(context, {account_id:'new', owner:'2', role:'primary', included:false, new_name:'Synthetic account', new_kind:'savings'});
  assert.equal(result.account_id, null);
  assert.equal(result.owner_member_id, 2);
  assert.equal(result.ownership, 'personal');
  assert.equal(result.new_name, 'Synthetic account');
});

test('review inclusion rejects an unset choice and transports explicit include/exclude', () => {
  const {bindingPayload} = require(source);
  const context = {connectionId:'4',providerAccount:'fixture',revision:'0'};
  const base = {account_id:'12',owner:'shared',role:'primary'};
  assert.throws(() => bindingPayload(context,{...base,included:''}), /include or exclude/i);
  assert.equal(bindingPayload(context,{...base,included:'include'}).included,true);
  assert.equal(bindingPayload(context,{...base,included:'exclude'}).included,false);
});

test('draft restoration retains its original optimistic revision and never clears unrelated accounts', () => {
  const {retainedDrafts} = require(source);
  assert.equal(typeof retainedDrafts, 'function');
  const drafts = [{key:'1:same',revision:'3',values:{owner:'shared'}},
    {key:'2:same',revision:'9',values:{owner:'2'}}];
  assert.deepEqual(retainedDrafts(drafts,'1:same'), [drafts[1]]);
  assert.deepEqual(retainedDrafts(drafts,null), drafts);
});

test('mounted Connections controller is exported for the actual page entrypoint', () => {
  assert.equal(typeof require(source).mount, 'function');
});

test('operation ownership holds real controls until authoritative read completes and refuses duplicate dispatch', async () => {
  const {createOperationOwner} = require(source);
  assert.equal(typeof createOperationOwner, 'function');
  let releasePost, releaseRead;
  const postGate = new Promise(resolve => {releasePost = resolve;});
  const readGate = new Promise(resolve => {releaseRead = resolve;});
  const events = [];
  const owner = createOperationOwner({lock:() => events.push('lock'), unlock:() => events.push('unlock'),
    refresh:async () => {events.push('read'); await readGate;}, fail:() => events.push('fail')});
  let calls = 0;
  const first = owner.run(async () => {calls++; await postGate; return 'saved';});
  await owner.run(async () => {calls++;});
  assert.equal(calls, 1);
  assert.deepEqual(events, ['lock']);
  releasePost();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(events, ['lock','read']);
  assert.equal(owner.busy, true);
  releaseRead();
  assert.equal(await first, 'saved');
  assert.deepEqual(events, ['lock','read','unlock']);
  assert.equal(owner.busy, false);
});

test('unlock publishes only after the operation owner is no longer busy', async () => {
  const {createOperationOwner} = require(source);
  let owner;
  owner = createOperationOwner({lock:() => {}, refresh:async () => {},
    unlock:() => assert.equal(owner.busy, false), fail:error => {throw error;}});
  await owner.run(async () => {});
});

test('failed POST is never replayed by state recovery and its error survives readback', async () => {
  const {createOperationOwner} = require(source);
  let posts = 0, reads = 0;
  const failures = [];
  const owner = createOperationOwner({lock:() => {}, unlock:() => {}, refresh:async () => {reads++;},
    fail:(error, read) => failures.push([error.message,read])});
  await owner.run(async () => {posts++; throw Error('uncertain');});
  assert.equal(posts,1);
  assert.equal(reads,1);
  assert.deepEqual(failures,[['uncertain',false]]);
});

test('mount is safe on absent roots and already-mounted roots', () => {
  const {mount} = require(source);
  assert.equal(mount(null), null);
  assert.equal(mount({dataset:{onboardingReady:'true'}}), null);
});

test('mounted member submit freezes posted values before CSRF and locks inputs through failed readback', async () => {
  const {mount} = require(source);
  const handlers = {};
  const field = {dataset:{},tagName:'INPUT',name:'name',value:'Synthetic Avery',type:'text',disabled:false};
  const submit = {dataset:{},tagName:'BUTTON',name:'',disabled:false};
  const retry = {disabled:false,dataset:{action:'refresh'}};
  const region = {textContent:'',dataset:{},hidden:true};
  const scope = {dataset:{scope:'member-add'},querySelector:() => region};
  const form = {dataset:{form:'member-add',draftKey:'member-add'},elements:[field,submit],
    reportValidity:() => true, querySelector:() => submit,
    closest:selector => selector === '[data-scope]' ? scope : null};
  field.form = form;
  const panel = {hidden:true, querySelector:selector => selector === 'button' ? retry : {textContent:''}};
  let releaseToken, releaseRead;
  const tokenGate = new Promise(resolve => {releaseToken = resolve;});
  const readGate = new Promise(resolve => {releaseRead = resolve;});
  const requests = [];
  const doc = {activeElement:null,body:{},getElementById:() => null};
  const win = {fetch:async (url, options) => {
    requests.push([url,options]);
    if (url === '/connections/data/overview') {await tokenGate; return {ok:true,json:async () => ({csrf:'synthetic-csrf'})};}
    if (url === '/connections/data/member') return {ok:true,json:async () => ({member_id:1})};
    if (url === '/connections') {await readGate; return {ok:false};}
    throw Error('unexpected request');
  }};
  doc.defaultView = win;
  const root = {dataset:{readOnly:'false'},ownerDocument:doc,contains:() => false,
    setAttribute:() => {}, removeAttribute:() => {},
    querySelector:selector => selector === '[data-refresh-panel]' ? panel : retry,
    querySelectorAll:selector => {
      if (selector === '[data-scope]') return [scope];
      if (selector.startsWith('form[')) return [form];
      if (selector === 'input,select,textarea,button,fieldset') return [field,submit];
      return [];
    }, addEventListener:(event,handler) => {handlers[event] = handler;}};
  const owner = mount(root);
  const event = {target:{closest:() => form},preventDefault:() => {}};
  handlers.submit(event);
  assert.equal(owner.busy,true);
  assert.equal(field.disabled,true);
  assert.equal(submit.disabled,true);
  assert.equal(region.textContent,'Working…');
  field.value = 'Changed after submit';
  handlers.submit(event);
  releaseToken();
  await new Promise(resolve => setImmediate(resolve));
  const posts = requests.filter(([,options]) => options?.method === 'POST');
  assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0][1].body),{name:'Synthetic Avery'});
  assert.equal(field.disabled,true);
  assert.equal(owner.busy,true);
  releaseRead();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(owner.blocked,true);
  assert.equal(field.disabled,true);
  assert.equal(submit.disabled,true);
  assert.equal(retry.disabled,false);
  assert.equal(root.dataset.lastOperation,'read-failed');
});

test('failed readback never restores stale mutation controls; retry performs a read only', async () => {
  const {createOperationOwner} = require(source);
  let reads = 0, posts = 0, unlocks = 0;
  const owner = createOperationOwner({lock:() => {}, unlock:() => unlocks++,
    refresh:async () => {reads++; if (reads === 1) throw Error('offline');}, fail:() => {}});
  await owner.run(async () => {posts++;});
  assert.equal(owner.blocked, true);
  assert.equal(unlocks, 0);
  await owner.run(async () => {posts++;});
  assert.equal(posts, 1);
  await owner.retryRead();
  assert.equal(reads, 2);
  assert.equal(posts, 1);
  assert.equal(unlocks, 1);
  assert.equal(owner.blocked, false);
});
