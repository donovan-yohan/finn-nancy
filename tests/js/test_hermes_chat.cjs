const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// A minimal DOM/socket boundary: assertions exercise the shipped script, not a
// duplicate reducer. No service, credentials, database, or model is contacted.
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {};
    this.dataset = {}; this.value = ''; this.disabled = false; this.hidden = false;
    this._text = ''; this.scrollHeight = 100; this.scrollTop = 0; this.clientHeight = 100;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('Provider content must never be rendered as HTML'); }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() { this.focused = true; }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  querySelectorAll(selector) {
    const tags = selector.split(',').map(s => s.trim());
    return this.children.flatMap(child => [...(tags.includes(child.tagName) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  fire(name, extra = {}) {
    const event = { preventDefault() { this.prevented = true; }, ...extra };
    this.listeners[name]?.(event); return event;
  }
}

function harness({ enabled = true, seed = '' } = {}) {
  const ids = Object.fromEntries(['chat-form', 'chat-input', 'chat-list', 'chat-send',
    'chat-stop', 'chat-status', 'chat-error', 'chat-uncertain', 'chat-reconnect',
    'chat-requests', 'chat-empty'].map(id => [id, new Element()]));
  const shell = new Element();
  shell.dataset = { chatEnabled: String(enabled), seedMessage: seed };
  const sockets = []; const timers = new Map(); let sequence = 0;
  class Socket {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(text) { this.sent.push(JSON.parse(text)); }
    close() { this.readyState = 3; this.onclose?.({}); }
    open() { this.readyState = 1; this.onopen?.({}); }
    receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  }
  const window = {
    location: { protocol: 'https:', host: 'synthetic.invalid' },
    crypto: { randomUUID: () => `synthetic-${++sequence}` }, WebSocket: Socket,
    setTimeout(callback, delay) { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener(name, callback) { this.listeners[name] = callback; }, listeners: {},
  };
  const document = {
    querySelector: () => shell, getElementById: id => ids[id],
    createElement: tag => new Element(tag), activeElement: null,
  };
  const sourcePath = path.resolve(__dirname, '../../app/web/static/hermes-chat.js');
  assert.ok(fs.existsSync(sourcePath), 'the custom Hermes frontend must exist');
  vm.runInNewContext(fs.readFileSync(sourcePath, 'utf8'), { window, document, console });
  return {
    ids, sockets, timers, document, shell, window,
    get socket() { return sockets.at(-1); },
    snapshot(frame = {}) { this.socket.open(); this.socket.receive({ type: 'snapshot', messages: [], inflight: null, running: false, ...frame }); },
    submit(text) { ids['chat-input'].value = text; ids['chat-form'].fire('submit'); },
    tick() { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.callback(); return timer.delay; },
  };
}

function descendants(node, tag) { return node.querySelectorAll(tag); }

test('history window is disclosed without claiming completeness or offering new authority', () => {
  const h = harness();
  h.snapshot({ messages: [{role: 'assistant', content: 'Recent answer'}],
    history_window: {status: 'recent', limit: 20, returned: 20} });
  assert.match(h.ids['chat-list'].textContent, /up to 20 stored entries/i);
  assert.match(h.ids['chat-list'].textContent, /Older history remains in Hermes/i);
  assert.match(h.ids['chat-list'].textContent, /not the full conversation/i);
  assert.match(h.ids['chat-list'].textContent, /Recent answer/);
  assert.equal(h.socket.sent.length, 0);
  assert.equal(descendants(h.ids['chat-list'], 'button, a').length, 0);
  h.snapshot({history_window: {status: 'unavailable', limit: 20, returned: 0}});
  assert.match(h.ids['chat-list'].textContent, /Saved history is unavailable/i);
  assert.doesNotMatch(h.ids['chat-list'].textContent, /Recent answer/);
  h.snapshot();
  assert.equal(h.ids['chat-list'].children.length, 0);
});

test('terminal errors settle the turn without treating validation errors as terminal', () => {
  const h = harness(); h.snapshot(); h.submit('Synthetic question');
  h.socket.receive({type: 'accepted', id: h.socket.sent[0].id});
  h.ids['chat-input'].value = 'Next question';
  h.socket.receive({type: 'error', message: 'Invalid request'});
  assert.equal(h.ids['chat-send'].disabled, true);
  h.socket.receive({type: 'error', message: 'Turn failed', terminal: true});
  assert.equal(h.ids['chat-send'].disabled, false);
  assert.equal(h.ids['chat-stop'].disabled, true);
});

test('reconnect snapshot removes questions that expired while disconnected', () => {
  const h = harness(); h.snapshot();
  h.socket.receive({type: 'request', id: 'expired', kind: 'approval', params: {command: 'synthetic', choices: ['once','deny']}});
  h.socket.close(); h.tick();
  h.snapshot({open_request_ids: []});
  assert.equal(h.ids['chat-requests'].children.length, 0);
});

test('reconnect restores canonical inflight, preserves uncertain draft and never replays', () => {
  const h = harness(); h.snapshot(); h.submit('Synthetic send');
  h.socket.close();
  assert.match(h.ids['chat-uncertain'].textContent, /uncertain/i);
  assert.equal(h.ids['chat-input'].value, 'Synthetic send');
  assert.equal(h.ids['chat-send'].disabled, true);
  h.tick();
  h.snapshot({ messages: [{ role: 'user', content: 'Synthetic send' }],
    inflight: { user: 'Synthetic send', assistant: 'Partial ', streaming: true }, running: true });
  assert.equal(h.socket.sent.length, 0);
  assert.equal(h.ids['chat-list'].children.length, 2, 'no duplicate inflight user');
  h.socket.receive({ type: 'token', text: 'answer' });
  assert.match(h.ids['chat-list'].textContent, /Partial answer/);
  h.socket.receive({ type: 'done', message: 'Canonical answer', status: 'complete' });
  assert.match(h.ids['chat-list'].textContent, /Canonical answer/);
  assert.doesNotMatch(h.ids['chat-list'].textContent, /Partial answer/);
  assert.equal(h.ids['chat-stop'].disabled, true);
  assert.match(h.ids['chat-uncertain'].textContent, /uncertain/i);
});

test('reconnect attempts are bounded even when sockets open without a snapshot', () => {
  const h = harness();
  const delays = [];
  for (let attempt = 0; attempt < 6; attempt++) {
    h.socket.open(); h.socket.close();
    if (h.timers.size) delays.push(h.tick());
  }
  assert.equal(h.sockets.length, 6);
  assert.equal(h.timers.size, 0);
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000]);
  assert.equal(h.ids['chat-reconnect'].hidden, false);
});

test('streamed interim is shown once; tool results survive missing starts after reconnect', () => {
  const h = harness(); h.snapshot({ running: true, inflight: {
    user: 'Synthetic request', assistant: 'Checking', streaming: true,
  } });
  h.socket.receive({ type: 'interim', text: 'Checking', already_streamed: true });
  h.socket.receive({ type: 'step_end', id: 's1', tool: 'synthetic_tool', output: '<img src=x onerror=alert(1)>' });
  h.socket.receive({ type: 'token', text: 'Final text' });
  h.socket.receive({ type: 'done', message: 'Final text', status: 'interrupted' });
  const text = h.ids['chat-list'].textContent;
  assert.equal(text.split('Checking').length - 1, 1);
  assert.match(text, /synthetic_tool/);
  assert.match(text, /<img src=x onerror=alert\(1\)>/);
  assert.match(text, /Final text/);
  assert.match(text, /Interrupted/);
  assert.equal(descendants(h.ids['chat-list'], 'img').length, 0);
});

test('Stop is explicit and Enter respects shift and composition', () => {
  const h = harness(); h.snapshot(); h.ids['chat-input'].value = 'Synthetic';
  h.ids['chat-input'].fire('keydown', { key: 'Enter', isComposing: true });
  h.ids['chat-input'].fire('keydown', { key: 'Enter', shiftKey: true });
  h.ids['chat-input'].fire('keydown', { key: 'Enter', keyCode: 229 });
  assert.equal(h.socket.sent.length, 0);
  h.ids['chat-input'].fire('keydown', { key: 'Enter' });
  assert.equal(h.socket.sent.length, 1);
  h.socket.receive({ type: 'accepted', id: h.socket.sent[0].id });
  h.ids['chat-stop'].fire('click');
  assert.deepEqual(h.socket.sent[1], { type: 'stop' });
  assert.match(h.ids['chat-status'].textContent, /Stopping/);
});

test('unconfigured chat makes no connection or fake successful transcript', () => {
  const h = harness({ enabled: false });
  h.submit('Synthetic');
  assert.equal(h.sockets.length, 0);
  assert.equal(h.ids['chat-input'].disabled, true);
  assert.equal(h.ids['chat-list'].children.length, 0);
});

test('approval is explicit, once-or-deny only, retained until matching cancellation', () => {
  const h = harness(); h.snapshot({ running: true });
  h.socket.receive({ type: 'request', id: 'approval-1', kind: 'approval', params: {
    command: 'synthetic --check', description: '<b>Review this command</b>', choices: ['once', 'deny', 'always'],
  } });
  h.socket.receive({ type: 'request', id: 'clarify-1', kind: 'clarify', params: { question: 'Which synthetic option?' } });
  assert.equal(h.ids['chat-requests'].children.length, 2);
  assert.equal(h.socket.sent.length, 0);
  const card = h.ids['chat-requests'].children[0];
  const buttons = descendants(card, 'button');
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].textContent, 'Allow once');
  buttons[0].fire('click');
  assert.deepEqual(h.socket.sent, [{ type: 'answer', id: 'approval-1', answer: { choice: 'once' } }]);
  assert.equal(h.ids['chat-requests'].children.length, 2);
  assert.equal(buttons[0].disabled, true);
  h.socket.receive({ type: 'request_cancel', id: 'unrelated' });
  assert.equal(h.ids['chat-requests'].children.length, 2);
  h.socket.receive({ type: 'request_cancel', id: 'approval-1' });
  assert.equal(h.ids['chat-requests'].children.length, 1);
  assert.match(h.ids['chat-requests'].textContent, /Which synthetic option/);
});

