const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../browser/banking.cjs'), 'utf8');
const start = source.indexOf('async function click(selector)');
const end = source.indexOf('async function set(selector', start);
const definition = source.slice(start, end);

test('banking journey activates the measured mounted control through native pointer input', async () => {
  const events = [];
  const expressions = [];
  const c = {
    async eval(expression) {
      expressions.push(expression);
      return {x: 68, y: 92};
    },
    async call(method, params) {
      events.push({method, params});
    },
  };
  await vm.runInNewContext(`(async () => { ${definition}; await click('#generated-control'); })()`, {c});
  assert.deepEqual(events.map(event => event.method), ['Input.dispatchMouseEvent', 'Input.dispatchMouseEvent']);
  assert.deepEqual(events.map(event => event.params.type), ['mousePressed', 'mouseReleased']);
  assert.ok(events.every(event => event.params.x === 68 && event.params.y === 92));
  assert.ok(expressions.some(expression => expression.includes('scrollIntoView')));
  assert.ok(expressions.some(expression => expression.includes('getBoundingClientRect')));
  assert.ok(expressions.every(expression => !expression.includes('.click()')));
});
