const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../browser/workspace_ui.cjs'), 'utf8');

test('workspace disclosure uses a complete printable Space event', async () => {
  const start = source.indexOf("await cdp.call('Input.dispatchKeyEvent'", source.indexOf('const density=[]'));
  const end = source.indexOf('\n      await cdp.until', start);
  assert.ok(start > 0 && end > start);
  const packets = [];
  await vm.runInNewContext(`(async()=>{${source.slice(start, end)}})()`, {
    cdp: {call: async (method, params) => packets.push(JSON.parse(JSON.stringify(params)))},
  });
  assert.deepEqual(packets, [
    {type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49, text: ' ', unmodifiedText: ' '},
    {type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 49},
  ]);
});