test('clarifications send strings and keyed multi-question answers without auto-submitting defaults', () => {
  const h = harness(); h.snapshot({ running: true });
  h.socket.receive({ type: 'request', id: 'q1', kind: 'clarify', params: { question: 'Explain?' } });
  const first = h.ids['chat-requests'].children[0];
  descendants(first, 'textarea')[0].value = 'Synthetic explanation';
  descendants(first, 'form')[0].fire('submit');
  assert.deepEqual(h.socket.sent[0], { type: 'answer', id: 'q1', answer: { answer: 'Synthetic explanation' } });
  h.socket.receive({ type: 'request', id: 'q2', kind: 'clarify', params: {
    questions: [{ qid: 'one', question: 'Choose one', choices: ['Alpha', 'Beta'] },
      { qid: 'many', question: 'Choose several', choices: ['Red', 'Green'], multi_select: true }],
    answers: { one: 'Beta' },
  } });
  const second = h.ids['chat-requests'].children[1];
  const options = descendants(second, 'input');
  options.filter(option => option.type === 'checkbox').forEach(option => { option.checked = true; });
  assert.equal(h.socket.sent.length, 1);
  descendants(second, 'form')[0].fire('submit');
  assert.deepEqual(h.socket.sent[1], { type: 'answer', id: 'q2', answer: { answers: { one: 'Beta', many: 'Red, Green' } } });
});

