const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

// Exercise the harness class without starting its fixture server or Chrome.
const source = fs.readFileSync(path.join(__dirname, '../browser/plaid_onboarding.cjs'), 'utf8');
const classSource = source.slice(source.indexOf('class Cdp {'), source.indexOf('\nasync function firstRun'));

test('owned child cleanup escalates and awaits exit with a deadline', async () => {
  const cleanupSource = source.slice(source.indexOf('async function stopChild('), source.indexOf('\nclass Cdp {'));
  assert.ok(cleanupSource, 'bounded owned-child cleanup exists');
  const {EventEmitter} = require('node:events');
  const timers = new Map();
  let nextId = 0;
  const stopChild = vm.runInNewContext(`${cleanupSource}; stopChild`, {
    setTimeout: callback => { timers.set(++nextId, callback); return nextId; },
    clearTimeout: id => timers.delete(id),
  });
  const child = Object.assign(new EventEmitter(), {exitCode: null, signalCode: null});
  const signals = [];
  child.kill = signal => { signals.push(signal); };
  let finished = false;
  const cleanup = stopChild(child).then(() => { finished = true; });
  assert.deepEqual(signals, ['SIGTERM']);
  timers.get(nextId)();
  assert.deepEqual(signals, ['SIGTERM', 'SIGKILL']);
  await Promise.resolve();
  assert.equal(finished, false, 'sending kill is not proof of exit');
  child.signalCode = 'SIGKILL';
  child.emit('exit');
  await cleanup;
  assert.equal(finished, true);
  assert.equal(child.listenerCount('exit'), 0);
  await stopChild(child);
  assert.equal(signals.length, 2, 'already-exited child is not signalled again');

  const stuck = Object.assign(new EventEmitter(), {exitCode: null, signalCode: null, kill() {}});
  const rejected = assert.rejects(stopChild(stuck), /Owned child did not exit/);
  timers.get(nextId)();
  timers.get(nextId)();
  await rejected;
  assert.equal(stuck.listenerCount('exit'), 0);
});

test('Space supplies a printable native keyDown and a matching keyUp', async () => {
  const messages = [];
  class Socket {
    constructor() { queueMicrotask(() => this.onopen()); }
    send(message) {
      const packet = JSON.parse(message);
      messages.push(packet.params);
      queueMicrotask(() => this.onmessage({data: JSON.stringify({id: packet.id, result: {}})}));
    }
  }
  const Cdp = vm.runInNewContext(`${classSource}; Cdp`, {WebSocket: Socket, setTimeout, clearTimeout});
  const cdp = new Cdp('ws://generated-only');
  await cdp.key(' ', 'Space', 32, 49);
  assert.deepEqual(messages, [
    {type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49, text: ' ', unmodifiedText: ' '},
    {type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49},
  ]);
});

for (const lateResponse of [{result: {}}, {error: {message: 'late protocol error'}}]) {
  test(`late CDP ${lateResponse.error ? 'error' : 'success'} after timeout is ignored`, async () => {
    const timers = new Map();
    let timerId = 0;
    class Socket {
      constructor() { queueMicrotask(() => this.onopen()); }
      send(message) { this.sent = JSON.parse(message); }
    }
    const Cdp = vm.runInNewContext(`${classSource}; Cdp`, {
      WebSocket: Socket,
      setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
      clearTimeout: id => timers.delete(id),
    });
    const cdp = new Cdp('ws://generated-only');
    await cdp.ready;
    const expired = cdp.call('Runtime.evaluate');
    const rejected = assert.rejects(expired, /CDP timeout Runtime.evaluate/);
    await Promise.resolve();
    const expiredId = cdp.ws.sent.id;
    timers.get(timerId)();
    await rejected;
    assert.equal(cdp.pending.size, 0);
    assert.doesNotThrow(() => cdp.ws.onmessage({data: JSON.stringify({id: expiredId, ...lateResponse})}));

    // Ignoring stale replies must not consume an unrelated live request.
    const live = cdp.call('Browser.getVersion');
    await Promise.resolve();
    const liveId = cdp.ws.sent.id;
    cdp.ws.onmessage({data: JSON.stringify({id: expiredId, ...lateResponse})});
    assert.equal(cdp.pending.size, 1);
    cdp.ws.onmessage({data: JSON.stringify({id: liveId, result: {product: 'generated'}})});
    assert.equal((await live).product, 'generated');
    assert.equal(cdp.pending.size, 0);
  });
}
