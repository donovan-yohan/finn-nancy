const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const baseUrl = (process.argv[2] || "http://127.0.0.1:8776").replace(/\/$/, "");
const artifactDir = path.resolve(
  process.argv[3] || path.join(os.tmpdir(), "finn-nancy-resolution-ui")
);
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

async function navigate(cdp, url) {
  const loaded = cdp.event("Page.loadEventFired", 15_000);
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
  fs.writeFileSync(path.join(artifactDir, filename), Buffer.from(result.data, "base64"));
}

async function inspectPage(cdp, viewportName) {
  return evaluate(
    cdp,
    [
      "const form = document.querySelector('form.resolution-review-form');",
      "const merchantPanel = document.querySelector(",
      "  '[data-resolution-kind=\"canonical_merchant\"]'",
      ");",
      "const categoryPanel = document.querySelector(",
      "  '[data-resolution-kind=\"expense_category\"]'",
      ");",
      "const merchant = merchantPanel && merchantPanel.querySelector('input[name=\"merchant\"]');",
      "const category = categoryPanel && categoryPanel.querySelector('select[name=\"category_name\"]');",
      "const isVisible = (element) => {",
      "  const style = getComputedStyle(element);",
      "  const rect = element.getBoundingClientRect();",
      "  return style.display !== 'none' && style.visibility !== 'hidden' &&",
      "    rect.width > 0 && rect.height > 0;",
      "};",
      "const labelFor = (element) =>",
      "  element.getAttribute('aria-label') || element.name ||",
      "  element.textContent.trim().replace(/\\s+/g, ' ').slice(0, 50) ||",
      "  element.tagName.toLowerCase();",
      "const interactive = Array.from(document.querySelectorAll(",
      "  'a[href], button, input, select, textarea, summary, [tabindex]'",
      ")).filter((element) => isVisible(element) && !element.disabled &&",
      "  element.tabIndex >= 0);",
      "const targetMetrics = interactive.map((element) => {",
      "  const rect = element.getBoundingClientRect();",
      "  return {",
      "    label: labelFor(element),",
      "    tag: element.tagName.toLowerCase(),",
      "    width: Math.round(rect.width * 100) / 100,",
      "    height: Math.round(rect.height * 100) / 100,",
      "  };",
      "});",
      "const overflowElements = [",
      "  document.documentElement,",
      "  document.body,",
      "  ...document.querySelectorAll('.hero, .grid, .card, .resolution-review-form, .resolution-decision-panel'),",
      "];",
      "const overflowMetrics = overflowElements.map((element, index) => ({",
      "  label: element === document.documentElement ? 'html' :",
      "    element === document.body ? 'body' :",
      "    element.matches('[data-resolution-kind]') ?",
      "      element.getAttribute('data-resolution-kind') :",
      "      element.className || element.tagName.toLowerCase() + '-' + index,",
      "  clientWidth: element.clientWidth,",
      "  scrollWidth: element.scrollWidth,",
      "}));",
      "const focusables = form ? Array.from(form.querySelectorAll(",
      "  'input:not([disabled]), select:not([disabled]), button:not([disabled])'",
      ")).filter(isVisible) : [];",
      "const focusOrder = focusables.map((element) =>",
      "  element.name || (element.type === 'submit' ? 'approve' : labelFor(element))",
      ");",
      "const formCopy = form ? form.textContent.replace(/\\s+/g, ' ').trim() : '';",
      "const merchantStyle = merchantPanel ? getComputedStyle(merchantPanel) : null;",
      "const categoryStyle = categoryPanel ? getComputedStyle(categoryPanel) : null;",
      "return {",
      "  viewportName: " + JSON.stringify(viewportName) + ",",
      "  viewport: { width: innerWidth, height: innerHeight },",
      "  formCount: document.querySelectorAll('form.resolution-review-form').length,",
      "  merchantCount: document.querySelectorAll(",
      "    '[data-resolution-kind=\"canonical_merchant\"] input[name=\"merchant\"]'",
      "  ).length,",
      "  categoryCount: document.querySelectorAll(",
      "    '[data-resolution-kind=\"expense_category\"] select[name=\"category_name\"]'",
      "  ).length,",
      "  independentPanels: Boolean(merchant && category &&",
      "    merchant.closest('fieldset') !== category.closest('fieldset')),",
      "  merchantBeforeCategory: Boolean(merchant && category &&",
      "    merchant.compareDocumentPosition(category) & Node.DOCUMENT_POSITION_FOLLOWING),",
      "  merchantCopyIndependent: formCopy.includes(",
      "    'saved independently from the expense category'",
      "  ),",
      "  categoryCopyIndependent: formCopy.includes(",
      "    'merchant confirmation never silently decides this category'",
      "  ),",
      "  controlsHaveLabels: focusables.every((element) =>",
      "    element.tagName === 'BUTTON' || element.labels.length > 0",
      "  ),",
      "  focusOrder,",
      "  formColumnCount: form ?",
      "    getComputedStyle(form).gridTemplateColumns.trim().split(/\\s+/).length : 0,",
      "  panelBorderColors: {",
      "    merchant: merchantStyle && merchantStyle.borderTopColor,",
      "    category: categoryStyle && categoryStyle.borderTopColor,",
      "  },",
      "  targetMetrics,",
      "  overflowMetrics,",
      "};",
    ].join("\n")
  );
}