test('disconnected approvals are inert until replayed by the server, never auto-approved', () => {
  const h = harness(); h.snapshot({ running: true });
  const request = { type: 'request', id: 'pending', kind: 'approval', params: { command: 'synthetic', choices: ['once', 'deny'] } };
  h.socket.receive(request);
  const card = h.ids['chat-requests'].children[0];
  h.socket.close();
  assert.equal(descendants(card, 'button')[0].disabled, true);
  h.tick(); h.snapshot({ running: true });
  assert.equal(descendants(card, 'button')[0].disabled, true);
  h.socket.receive(request);
  assert.equal(h.ids['chat-requests'].children.length, 1);
  assert.equal(h.socket.sent.length, 0);
  const deny = descendants(h.ids['chat-requests'].children[0], 'button')[1];
  assert.equal(deny.disabled, false);
  deny.fire('click');
  assert.deepEqual(h.socket.sent[0], { type: 'answer', id: 'pending', answer: { choice: 'deny' } });
});

test('back-forward cache restores a connection without resending a draft', () => {
  const h = harness(); h.snapshot(); h.submit('Synthetic pending draft');
  h.window.listeners.pagehide();
  assert.equal(h.timers.size, 0);
  h.window.listeners.pageshow?.({ persisted: true });
  assert.equal(h.sockets.length, 2);
  h.snapshot();
  assert.equal(h.socket.sent.length, 0);
  assert.equal(h.ids['chat-input'].value, 'Synthetic pending draft');
});

