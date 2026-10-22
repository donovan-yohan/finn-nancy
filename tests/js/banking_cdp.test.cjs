const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../browser/banking.cjs'), 'utf8');
const definition = source.slice(source.indexOf('class CDP{'), source.indexOf('async function main()'));

function harness() {
  const deadlines = [];
  const ctx = {
    WebSocket: class {
      constructor() { this.sent = []; }
      send(value) { this.sent.push(JSON.parse(value)); }
      close() {}
    },
    setTimeout(fn) { deadlines.push(fn); return deadlines.length; },
    clearTimeout() {},
    pause: async () => {},
  };
  vm.runInNewContext(definition + ';globalThis.client = new CDP("generated");', ctx);
  ctx.client.ws.onopen();
  return {client: ctx.client, deadlines};
}

test('banking CDP ignores late replies after a bounded timeout', async () => {
  const {client, deadlines} = harness();
  const request = client.call('Fetch.fulfillRequest', {requestId: 'generated'});
  await Promise.resolve();
  const rejected = assert.rejects(request, /CDP deadline Fetch.fulfillRequest/);
  deadlines[0]();
  await rejected;
  assert.equal(client.pending.size, 0);
  assert.doesNotThrow(() => client.ws.onmessage({data: JSON.stringify({id: 1, result: {}})}));
});

test('banking CDP drains held event handlers before closing', async () => {
  const {client} = harness();
  const failed = client.call('Fetch.fulfillRequest', {requestId: 'generated'});
  await Promise.resolve();
  const rejected = assert.rejects(failed, /Harness closing/);
  client.close();
  await rejected;
  assert.equal(client.pending.size, 0);
  assert.doesNotThrow(() => client.ws.onmessage({data: JSON.stringify({id: 1, result: {}})}));
});

test('hosted fixture intercepts documents only and fences teardown', () => {
  assert.ok(source.includes("q.resourceType==='Document'"), 'do not fulfill irrelevant favicon requests during target teardown');
  assert.ok(source.includes('Fetch.disable'), 'drain interception before target close');
  assert.ok(source.includes('interceptionErrors'), 'event request failures must be captured, not unhandled promises');
});

test('member-save polling waits for the reloaded banking document and handlers', async () => {
  const start = source.indexOf("for(const name of ['Generated Alex'");
  const end = source.indexOf('\n  assert.equal', start);
  assert.ok(start > 0 && end > start);
  let inspected = 0;
  const states = name => [
    {readyState: 'loading', body: {innerText: name}, querySelector: () => null},
    {readyState: 'complete', body: {innerText: name}, querySelector: selector => selector.includes('banking-ready') ? null : {value: ''}},
    {readyState: 'complete', body: {innerText: name}, querySelector: () => ({value: ''})},
  ];
  let member = 0;
  await vm.runInNewContext(`(async()=>{${source.slice(start, end)}})()`, {
    set: async () => {}, click: async () => {},
    c: {until: async expression => {
      const name = ['Generated Alex', 'Generated Blair'][member++];
      for (const [i, document] of states(name).entries()) {
        const accepted = Boolean(vm.runInNewContext(expression, {document}));
        assert.equal(accepted, i === 2, 'intermediate reload must remain pending');
        inspected++;
      }
    }},
  });
  assert.equal(inspected, 6);
});
