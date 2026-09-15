const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const baseUrl = (process.argv[2] || "http://127.0.0.1:8776").replace(/\/$/, "");
const month = process.argv[3] || "2026-06";
const artifactDir = path.resolve(
  process.argv[4] || path.join(os.tmpdir(), "finn-nancy-period-statement-ui")
);
const chromiumBin = process.env.CHROMIUM_BIN || "chromium";

function pause(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
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
          pending.reject(new Error(message.error.message));
        } else {
          pending.resolve(message.result || {});
        }
        return;
      }
      const listeners = this.waiters.get(message.method) || [];
      this.waiters.delete(message.method);
      listeners.forEach((listener) => listener.resolve(message.params || {}));
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
        reject(new Error("timed out waiting for " + method));
      }, timeoutMs || 15_000);
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

async function navigate(cdp, url) {
  const loaded = cdp.event("Page.loadEventFired");
  await cdp.call("Page.navigate", { url });
  await loaded;
}

async function setViewport(cdp, viewport) {
  await cdp.call("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: viewport.mobile ? 3 : 1,
    mobile: viewport.mobile,
  });
  await cdp.call("Emulation.setTouchEmulationEnabled", {
    enabled: viewport.mobile,
    maxTouchPoints: viewport.mobile ? 5 : 1,
  });
}

async function capture(cdp, filename) {
  const result = await cdp.call("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  fs.writeFileSync(
    path.join(artifactDir, filename),
    Buffer.from(result.data, "base64")
  );
}

async function inspect(cdp, viewportName) {
  return evaluate(
    cdp,
    [
      "const visible = (element) => {",
      "  const rect = element.getBoundingClientRect();",
      "  const style = getComputedStyle(element);",
      "  return rect.width > 0 && rect.height > 0 &&",
      "    style.visibility !== 'hidden' && style.display !== 'none';",
      "};",
      "const label = (element) =>",
      "  element.getAttribute('aria-label') ||",
      "  (element.labels && element.labels[0] && element.labels[0].textContent.trim()) ||",
      "  element.textContent.trim().replace(/\\s+/g, ' ').slice(0, 70) ||",
      "  element.tagName.toLowerCase();",
      "const scopedTargets = Array.from(document.querySelectorAll(",
      "  '.period-statement-hero a[href], .period-statement-hero select, ' +",
      "  '.period-statement-workspace a[href], .period-statement-workspace select'",
      ")).filter(visible);",
      "const targetMetrics = scopedTargets.map((element) => {",
      "  const rect = element.getBoundingClientRect();",
      "  return {",
      "    label: label(element),",
      "    width: Math.round(rect.width * 100) / 100,",
      "    height: Math.round(rect.height * 100) / 100,",
      "  };",
      "});",
      "const overflowElements = [document.documentElement, document.body,",
      "  ...document.querySelectorAll(",
      "    '.period-statement-hero, .period-statement-workspace, ' +",
      "    '.period-statement-workspace .card, .period-total-grid, ' +",
      "    '.period-resolution-grid, .period-report-row'",
      "  )",
      "];",
      "const overflowMetrics = overflowElements.map((element, index) => {",
      "  const elementRect = element.getBoundingClientRect();",
      "  const rightmost = Array.from(element.querySelectorAll('*')).reduce(",
      "    (current, candidate) => candidate.getBoundingClientRect().right >",
      "      current.getBoundingClientRect().right ? candidate : current, element",
      "  );",
      "  const rightmostRect = rightmost.getBoundingClientRect();",
      "  const widest = Array.from(element.querySelectorAll('*')).reduce(",
      "    (current, candidate) => candidate.scrollWidth > current.scrollWidth ?",
      "      candidate : current, element",
      "  );",
      "  const heading = element.querySelector('h2');",
      "  return {",
      "    label: element === document.documentElement ? 'html' :",
      "      element === document.body ? 'body' :",
      "      String(element.className || element.tagName) + '-' + index +",
      "      (heading ? ':' + heading.textContent.trim() : ''),",
      "    clientWidth: element.clientWidth,",
      "    scrollWidth: element.scrollWidth,",
      "    widest: label(widest) + ':' + widest.scrollWidth,",
      "    rightmost: label(rightmost) + ':' + Math.round(rightmostRect.right) +",
      "      ' vs ' + Math.round(elementRect.right),",
      "    layout: getComputedStyle(element).gridTemplateColumns +",
      "      ' gap=' + getComputedStyle(element).columnGap +",
      "      ' width=' + Math.round(elementRect.width),",
      "  };",
      "});",
      "const totalLinks = Array.from(document.querySelectorAll('.period-total-link'));",
      "const totalTargets = totalLinks.map((link) => link.getAttribute('href'));",
      "const ids = Array.from(document.querySelectorAll('[id]')).map((element) => element.id);",
      "const text = document.body.textContent.replace(/\\s+/g, ' ').trim();",
      "return {",
      "  viewportName: " + JSON.stringify(viewportName) + ",",
      "  viewport: { width: innerWidth, height: innerHeight },",
      "  reportReady: document.querySelector('.period-statement-workspace')",
      "    .dataset.reportReady,",
      "  reportOrigin: document.querySelector('.period-statement-workspace')",
      "    .dataset.reportOrigin,",
      "  totalCount: totalLinks.length,",
      "  totalTargetsResolve: totalTargets.every((target) =>",
      "    target && document.getElementById(target.slice(1))",
      "  ),",
      "  uniqueIds: new Set(ids).size === ids.length,",
      "  rowCount: document.querySelectorAll('.period-report-row').length,",
      "  resolutionColumns: getComputedStyle(",
      "    document.querySelector('.period-resolution-grid')",
      "  ).gridTemplateColumns.trim().split(/\\s+/).length,",
      "  hasWorkflowLinks: ['/review/import', '/recon?month=" + month + "',",
      "    '/close?month=" + month + "'].every((href) =>",
      "      document.querySelector('a[href=\"' + href + '\"]')",
      "    ),",
      "  hasResolutionTruth: text.includes('human approved') &&",
      "    text.includes('unresolved') && text.includes('Acme Market'),",
      "  hasLiquidCaveat: text.includes('Account-kind-v1 boundary') &&",
      "    text.includes('investment value is excluded'),",
      "  targetMetrics,",
      "  overflowMetrics,",
      "};",
    ].join("\n")
  );
}

