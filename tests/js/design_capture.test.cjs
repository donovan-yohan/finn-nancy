const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Execute the actual feature renderer without a production testing API.
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.listeners = {}; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
}
function harness(records = [], serverIds = []) {
  const nodes = Object.fromEntries(['capture-center', 'capture-status-list', 'capture-count', 'capture-sync-note', 'processing-nav-count'].map(id => [id, new Element('div')]));
  const source = fs.readFileSync(path.resolve(__dirname, '../../app/web/static/capture-outbox.js'), 'utf8');
  const window = { FinnCaptureStore: { async listCaptures() { return records; } } };
  const document = {
    getElementById(id) { return nodes[id] || null; },
    createElement(tag) { return new Element(tag); },
    addEventListener() {},
    querySelectorAll(selector) {
      assert.equal(selector, '[data-server-document-id]');
      return serverIds.map(id => ({ dataset: { serverDocumentId: String(id) } }));
    },
  };
  vm.runInNewContext(source.replace('  document.addEventListener("DOMContentLoaded",',
    '  window.hooks = { captureItem, render };\n  document.addEventListener("DOMContentLoaded",'),
    { window, document, navigator: { onLine: false } });
  return { ...window.hooks, nodes };
}

test('runtime rows retain Pending versus acknowledged server phases and recovery actions', () => {
  const h = harness();
  for (const [state, file, expected, actions] of [
    ['pending', {}, 'Pending', ['Retry Generated.jpg', 'Remove']],
    ['sending', {}, 'Pending', []],
    ['retry', {}, 'Pending', ['Retry Generated.jpg', 'Remove']],
    ['saved', null, 'Saved', []],
    ['processing', null, 'Processing', []],
    ['needs_review', null, 'Needs review', ['Review', 'Dismiss']],
    ['filed', null, 'Filed', ['View', 'Dismiss']],
    ['failed', {}, 'Failed', ['Retry Generated.jpg', 'Remove']],
    ['failed', null, 'Failed after Saved', ['Refresh status', 'Dismiss']],
  ]) {
    const row = h.captureItem({ id: 'stable-generated-id', name: 'Generated.jpg', state, file });
    assert.equal(row.dataset.captureId, 'stable-generated-id');
    assert.equal(row.attrs.role, 'listitem');
    assert.equal(row.children[0].children[1].textContent, expected);
    assert.deepEqual(row.children[1].children.map(child => child.textContent), actions);
    assert.match(row.attrs['aria-labelledby'], /capture-stable-generated-id-name/);
    assert.match(row.attrs['aria-describedby'], /capture-stable-generated-id-detail/);
  }
  const retry = h.captureItem({ id: 'retry', name: 'Generated.jpg', state: 'retry', file: {}, autoRetry: false, lastError: 'Generated acknowledgement missing' });
  assert.equal(retry.children[0].children[1].textContent, 'Retry needed');
  assert.equal(retry.children[0].children[3].className, 'capture-error');
});

test('partial acknowledgement never suppresses an unacknowledged device original', async () => {
  const h = harness([
    { id: 'device', name: 'Pending.jpg', state: 'pending', file: {}, serverDocumentId: 7 },
    { id: 'ack', name: 'Saved.jpg', state: 'saved', file: null, serverDocumentId: 7 },
  ], [7]);
  await h.render();
  assert.equal(h.nodes['capture-count'].textContent, '1');
  assert.equal(h.nodes['capture-center'].hidden, false);
  const rows = h.nodes['capture-status-list'].children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].dataset.captureId, 'device');
  assert.equal(rows[0].children[0].children[1].textContent, 'Pending');
  assert.match(h.nodes['capture-sync-note'].textContent, /Keep the original until status reaches Saved/);
});

test('lane CSS supports feature-generated markup without legacy app.css', () => {
  const css = fs.readFileSync(path.resolve(__dirname, '../../app/web/static/foundation/capture.css'), 'utf8');
  for (const selector of ['.capture-status-item', '.capture-status-copy', '.capture-state', '.capture-detail', '.capture-error', '.capture-sync-note', '.capture-status-actions .compact-action', '.capture-count']) {
    assert.ok(css.includes(selector), selector);
  }
  assert.match(css, /min-height:var\(--control\)/);
  assert.doesNotMatch(css, /@import|#[0-9a-f]{3,8}\b|gradient\(|blur\(/i);
});
