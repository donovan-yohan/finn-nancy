'use strict';
// Diagnostic preload only: preserve spawn argv/stdio, fetch options and deadlines.
const fs = require('node:fs');
const cp = require('node:child_process');
const dc = require('node:diagnostics_channel');
const path = require('node:path');
const {performance} = require('node:perf_hooks');
const read = fs.readFileSync.bind(fs);
const started = performance.now();
const events = [];
// Poll traffic must not consume the sole boundary snapshot or terminal receipt.
const reservedKinds = new Set(['readiness-boundary-snapshot', 'observer-exit']);
const reservedEvents = new Map();
const requests = new WeakMap();
let sequence = 0, activeRequest, browser, stderrPath, watcher, port;
let captured = false, stderrOffset = 0, stderrTail = '', announcement = false;
let droppedEvents = 0;
const limit = 512;
function record(kind, fields = {}) {
  const event = {kind, elapsedMs: performance.now() - started, ...fields};
  if (reservedKinds.has(kind)) reservedEvents.set(kind, event);
  else if (events.length < limit - reservedKinds.size) events.push(event);
  else droppedEvents++;
}
function safeRead(filename, max = 4096) {
  try { return read(filename, 'utf8').slice(0, max); }
  catch (error) { return {unavailable: error.code}; }
}
function classify(filename) {
  if (!filename) return 'anonymous';
  const runtime = process.env.TMPDIR;
  if (runtime && (filename === runtime || filename.startsWith(runtime + path.sep))) return 'generated-runtime';
  if (/^\/(opt\/google\/chrome|Applications\/Google Chrome.app)\//.test(filename)) return 'browser-install';
  if (/^\/(usr\/)?lib(64)?\//.test(filename)) return 'system-library';
  return 'other-redacted';
}
function instructionMapping(syscall, maps) {
  if (typeof syscall !== 'string' || typeof maps !== 'string') return null;
  const fields = syscall.trim().split(/\s+/);
  const ip = fields.at(-1);
  if (!/^0x[0-9a-f]+$/i.test(ip)) return null;
  const address = BigInt(ip);
  for (const line of maps.split('\n')) {
    const m = line.match(/^([0-9a-f]+)-([0-9a-f]+)\s+(\S+)\s+([0-9a-f]+)\s+\S+\s+\d+\s*(.*)$/);
    if (!m) continue;
    const low = BigInt('0x' + m[1]), high = BigInt('0x' + m[2]);
    if (low <= address && address < high) {
      return {class: classify(m[5]), basename: classify(m[5]) === 'other-redacted' ? null : path.basename(m[5]),
        permissions: m[3], fileOffset: '0x' + (address - low + BigInt('0x' + m[4])).toString(16)};
    }
  }
  return {unavailable: 'mapping-not-found'};
}
function processTree(pid) {
  const pending = [String(pid)], seen = new Set(), processes = [];
  while (pending.length && seen.size < 32) {
    const current = pending.shift();
    if (seen.has(current)) continue;
    seen.add(current);
    const root = '/proc/' + current;
    let allTids = [];
    try { allTids = fs.readdirSync(root + '/task'); } catch (_) {}
    const tids = allTids.slice(0, 64);
    // Read maps locally only to attribute an instruction address; never upload maps.
    const maps = safeRead(root + '/maps', 1048576);
    const tasks = tids.map(tid => {
      const task = root + '/task/' + tid;
      const children = safeRead(task + '/children');
      if (typeof children === 'string') pending.push(...children.trim().split(/\s+/).filter(Boolean));
      const syscall = safeRead(task + '/syscall');
      return {tid, comm: safeRead(task + '/comm', 128), stat: safeRead(task + '/stat'),
        wchan: safeRead(task + '/wchan'), schedstat: safeRead(task + '/schedstat'), syscall,
        instructionMapping: instructionMapping(syscall, maps)};
    });
    processes.push({pid: current, status: safeRead(root + '/status'), io: safeRead(root + '/io'),
      omittedTasks: Math.max(0, allTids.length - tids.length), tasks});
  }
  return {processes, pendingProcesses: pending.length};
}
function sampleStderr(source = 'boundary') {
  if (!stderrPath || announcement || stderrOffset >= 65536) return;
  try {
    const fd = fs.openSync(stderrPath, 'r');
    try {
      const chunk = Buffer.alloc(4096);
      while (!announcement && stderrOffset < 65536) {
        const count = fs.readSync(fd, chunk, 0, Math.min(chunk.length, 65536 - stderrOffset), stderrOffset);
        if (!count) break;
        stderrOffset += count;
        const text = stderrTail + chunk.subarray(0, count).toString('utf8');
        stderrTail = text.slice(-512);
        if (/DevTools listening on ws:\/\//.test(text)) {
          announcement = true;
          record('listener-announcement-observed', {source,
            clockMeaning: 'parent receipt upper bound, not child emission time'});
        }
      }
    } finally { fs.closeSync(fd); }
  } catch (error) { record('stderr-observation-error', {code: error.code}); }
}
function snapshot(boundary) {
  if (captured || !browser) return;
  captured = true;
  sampleStderr();
  const begin = performance.now();
  const state = {boundary, boundaryElapsedMs: begin - started,
    pid: browser.pid, exitCode: browser.exitCode, signalCode: browser.signalCode};
  if (process.platform === 'linux' && browser.pid) {
    Object.assign(state, processTree(browser.pid));
    state.host = Object.fromEntries([
      '/proc/loadavg', '/proc/pressure/cpu', '/proc/pressure/io', '/proc/pressure/memory',
      '/sys/fs/cgroup/cpu.stat', '/sys/fs/cgroup/memory.events',
    ].map(filename => [filename, safeRead(filename)]));
  }
  record('readiness-boundary-snapshot', {...state, snapshotDurationMs: performance.now() - begin,
    observerUsage: process.resourceUsage()});
}
const spawn = cp.spawn;
cp.spawn = function(executable, args, options) {
  // The shipping harness spawns only Chrome. Refuse accidental observation of another child.
  if (!args?.some(arg => arg.startsWith('--remote-debugging-port='))) return spawn.call(this, executable, args, options);
  const requested = performance.now();
  browser = spawn.call(this, executable, args, options);
  port = Number(args.find(arg => arg.startsWith('--remote-debugging-port=')).split('=')[1]);
  const profile = args.find(arg => arg.startsWith('--user-data-dir=')).slice('--user-data-dir='.length);
  stderrPath = profile + '.stderr.log';
  record('spawn-requested', {executable, args, pid: browser.pid, spawnCallMs: performance.now() - requested,
    inputSessionBusPresent: Object.hasOwn(process.env, 'DBUS_SESSION_BUS_ADDRESS'),
    stderrStdioPreserved: Number.isInteger(options?.stdio?.[2])});
  browser.once('spawn', () => record('spawned', {pid: browser.pid}));
  browser.once('error', error => record('spawn-error', {code: error.code}));
  browser.once('close', (code, signal) => {
    sampleStderr(); watcher?.close(); record('closed', {code, signal});
  });
  try {
    // Watch the existing regular file; never replace Chrome's fd with a pipe.
    watcher = fs.watch(stderrPath, () => sampleStderr('file-watch'));
    watcher.unref();
  } catch (error) { record('stderr-watch-unavailable', {code: error.code}); }
  const kill = browser.kill;
  browser.kill = function(signal) { record('kill-requested', {signal}); return kill.call(this, signal); };
  return browser;
};
fs.readFileSync = function(filename, ...args) {
  if (String(filename) === stderrPath) snapshot('startup-error-before-cleanup');
  return read(filename, ...args);
};
function readinessRequest(request) {
  return request?.path === '/json/list' && String(request.origin) === `http://127.0.0.1:${port}`;
}
for (const [channel, kind] of [['undici:request:create', 'http-request-created'],
  ['undici:client:sendHeaders', 'http-request-send-headers'], ['undici:request:headers', 'http-response-headers']]) {
  dc.channel(channel).subscribe(message => {
    if (!readinessRequest(message.request)) return;
    if (kind === 'http-request-created') requests.set(message.request, activeRequest);
    record(kind, {requestId: requests.get(message.request) ?? null});
  });
}
dc.channel('undici:client:connected').subscribe(({connectParams}) => {
  // Undici emits this after connectH1/parser initialization, not at the raw TCP SYN/ACK.
  if (!captured && connectParams.hostname === '127.0.0.1' && Number(connectParams.port) === port) record('http-client-connected');
});
const fetch = global.fetch;
global.fetch = async function(url, options) {
  if (String(url) !== `http://127.0.0.1:${port}/json/list`) return fetch.call(this, url, options);
  const requestId = ++sequence, requested = performance.now();
  record('readiness-request', {requestId});
  let pending;
  activeRequest = requestId;
  try { pending = fetch.call(this, url, options); } finally { activeRequest = undefined; }
  try {
    const response = await pending;
    record('readiness-headers', {requestId, requestMs: performance.now() - requested, status: response.status});
    const json = response.json.bind(response);
    response.json = async function() {
      try {
        const targets = await json();
        record('readiness-body', {requestId, requestMs: performance.now() - requested,
          targetTypes: Array.isArray(targets) ? targets.map(target => target.type) : null});
        if (Array.isArray(targets) && targets.some(target => target.type === 'page')) snapshot('page-target-ready');
        return targets;
      } catch (error) {
        record('readiness-body-error', {requestId, name: error.name}); throw error;
      }
    };
    return response;
  } catch (error) {
    record('readiness-fetch-error', {requestId, requestMs: performance.now() - requested,
      name: error.name, causeCode: error.cause?.code ?? null});
    throw error;
  }
};
const WebSocket = global.WebSocket;
if (WebSocket) global.WebSocket = new Proxy(WebSocket, {construct(target, args, newTarget) {
  record('page-websocket-handoff');
  return Reflect.construct(target, args, newTarget);
}});
record('observer-loaded', {node: process.version, platform: process.platform, wallTime: new Date().toISOString()});
process.on('exit', code => {
  watcher?.close();
  record('observer-exit', {code});
  if (process.env.FN_STARTUP_OBSERVATION) fs.writeFileSync(process.env.FN_STARTUP_OBSERVATION,
    JSON.stringify({schema: 1, droppedEvents,
      events: [...events, ...reservedEvents.values()].sort((a, b) => a.elapsedMs - b.elapsedMs)},
    null, 2) + '\n', {mode: 0o600});
});
// Pure parser exports for generated controls; requiring this file is the preload.
module.exports = {instructionMapping, classify};