test('rejected submission keeps the edited draft and reports failure instead of waiting forever', () => {
  const h = harness(); h.snapshot(); h.submit('Synthetic question');
  h.ids['chat-input'].value = 'Edited synthetic question';
  h.socket.receive({ type: 'error', code: 'invalid_request', message: '<b>Cannot send</b>' });
  assert.equal(h.ids['chat-input'].value, 'Edited synthetic question');
  assert.equal(h.ids['chat-error'].textContent, '<b>Cannot send</b>');
  assert.doesNotMatch(h.ids['chat-status'].textContent, /acceptance/);
  assert.equal(h.ids['chat-send'].disabled, false);
});

test('duplicate request frames cannot re-enable an answer already sent on this connection', () => {
  const h = harness(); h.snapshot({ running: true });
  const frame = { type: 'request', id: 'duplicate', kind: 'approval', params: { command: 'synthetic', choices: ['once', 'deny'] } };
  h.socket.receive(frame);
  const allow = descendants(h.ids['chat-requests'].children[0], 'button')[0];
  allow.fire('click');
  h.socket.receive(frame);
  assert.equal(allow.disabled, true);
  allow.fire('click');
  assert.equal(h.socket.sent.length, 1);
});

test('interrupted completion without a final message retains the partial response', () => {
  const h = harness(); h.snapshot({ running: true, inflight: { user: 'Synthetic', assistant: 'Partial response', streaming: true } });
  h.socket.receive({ type: 'done', message: '', status: 'interrupted' });
  assert.match(h.ids['chat-list'].textContent, /Partial response/);
  assert.match(h.ids['chat-list'].textContent, /Interrupted/);
});

test('thread-in-use errors and close 4409 stop reconnect attempts with actionable guidance', () => {
  for (const mode of ['error', 'close']) {
    const h = harness(); h.socket.open();
    if (mode === 'error') h.socket.receive({ type: 'error', code: 'thread_in_use', message: 'Busy' });
    else { h.socket.readyState = 3; h.socket.onclose({ code: 4409 }); }
    assert.equal(h.timers.size, 0, mode);
    assert.match(h.ids['chat-error'].textContent, /other tab|another tab/i);
    assert.match(h.ids['chat-error'].textContent, /new chat/i);
    assert.equal(h.ids['chat-reconnect'].hidden, false);
    assert.equal(h.ids['chat-send'].disabled, true);
  }
});

test('template exposes an accessible disabled-first shell without legacy SSE or embedded history', () => {
  const template = fs.readFileSync(path.resolve(__dirname, '../../app/web/templates/chat.html'), 'utf8');
  assert.match(template, /data-chat-enabled/);
  assert.match(template, /Powered by Hermes/);
  assert.match(template, /Namako/);
  assert.match(template, /role="log"/);
  assert.match(template, /for="chat-input"/);
  assert.match(template, /action="\/chat\/new"/);
  assert.match(template, /href="\/(upload|processing)"/);
  assert.match(template, /hermes-chat\.js/);
  assert.doesNotMatch(template, /\/chat\/stream|\/chat\/ping|for item in messages|data-thread-id/);
});

test('waits for canonical snapshot, sends only explicit submissions, clears draft on acceptance', () => {
  const h = harness({ seed: 'A synthetic question' });
  assert.equal(h.socket.url, 'wss://synthetic.invalid/chat/socket');
  assert.equal(h.ids['chat-input'].value, 'A synthetic question');
  h.submit('A synthetic question');
  assert.equal(h.socket.sent.length, 0);
  h.snapshot();
  assert.equal(h.socket.sent.length, 0, 'seed is a draft, never an automatic send');
  h.submit('A synthetic question');
  assert.deepEqual(h.socket.sent, [{ type: 'submit', id: h.socket.sent[0].id, message: 'A synthetic question' }]);
  assert.equal(h.ids['chat-input'].value, 'A synthetic question');
  h.socket.receive({ type: 'accepted', id: h.socket.sent[0].id });
  assert.equal(h.ids['chat-input'].value, '');
  assert.match(h.ids['chat-list'].textContent, /A synthetic question/);
  assert.equal(h.ids['chat-stop'].disabled, false);
});
