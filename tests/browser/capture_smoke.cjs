const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const baseUrl = process.argv[2] || "http://127.0.0.1:8776";
const uploadPath = process.argv[3] || "app/web/static/icon-192.png";
const uploadFile = path.resolve(process.cwd(), uploadPath);
const chromiumBin = process.env.CHROMIUM_BIN || "chromium";

function pause(milliseconds) {
  return new Promise(function (resolve) {
    setTimeout(resolve, milliseconds);
  });
}

async function freePort() {
  const server = net.createServer();
  await new Promise(function (resolve, reject) {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  await new Promise(function (resolve) {
    server.close(resolve);
  });
  return port;
}

async function waitForJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return response.json();
    } catch (error) {
      lastError = error;
    }
    await pause(50);
  }
  throw lastError || new Error("timed out waiting for " + url);
}

class Cdp {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.waiters = new Map();
    this.socket = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) {
          pending.reject(
            new Error(message.error.message || JSON.stringify(message.error))
          );
        } else {
          pending.resolve(message.result || {});
        }
        return;
      }
      const listeners = this.waiters.get(message.method) || [];
      this.waiters.delete(message.method);
      for (const listener of listeners) listener.resolve(message.params || {});
    });
  }

  async call(method, params) {
    await this.ready;
    const id = this.nextId++;
    const result = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.socket.send(JSON.stringify({ id, method, params: params || {} }));
    return result;
  }

  event(method, timeoutMs) {
    const timeout = timeoutMs || 10_000;
    return new Promise((resolve, reject) => {
      const listeners = this.waiters.get(method) || [];
      const waiter = {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
      };
      listeners.push(waiter);
      this.waiters.set(method, listeners);
      const timer = setTimeout(() => {
        const current = this.waiters.get(method) || [];
        this.waiters.set(
          method,
          current.filter((item) => item !== waiter)
        );
        reject(new Error("timed out waiting for CDP event " + method));
      }, timeout);
    });
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(cdp, body) {
  const response = await cdp.call("Runtime.evaluate", {
    expression: "(async function () {" + body + "})()",
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    throw new Error(
      response.exceptionDetails.text ||
        JSON.stringify(response.exceptionDetails.exception)
    );
  }
  return response.result && response.result.value;
}

async function navigate(cdp, url, reload) {
  const loaded = cdp.event("Page.loadEventFired", 15_000);
  if (reload) {
    await cdp.call("Page.reload", { ignoreCache: false });
  } else {
    await cdp.call("Page.navigate", { url });
  }
  await loaded;
}

async function waitForValue(cdp, body, predicate, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let latest;
  while (Date.now() < deadline) {
    latest = await evaluate(cdp, body);
    if (predicate(latest)) return latest;
    await pause(50);
  }
  throw new Error("browser condition timed out; latest=" + JSON.stringify(latest));
}

async function main() {
  assert.equal(fs.existsSync(uploadFile), true, "upload fixture does not exist");
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "fn145-chromium-"));
  const browser = spawn(
    chromiumBin,
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=" + port,
      "--user-data-dir=" + profile,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] }
  );
  let browserErrors = "";
  browser.stderr.on("data", function (chunk) {
    browserErrors += String(chunk);
  });

  let cdp;
  try {
    const pages = await waitForJson(
      "http://127.0.0.1:" + port + "/json/list",
      10_000
    );
    const page = pages.find(function (candidate) {
      return candidate.type === "page";
    });
    assert.ok(page && page.webSocketDebuggerUrl, "Chromium page target missing");
    cdp = new Cdp(page.webSocketDebuggerUrl);
    await Promise.all([
      cdp.call("Page.enable"),
      cdp.call("Runtime.enable"),
      cdp.call("DOM.enable"),
      cdp.call("Network.enable"),
      cdp.call("Accessibility.enable"),
    ]);
    await cdp.call("Emulation.setDeviceMetricsOverride", {
      width: 390,
      height: 844,
      deviceScaleFactor: 3,
      mobile: true,
    });
    await cdp.call("Emulation.setTouchEmulationEnabled", {
      enabled: true,
      maxTouchPoints: 5,
    });
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", {
      source: [
        "try {",
        "  Object.defineProperty(ServiceWorkerRegistration.prototype, 'sync', {",
        "    configurable: true,",
        "    get: function () { return undefined; }",
        "  });",
        "} catch (_error) {}",
      ].join("\n"),
    });

    const url = baseUrl + "/upload?mode=camera&intent=receipt";
    await navigate(cdp, url, false);
    await evaluate(
      cdp,
      "await navigator.serviceWorker.ready; return true;"
    );
    await navigate(cdp, url, true);
    const controlled = await waitForValue(
      cdp,
      "return Boolean(navigator.serviceWorker.controller);",
      Boolean,
      5_000
    );
    assert.equal(controlled, true, "offline reload requires an active controller");

    const disabledBackgroundSync = await evaluate(
      cdp,
      [
        "try {",
        "  Object.defineProperty(ServiceWorkerRegistration.prototype, 'sync', {",
        "    configurable: true,",
        "    get: function () { return undefined; }",
        "  });",
        "  return true;",
        "} catch (_error) { return false; }",
      ].join("\n")
    );
    assert.equal(disabledBackgroundSync, true);

    await cdp.call("Network.emulateNetworkConditions", {
      offline: true,
      latency: 0,
      downloadThroughput: 0,
      uploadThroughput: 0,
      connectionType: "none",
    });
    await evaluate(
      cdp,
      "window.dispatchEvent(new Event('offline')); return navigator.onLine;"
    );

    const selectedBatchSize = await evaluate(
      cdp,
      [
        "const transfer = new DataTransfer();",
        "for (let index = 0; index < 21; index += 1) {",
        "  transfer.items.add(new File(",
        "    [new Uint8Array([index, 21, 145, 1])],",
        "    'batch-' + index + '.jpg',",
        "    { type: 'image/jpeg' }",
        "  ));",
        "}",
        "const input = document.querySelector('#capture-files-input');",
        "input.files = transfer.files;",
        "input.dispatchEvent(new Event('change', { bubbles: true }));",
        "return input.files.length;",
      ].join("\n")
    );
    assert.equal(selectedBatchSize, 21);

    const pendingBatch = await waitForValue(
      cdp,
      [
        "const records = await FinnCaptureStore.listCaptures();",
        "return {",
        "  records: records.length,",
        "  deviceOwned: records.filter(function (record) { return Boolean(record.file); }).length,",
        "  uniqueIds: new Set(records.map(function (record) { return record.id; })).size,",
        "  rendered: document.querySelectorAll('.capture-status-item').length,",
        "  visibleCount: document.querySelector('#capture-count').innerText",
        "};",
      ].join("\n"),
      function (value) {
        return (
          value &&
          value.records === 21 &&
          value.deviceOwned === 21 &&
          value.uniqueIds === 21 &&
          value.rendered === 21 &&
          value.visibleCount === "21"
        );
      },
      10_000
    );
    assert.equal(pendingBatch.records, 21);

    await cdp.call("Network.emulateNetworkConditions", {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
      connectionType: "wifi",
    });
    await evaluate(
      cdp,
      "window.dispatchEvent(new Event('online')); return navigator.onLine;"
    );

    const durableBatch = await waitForValue(
      cdp,
      [
        "const records = await FinnCaptureStore.listCaptures();",
        "return {",
        "  records: records.length,",
        "  durable: records.filter(function (record) { return Boolean(record.durableAt) && !record.file; }).length,",
        "  documentIds: new Set(records.map(function (record) { return record.serverDocumentId; })).size",
        "};",
      ].join("\n"),
      function (value) {
        return (
          value &&
          value.records === 21 &&
          value.durable === 21 &&
          value.documentIds === 21
        );
      },
      30_000
    );
    assert.equal(durableBatch.durable, 21);

    const batchServerStatuses = await evaluate(
      cdp,
      [
        "const records = await FinnCaptureStore.listCaptures();",
        "const statuses = await Promise.all(records.map(async function (record) {",
        "  const response = await fetch('/captures/' + encodeURIComponent(record.id));",
        "  return response.ok ? response.json() : null;",
        "}));",
        "return {",
        "  count: statuses.length,",
        "  durable: statuses.filter(function (status) { return status && status.durable === true; }).length",
        "};",
      ].join("\n")
    );
    assert.deepEqual(batchServerStatuses, { count: 21, durable: 21 });

    const separatedReliability = await waitForValue(
      cdp,
      [
        "const records = await FinnCaptureStore.listCaptures();",
        "const response = await fetch('/capture/metrics');",
        "const metrics = await response.json();",
        "return {",
        "  online: records.filter(function (record) { return record.onlineDurableAckMs !== null && Number.isFinite(Number(record.onlineDurableAckMs)); }).length,",
        "  offline: records.filter(function (record) { return record.offlineRecoveryMs !== null && Number.isFinite(Number(record.offlineRecoveryMs)); }).length,",
        "  onlineRecorded: records.filter(function (record) { return record.onlineAckTelemetryRecorded === true; }).length,",
        "  offlineRecorded: records.filter(function (record) { return record.offlineTelemetryRecorded === true; }).length,",
        "  metricOnline: metrics.online_durable_ack.count,",
        "  metricOffline: metrics.offline_recovery.count",
        "};",
      ].join("\n"),
      function (value) {
        return (
          value &&
          value.online === 21 &&
          value.offline === 21 &&
          value.onlineRecorded === 21 &&
          value.offlineRecorded === 21 &&
          value.metricOnline === 21 &&
          value.metricOffline === 21
        );
      },
      10_000
    );
    assert.equal(separatedReliability.metricOnline, 21);
    assert.equal(separatedReliability.metricOffline, 21);

    await evaluate(
      cdp,
      [
        "const records = await FinnCaptureStore.listCaptures();",
        "for (const record of records) await FinnCaptureStore.deleteCapture(record.id);",
        "return (await FinnCaptureStore.listCaptures()).length;",
      ].join("\n")
    );
    await cdp.call("Network.emulateNetworkConditions", {
      offline: true,
      latency: 0,
      downloadThroughput: 0,
      uploadThroughput: 0,
      connectionType: "none",
    });
    await evaluate(
      cdp,
      "window.dispatchEvent(new Event('offline')); return navigator.onLine;"
    );

    const documentNode = await cdp.call("DOM.getDocument", {
      depth: -1,
      pierce: true,
    });
    const inputNode = await cdp.call("DOM.querySelector", {
      nodeId: documentNode.root.nodeId,
      selector: "#capture-page-files",
    });
    assert.ok(inputNode.nodeId, "capture file input missing");
    await cdp.call("DOM.setFileInputFiles", {
      nodeId: inputNode.nodeId,
      files: [uploadFile],
    });
    await evaluate(
      cdp,
      "document.querySelector('[data-capture-form]').requestSubmit(); return true;"
    );

    const pending = await waitForValue(
      cdp,
      [
        "const records = await FinnCaptureStore.listCaptures();",
        "if (records.length !== 1) return null;",
        "const record = records[0];",
        "return {",
        "  id: record.id,",
        "  state: record.state,",
        "  fileSize: record.file && record.file.size,",
        "  persistence: record.storagePersistence,",
        "  statusText: document.querySelector('#capture-center').innerText,",
        "  retryCount: document.querySelectorAll('.capture-status-actions button').length",
        "};",
      ].join("\n"),
      function (value) {
        return (
          value &&
          value.state === "pending" &&
          value.fileSize > 0 &&
          /Pending/.test(value.statusText) &&
          value.retryCount >= 1
        );
      },
      5_000
    );
    assert.match(
      pending.id,
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
    );
    assert.match(pending.statusText, /Pending/);
    assert.match(pending.statusText, /Background retry is unavailable/);
    assert.ok(pending.retryCount >= 1, "manual Retry must remain visible");
    assert.ok(
      ["persistent", "best_effort", "unsupported"].includes(pending.persistence)
    );
    if (pending.persistence !== "persistent") {
      assert.match(pending.statusText, /not protected from eviction/);
    }

    await navigate(cdp, url, true);
    const afterReload = await waitForValue(
      cdp,
      [
        "const records = await FinnCaptureStore.listCaptures();",
        "if (records.length !== 1) return null;",
        "return {",
        "  id: records[0].id,",
        "  state: records[0].state,",
        "  fileSize: records[0].file && records[0].file.size,",
        "  controlled: Boolean(navigator.serviceWorker.controller),",
        "  heading: document.querySelector('h1') && document.querySelector('h1').innerText",
        "};",
      ].join("\n"),
      function (value) {
        return value && value.state === "pending";
      },
      5_000
    );
    assert.equal(afterReload.id, pending.id);
    assert.equal(afterReload.fileSize, pending.fileSize);
    assert.equal(afterReload.controlled, true);
    assert.match(afterReload.heading, /Capture a receipt/);

    await evaluate(
      cdp,
      [
        "Object.defineProperty(ServiceWorkerRegistration.prototype, 'sync', {",
        "  configurable: true,",
        "  get: function () { return undefined; }",
        "});",
        "window.__capturePostIds = [];",
        "window.__dropFirstCaptureAck = true;",
        "const originalFetch = window.fetch.bind(window);",
        "window.fetch = async function (input, init) {",
        "  const method = String((init && init.method) || 'GET').toUpperCase();",
        "  const target = new URL(typeof input === 'string' ? input : input.url, location.href);",
        "  if (method === 'POST' && target.pathname === '/captures') {",
        "    const captureId = init.body.get('client_capture_id');",
        "    window.__capturePostIds.push(captureId);",
        "    if (window.__dropFirstCaptureAck) {",
        "      window.__dropFirstCaptureAck = false;",
        "      const committed = await originalFetch(input, init);",
        "      await committed.clone().json();",
        "      throw new DOMException('simulated acknowledgement loss', 'AbortError');",
        "    }",
        "  }",
        "  return originalFetch(input, init);",
        "};",
        "return true;",
      ].join("\n")
    );
    const reconnectedAt = Date.now();
    await cdp.call("Network.emulateNetworkConditions", {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
      connectionType: "wifi",
    });
    await evaluate(
      cdp,
      "window.dispatchEvent(new Event('online')); return navigator.onLine;"
    );

    const lostAcknowledgement = await waitForValue(
      cdp,
      [
        "const record = (await FinnCaptureStore.listCaptures())[0];",
        "return {",
        "  state: record && record.state,",
        "  fileSize: record && record.file && record.file.size,",
        "  error: record && record.lastError,",
        "  postIds: window.__capturePostIds.slice()",
        "};",
      ].join("\n"),
      function (value) {
        return value && value.state === "retry";
      },
      5_000
    );
    assert.equal(lostAcknowledgement.fileSize, pending.fileSize);
    assert.match(lostAcknowledgement.error, /timed out/);
    assert.deepEqual(lostAcknowledgement.postIds, [pending.id]);

    const acknowledged = await waitForValue(
      cdp,
      [
        "const record = (await FinnCaptureStore.listCaptures())[0];",
        "return {",
        "  id: record && record.id,",
        "  state: record && record.state,",
        "  hasFile: Boolean(record && record.file),",
        "  durableAt: record && record.durableAt,",
        "  documentId: record && record.serverDocumentId,",
        "  postIds: window.__capturePostIds.slice(),",
        "  statusText: document.querySelector('#capture-center').innerText",
        "};",
      ].join("\n"),
      function (value) {
        return value && value.durableAt && !value.hasFile;
      },
      12_000
    );
    const acknowledgementMs = Date.now() - reconnectedAt;
    assert.equal(acknowledged.id, pending.id);
    assert.ok(acknowledged.documentId > 0);
    assert.deepEqual(acknowledged.postIds, [pending.id, pending.id]);
    assert.ok(
      acknowledgementMs <= 10_000,
      "durable acknowledgement exceeded 10 seconds"
    );

    const serverStatus = await evaluate(
      cdp,
      [
        "const response = await fetch('/captures/" + pending.id + "');",
        "return response.json();",
      ].join("\n")
    );
    assert.equal(serverStatus.client_capture_id, pending.id);
    assert.equal(serverStatus.durable, true);
    assert.equal(serverStatus.source_document_id, acknowledged.documentId);

    const mobileUx = await evaluate(
      cdp,
      [
        "document.querySelector('details.fab').open = true;",
        "const pickerRects = Array.from(document.querySelectorAll('[data-capture-picker]')).map(function (button) {",
        "  const rect = button.getBoundingClientRect();",
        "  return { name: button.innerText.trim(), left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height, tag: button.tagName };",
        "});",
        "const panel = document.querySelector('#capture-center').getBoundingClientRect();",
        "const toggle = document.querySelector('#capture-toggle').getBoundingClientRect();",
        "return {",
        "  pickerRects: pickerRects,",
        "  panel: { left: panel.left, right: panel.right, top: panel.top, bottom: panel.bottom },",
        "  toggle: { width: toggle.width, height: toggle.height },",
        "  overflow: document.documentElement.scrollWidth - window.innerWidth,",
        "  liveRole: document.querySelector('#capture-announcer').getAttribute('role'),",
        "  liveMode: document.querySelector('#capture-announcer').getAttribute('aria-live')",
        "};",
      ].join("\n")
    );
    assert.equal(mobileUx.overflow <= 0, true);
    assert.equal(mobileUx.liveRole, "status");
    assert.equal(mobileUx.liveMode, "polite");
    assert.ok(mobileUx.toggle.width >= 44);
    assert.ok(mobileUx.toggle.height >= 44);
    for (const control of mobileUx.pickerRects) {
      assert.equal(control.tag, "BUTTON");
      assert.ok(control.height >= 44, control.name + " touch target is too short");
      assert.ok(control.width >= 44, control.name + " touch target is too narrow");
      const overlapsPanel =
        control.left < mobileUx.panel.right &&
        control.right > mobileUx.panel.left &&
        control.top < mobileUx.panel.bottom &&
        control.bottom > mobileUx.panel.top;
      assert.equal(
        overlapsPanel,
        false,
        control.name + " is blocked by the capture-status panel"
      );
    }
    const collapseState = await evaluate(
      cdp,
      [
        "const toggle = document.querySelector('#capture-toggle');",
        "toggle.click();",
        "return {",
        "  expanded: toggle.getAttribute('aria-expanded'),",
        "  bodyHidden: document.querySelector('#capture-center-body').hidden",
        "};",
      ].join("\n")
    );
    assert.equal(collapseState.expanded, "false");
    assert.equal(collapseState.bodyHidden, true);

    const ax = await cdp.call("Accessibility.getFullAXTree");
    const accessibleButtons = ax.nodes
      .filter(function (node) {
        return node.role && node.role.value === "button";
      })
      .map(function (node) {
        return node.name && node.name.value;
      });
    assert.ok(accessibleButtons.includes("Take photo"));
    assert.ok(accessibleButtons.includes("Upload files"));
    assert.ok(accessibleButtons.includes("Save to device outbox"));

    console.log(
      JSON.stringify(
        {
          result: "pass",
          page_batch_records: pendingBatch.records,
          page_batch_durable_statuses: batchServerStatuses.durable,
          client_capture_id: pending.id,
          source_document_id: acknowledged.documentId,
          offline_reload_preserved_bytes: pending.fileSize,
          post_capture_ids: acknowledged.postIds,
          acknowledgement_ms: acknowledgementMs,
          terminal_device_state: acknowledged.state,
          terminal_server_state: serverStatus.server_state,
          storage_persistence: pending.persistence,
          background_sync_fallback: "manual retry visible",
          mobile_viewport: "390x844x3 touch",
          accessible_capture_buttons: accessibleButtons.filter(Boolean),
        },
        null,
        2
      )
    );
  } catch (error) {
    if (browserErrors) {
      error.message += "\nChromium stderr:\n" + browserErrors.slice(-4_000);
    }
    throw error;
  } finally {
    if (cdp) cdp.close();
    browser.kill("SIGTERM");
    await pause(100);
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

main().catch(function (error) {
  console.error(error.stack || error);
  process.exitCode = 1;
});
