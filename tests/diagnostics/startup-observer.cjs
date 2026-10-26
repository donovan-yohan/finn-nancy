'use strict';
// Generated-test diagnostic preload. Does not change browser argv, polling, or budgets.
const fs = require('node:fs');
const cp = require('node:child_process');
const path = require('node:path');
const {performance} = require('node:perf_hooks');
const read = fs.readFileSync.bind(fs);
const events = [];
const start = performance.now();
const output = process.env.FN_STARTUP_OBSERVATION;
let browser;
let stderrPath;
let captured = false;
function record(kind, fields = {}) {
  events.push({kind, elapsedMs: performance.now() - start, ...fields});
}
function safeRead(filename, limit = 4096) {
  try { return read(filename, 'utf8').slice(0, limit); }
  catch (error) { return {unavailable: error.code}; }
}
function processTree(pid) {
  const pending = [String(pid)], seen = new Set(), processes = [];
  while (pending.length && seen.size < 32) {
    const current = pending.shift();
    if (seen.has(current)) continue;
    seen.add(current);
    const root = '/proc/' + current;
    let tids = [];
    try { tids = fs.readdirSync(root + '/task').slice(0, 64); } catch (_) {}
    const tasks = tids.map(tid => {
      const task = root + '/task/' + tid;
      const children = safeRead(task + '/children');
      if (typeof children === 'string') pending.push(...children.trim().split(/\s+/).filter(Boolean));
      return {tid, comm: safeRead(task + '/comm', 128), stat: safeRead(task + '/stat'),
        wchan: safeRead(task + '/wchan'), schedstat: safeRead(task + '/schedstat'),
        syscall: safeRead(task + '/syscall')};
    });
    const environment = safeRead(root + '/environ', 1048576);
    const session = typeof environment === 'string'
      ? environment.split('\0').find(value => value.startsWith('DBUS_SESSION_BUS_ADDRESS=')) ?? null
      : environment;
    processes.push({pid: current, status: safeRead(root + '/status'), io: safeRead(root + '/io'),
      sessionBusAddress: session, tasks});
  }
  return processes;
}
function snapshot(boundary) {
  if (captured || !browser) return;
  captured = true;
  const state = {boundary, pid: browser.pid, exitCode: browser.exitCode,
    signalCode: browser.signalCode, observerUsage: process.resourceUsage()};
  if (process.platform === 'linux' && browser.pid) {
    state.processes = processTree(browser.pid);
    state.host = Object.fromEntries([
      '/proc/loadavg', '/proc/meminfo', '/proc/pressure/cpu', '/proc/pressure/io',
      '/proc/pressure/memory', '/sys/fs/cgroup/cpu.stat', '/sys/fs/cgroup/memory.events',
    ].map(filename => [filename, safeRead(filename)]));
  }
  record('readiness-boundary-snapshot', state);
}
const spawn = cp.spawn;
cp.spawn = function(executable, args, options) {
  browser = spawn.call(this, executable, args, options);
  const profile = args.find(arg => arg.startsWith('--user-data-dir='))?.split('=').slice(1).join('=');
  stderrPath = profile && profile + '.stderr.log';
  record('spawn-requested', {executable, args, pid: browser.pid,
    inputSessionBusPresent: Object.hasOwn(process.env, 'DBUS_SESSION_BUS_ADDRESS')});
  browser.once('spawn', () => record('spawned', {pid: browser.pid}));
  browser.once('error', error => record('spawn-error', {code: error.code, message: error.message}));
  browser.once('close', (code, signal) => record('closed', {code, signal}));
  const kill = browser.kill;
  browser.kill = function(signal) { record('kill-requested', {signal}); return kill.call(this, signal); };
  return browser;
};
// startupError reads stderr before finally sends SIGTERM. Sample only there or
// after target publication, never while the browser is racing the startup clock.
fs.readFileSync = function(filename, ...args) {
  if (String(filename) === stderrPath) snapshot('startup-error-before-cleanup');
  return read(filename, ...args);
};
const fetch = global.fetch;
global.fetch = async function(url, options) {
  const requested = performance.now();
  try {
    const response = await fetch.call(this, url, options);
    record('readiness-headers', {requestMs: performance.now() - requested, status: response.status});
    const json = response.json.bind(response);
    response.json = async function() {
      try {
        const targets = await json();
        record('readiness-body', {requestMs: performance.now() - requested,
          targetTypes: Array.isArray(targets) ? targets.map(target => target.type) : null});
        if (Array.isArray(targets) && targets.some(target => target.type === 'page')) snapshot('page-target-ready');
        return targets;
      } catch (error) {
        record('readiness-body-error', {requestMs: performance.now() - requested, name: error.name});
        throw error;
      }
    };
    return response;
  } catch (error) {
    record('readiness-fetch-error', {requestMs: performance.now() - requested,
      name: error.name, causeCode: error.cause?.code ?? null});
    throw error;
  }
};
record('observer-loaded', {node: process.version, platform: process.platform, wallTime: new Date().toISOString()});
process.on('exit', code => {
  record('observer-exit', {code});
  if (output) fs.writeFileSync(output, JSON.stringify({events}, null, 2) + '\n', {mode: 0o600});
});