async function actualTabOrder(cdp) {
  await evaluate(
    cdp,
    [
      "const merchant = document.querySelector(",
      "  '[data-resolution-kind=\"canonical_merchant\"] input[name=\"merchant\"]'",
      ");",
      "merchant.focus();",
      "return document.activeElement === merchant;",
    ].join("\n")
  );
  const raw = [];
  for (let index = 0; index < 20; index += 1) {
    const activeName = await evaluate(
      cdp,
      [
        "const active = document.activeElement;",
        "return active.name ||",
        "  (active.type === 'submit' ? 'approve' : active.tagName.toLowerCase());",
      ].join("\n")
    );
    raw.push(activeName);
    if (activeName === "approve") break;
    await cdp.call("Input.dispatchKeyEvent", {
      type: "keyDown",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
      nativeVirtualKeyCode: 9,
    });
    await cdp.call("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: "Tab",
      code: "Tab",
      windowsVirtualKeyCode: 9,
      nativeVirtualKeyCode: 9,
    });
  }
  return {
    raw,
    controls: raw.filter((name, index) => index === 0 || name !== raw[index - 1]),
  };
}

function assertMetrics(metrics, expected) {
  const expectedFocusOrder = [
    "merchant",
    "purchased_on",
    "total",
    "category_name",
    "account_id",
    "approve",
  ];
  assert.deepEqual(metrics.viewport, expected.viewport);
  assert.equal(metrics.formCount, 1);
  assert.equal(metrics.merchantCount, 1);
  assert.equal(metrics.categoryCount, 1);
  assert.equal(metrics.independentPanels, true);
  assert.equal(metrics.merchantBeforeCategory, true);
  assert.equal(metrics.merchantCopyIndependent, true);
  assert.equal(metrics.categoryCopyIndependent, true);
  assert.equal(metrics.controlsHaveLabels, true);
  assert.deepEqual(metrics.focusOrder, expectedFocusOrder);
  assert.deepEqual(metrics.actualTabOrder.controls, expectedFocusOrder);
  assert.equal(metrics.formColumnCount, expected.formColumnCount);
  assert.notEqual(
    metrics.panelBorderColors.merchant,
    metrics.panelBorderColors.category,
    "merchant and category decisions should remain visually distinct"
  );
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
        element.scrollWidth + " > " + element.clientWidth
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "fn149b-chromium-"));
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
      cdp.call("Accessibility.enable"),
    ]);

    const mobile = { width: 390, height: 844, mobile: true };
    await setViewport(cdp, mobile);
    await navigate(cdp, baseUrl + "/review");
    const mobileMetrics = await inspectPage(cdp, "mobile");
    mobileMetrics.actualTabOrder = await actualTabOrder(cdp);
    assertMetrics(mobileMetrics, {
      viewport: { width: 390, height: 844 },
      formColumnCount: 1,
    });
    await evaluate(cdp, "scrollTo(0, 0); return scrollY;");
    await capture(cdp, "01-mobile-review-top.png");
    await evaluate(
      cdp,
      [
        "const form = document.querySelector('.resolution-review-form');",
        "form.scrollIntoView({ block: 'start' });",
        "scrollBy(0, -12);",
        "document.querySelector(",
        "  '[data-resolution-kind=\"expense_category\"] select'",
        ").focus();",
        "return scrollY;",
      ].join("\n")
    );
    await capture(cdp, "02-mobile-resolution-form.png");

    const desktop = { width: 1440, height: 1100, mobile: false };
    await setViewport(cdp, desktop);
    await navigate(cdp, baseUrl + "/review");
    const desktopMetrics = await inspectPage(cdp, "desktop");
    desktopMetrics.actualTabOrder = await actualTabOrder(cdp);
    assertMetrics(desktopMetrics, {
      viewport: { width: 1440, height: 1100 },
      formColumnCount: 2,
    });
    await evaluate(cdp, "scrollTo(0, 0); return scrollY;");
    await capture(cdp, "03-desktop-resolution-review.png");

    const report = {
      url: baseUrl + "/review",
      mobile: mobileMetrics,
      desktop: desktopMetrics,
    };
    fs.writeFileSync(
      path.join(artifactDir, "review-resolution-metrics.json"),
      JSON.stringify(report, null, 2) + "\n"
    );
    process.stdout.write(
      JSON.stringify({
        ok: true,
        artifactDir,
        mobile: {
          viewport: mobileMetrics.viewport,
          targetCount: mobileMetrics.targetMetrics.length,
          formColumnCount: mobileMetrics.formColumnCount,
        },
        desktop: {
          viewport: desktopMetrics.viewport,
          targetCount: desktopMetrics.targetMetrics.length,
          formColumnCount: desktopMetrics.formColumnCount,
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

main().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});
