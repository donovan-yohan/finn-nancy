const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class Element {
  constructor() { this.children = []; this.listeners = {}; this.textContent = ''; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
}

function harness({ failAt = Infinity, initialRecords = [] } = {}) {
  const toast = new Element();
  const timers = new Map();
  const records = initialRecords;
  let timerId = 0;
  const document = {
    activeElement: null,
    getElementById(id) { return id === 'toast' ? toast : null; },
    createElement() { return new Element(); },
    addEventListener() {},
    querySelectorAll() { return []; },
  };
  const window = {
    FinnCaptureStore: {
      createCaptureRecord(file) { return { file, name: file.name, state: 'pending' }; },
      async putCapture(record) {
        if (records.length === failAt) throw new Error('quota');
        records.push(record);
      },
      async listCaptures() { return records.map(record => ({ ...record })); },
      async updateCapture(id, updater) {
        const index = records.findIndex(record => record.id === id);
        records[index] = updater(records[index]);
        return records[index];
      },
    },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const source = fs.readFileSync(path.resolve(__dirname, '../../app/web/static/capture-outbox.js'), 'utf8');
  // Access the actual closure without exposing testing API in the shipped app.
  const instrumented = source.replace('  document.addEventListener("DOMContentLoaded",',
    '  window.hooks = { notifyUpload, enqueueFiles, markPendingReconnected, markPendingOffline };\n  document.addEventListener("DOMContentLoaded",');
  vm.runInNewContext(instrumented, { window, document, navigator: { onLine: false } });
  return { toast, timers, records, document, ...window.hooks };
}

test('toast is one linked notification, not an accumulating queue', () => {
  const h = harness();
  h.notifyUpload('1 file queued', false);
  h.notifyUpload('2 files queued', false);
  assert.equal(h.toast.children.length, 1);
  const [link, dismiss] = h.toast.children[0].children;
  assert.equal(link.href, '/processing');
  assert.equal(link.textContent, '2 files queued · View processing');
  assert.equal(h.timers.size, 1);
  dismiss.listeners.click();
  assert.equal(h.toast.children.length, 0);
  assert.equal(h.timers.size, 0);
});

test('normal toast expires but focus pauses removal', () => {
  const h = harness();
  h.notifyUpload('1 file queued', false);
  const notice = h.toast.children[0];
  notice.listeners.focusin();
  assert.equal(h.timers.size, 0);
  notice.listeners.focusout();
  const timer = [...h.timers.values()][0];
  assert.equal(timer.delay, 8000);
  h.document.activeElement = notice.children[0];
  timer.callback();
  assert.equal(h.toast.children.length, 1);
  h.document.activeElement = null;
  notice.listeners.focusout();
  [...h.timers.values()][0].callback();
  assert.equal(h.toast.children.length, 0);
});

test('partial storage failure stays visible and preserves accepted originals', async () => {
  const h = harness({ failAt: 1 });
  assert.equal(await h.enqueueFiles([{ name: 'a.jpg' }, { name: 'b.jpg' }], {}), false);
  assert.equal(h.records.length, 1);
  assert.equal(h.records[0].file.name, 'a.jpg');
  assert.match(h.toast.children[0].children[0].textContent, /1 of 2 files added/);
  assert.equal(h.timers.size, 0);
});

test('queued toast appears only after device storage accepts the whole batch', async () => {
  const h = harness();
  assert.equal(await h.enqueueFiles([{ name: 'synthetic.jpg' }], {}), true);
  assert.equal(h.records[0].state, 'pending');
  assert.equal(h.records[0].file.name, 'synthetic.jpg');
  assert.equal(h.toast.children[0].children[0].textContent, '1 file queued · View processing');
  assert.doesNotMatch(h.toast.children[0].children[0].textContent, /Saved/);
});

test('overlapping reconnect notifications record one offline episode', async () => {
  const h = harness({ initialRecords: [{ id: 'test', file: {}, state: 'pending',
    offlineStartedAt: '2026-01-01T00:00:00.000Z', offlineRecoverySequence: 0 }] });
  await Promise.all([
    h.markPendingReconnected(Date.parse('2026-01-01T00:00:01.000Z')),
    h.markPendingReconnected(Date.parse('2026-01-01T00:00:02.000Z')),
  ]);
  assert.equal(h.records[0].offlineRecoverySequence, 1);
  assert.equal(h.records[0].offlineRecoveryMs, 1000);
});