function assertMetrics(metrics, expected) {
  assert.deepEqual(metrics.viewport, expected.viewport);
  assert.equal(metrics.reportReady, "true");
  assert.equal(metrics.reportOrigin, "live");
  assert.ok(metrics.totalCount >= 10);
  assert.equal(metrics.totalTargetsResolve, true);
  assert.equal(metrics.uniqueIds, true);
  assert.ok(metrics.rowCount >= 10);
  assert.equal(metrics.resolutionColumns, expected.resolutionColumns);
  assert.equal(metrics.hasWorkflowLinks, true);
  assert.equal(metrics.hasResolutionTruth, true);
  assert.equal(metrics.hasLiquidCaveat, true);
  for (const target of metrics.targetMetrics) {
    assert.ok(
      target.width >= 44 && target.height >= 44,
      target.label + " target is smaller than 44x44: " +
        target.width + "x" + target.height
    );
  }
  for (const element of metrics.overflowMetrics) {
    assert.ok(
      element.scrollWidth <= element.clientWidth + 1,
      element.label + " overflows horizontally: " +
        element.scrollWidth + " > " + element.clientWidth +
        " (widest " + element.widest + "; rightmost " + element.rightmost +
        "; layout " + element.layout + ")"
    );
  }
}

async function main() {
  const tempRoot = path.resolve(os.tmpdir()) + path.sep;
  assert.ok(
    artifactDir.startsWith(tempRoot),
    "browser evidence must be written under the system temp directory"
  );
  fs.mkdirSync(artifactDir, { recursive: true });

  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "fn148-chromium-"));
  const browser = spawn(
    chromiumBin,
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--hide-scrollbars",
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=" + port,
      "--user-data-dir=" + profile,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] }
  );
  let browserErrors = "";
  browser.stderr.on("data", (chunk) => {
    browserErrors += String(chunk);
  });

  let cdp;
  try {
    const pages = await waitForJson(
      "http://127.0.0.1:" + port + "/json/list",
      10_000
    );
    const page = pages.find((candidate) => candidate.type === "page");
    assert.ok(page && page.webSocketDebuggerUrl, "Chromium page target missing");
    cdp = new Cdp(page.webSocketDebuggerUrl);
    await Promise.all([
      cdp.call("Page.enable"),
      cdp.call("Runtime.enable"),
      cdp.call("DOM.enable"),
    ]);

    const url = baseUrl + "/statements?month=" + month;
    await setViewport(cdp, { width: 390, height: 844, mobile: true });
    await navigate(cdp, url);
    const mobile = await inspect(cdp, "mobile");
    assertMetrics(mobile, {
      viewport: { width: 390, height: 844 },
      resolutionColumns: 1,
    });
    await evaluate(cdp, "scrollTo(0, 0); return scrollY;");
    await capture(cdp, "01-mobile-statement-top.png");
    await evaluate(
      cdp,
      [
        "const target = document.querySelector('.period-resolution-grid');",
        "target.scrollIntoView({ block: 'start' });",
        "scrollBy(0, -12);",
        "return scrollY;",
      ].join("\n")
    );
    await capture(cdp, "02-mobile-resolution-drilldown.png");

    await setViewport(cdp, { width: 1440, height: 1000, mobile: false });
    await navigate(cdp, url);
    const desktop = await inspect(cdp, "desktop");
    assertMetrics(desktop, {
      viewport: { width: 1440, height: 1000 },
      resolutionColumns: 4,
    });
    await evaluate(cdp, "scrollTo(0, 0); return scrollY;");
    await capture(cdp, "03-desktop-statement-top.png");

    const report = { url, mobile, desktop };
    fs.writeFileSync(
      path.join(artifactDir, "period-statement-metrics.json"),
      JSON.stringify(report, null, 2) + "\n"
    );
    process.stdout.write(
      JSON.stringify({
        ok: true,
        artifactDir,
        mobile: {
          viewport: mobile.viewport,
          targetCount: mobile.targetMetrics.length,
          totalCount: mobile.totalCount,
          rowCount: mobile.rowCount,
        },
        desktop: {
          viewport: desktop.viewport,
          targetCount: desktop.targetMetrics.length,
          totalCount: desktop.totalCount,
          rowCount: desktop.rowCount,
        },
      }) + "\n"
    );
  } catch (error) {
    if (browserErrors) process.stderr.write(browserErrors.slice(-4000));
    throw error;
  } finally {
    if (cdp) cdp.close();
    browser.kill("SIGTERM");
    await pause(150);
    if (browser.exitCode === null) browser.kill("SIGKILL");
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
