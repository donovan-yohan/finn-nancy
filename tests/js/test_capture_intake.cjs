const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.resolve(__dirname, "../../app/web/static/sw.js"),
  "utf8"
);

class FakeFormData {
  constructor() {
    this.values = new Map();
  }

  append(name, value) {
    const existing = this.values.get(name) || [];
    existing.push(value);
    this.values.set(name, existing);
  }

  get(name) {
    const values = this.values.get(name) || [];
    return values.length ? values[0] : null;
  }
}

function loadWorker(options) {
  const listeners = new Map();
  const saved = options.records || [];
  const uploads = [];
  const telemetry = [];
  const syncRegistrations = [];
  const captureStore = {
    MAX_AUTO_ATTEMPTS: 5,
    createCaptureRecord(file, metadata) {
      return {
        id: "shared-" + saved.length,
        file,
        name: file.name,
        state: "pending",
        createdAt: new Date().toISOString(),
        attempts: 0,
        autoRetry: true,
        nextAttemptAt: 0,
        leaseUntil: 0,
        ...metadata,
      };
    },
    async putCapture(record) {
      saved.push(record);
    },
    async recoverExpiredLeases() {},
    async claimDueCapture() {
      const record = saved.find((candidate) => candidate.state === "pending");
      if (!record) return null;
      record.state = "sending";
      record.attempts += 1;
      return record;
    },
    async updateCapture(id, updater) {
      const record = saved.find((candidate) => candidate.id === id);
      return updater(record);
    },
    async listCaptures() {
      return saved;
    },
    async getProofContext() {
      return null;
    },
    nextState(state, event) {
      assert.equal(state, "sending");
      assert.equal(event, "ACK");
      return "saved";
    },
    retryDelay() {
      return 1_000;
    },
    isTransientStatus() {
      return false;
    },
  };
  const workerSelf = {
    FinnCaptureStore: captureStore,
    location: { origin: "https://finn.example" },
    navigator: {
      storage: {
        async persisted() {
          return false;
        },
        async persist() {
          return false;
        },
      },
    },
    registration: {
      sync: options.backgroundSync
        ? {
            async register(tag) {
              syncRegistrations.push(tag);
            },
          }
        : undefined,
    },
    clients: {
      async matchAll() {
        return [];
      },
      async claim() {},
    },
    addEventListener(type, handler) {
      listeners.set(type, handler);
    },
    async skipWaiting() {},
  };
  const context = {
    self: workerSelf,
    importScripts() {},
    caches: {
      async keys() {
        return [];
      },
      async open() {
        return { async addAll() {}, async put() {} };
      },
      async match() {
        return null;
      },
      async delete() {},
    },
    async fetch(input, init) {
      if (String(input).includes("/client-event")) {
        telemetry.push({
          event: init.body.get("event"),
          durationMs: init.body.get("duration_ms"),
          sequenceNo: init.body.get("sequence_no"),
        });
        return { ok: true, status: 200 };
      }
      const captureId = init.body.get("client_capture_id");
      uploads.push(captureId);
      return {
        ok: true,
        status: 200,
        async json() {
          return {
            durable: true,
            client_capture_id: captureId,
            source_document_id: uploads.length,
            server_state: "queued",
          };
        },
      };
    },
    FormData: FakeFormData,
    Response: {
      redirect(url, status) {
        return { url, status };
      },
    },
    URL,
    Date,
    Math,
    Error,
    Object,
    String,
    Number,
    Promise,
  };
  vm.runInNewContext(source, context, { filename: "sw.js" });
  return { listeners, saved, uploads, telemetry, syncRegistrations };
}

function runWaitUntil(handler, event) {
  let pending;
  handler({
    ...event,
    waitUntil(promise) {
      pending = promise;
    },
  });
  return pending;
}

test("share target persists all 21 files before redirect", async function () {
  const worker = loadWorker({ records: [], backgroundSync: false });
  const files = Array.from({ length: 21 }, function (_unused, index) {
    return {
      name: "shared-" + index + ".jpg",
      size: index + 1,
      async arrayBuffer() {
        return new ArrayBuffer(index + 1);
      },
    };
  });
  let responsePromise;
  worker.listeners.get("fetch")({
    request: {
      method: "POST",
      url: "https://finn.example/share-target",
      async formData() {
        return {
          getAll(name) {
            return name === "files" ? files : [];
          },
          get() {
            return "";
          },
        };
      },
    },
    respondWith(promise) {
      responsePromise = promise;
    },
  });

  const response = await responsePromise;

  assert.equal(response.status, 303);
  assert.equal(worker.saved.length, 21);
  assert.equal(new Set(worker.saved.map((record) => record.name)).size, 21);
});

test("20-item service-worker drain registers and completes the 21st item", async function () {
  const records = Array.from({ length: 21 }, function (_unused, index) {
    return {
      id: "capture-" + index,
      file: { name: "capture-" + index + ".jpg" },
      name: "capture-" + index + ".jpg",
      state: "pending",
      attempts: 0,
      createdAt: new Date().toISOString(),
      autoRetry: true,
      nextAttemptAt: 0,
      leaseUntil: 0,
    };
  });
  const worker = loadWorker({ records, backgroundSync: true });
  const syncHandler = worker.listeners.get("sync");

  await runWaitUntil(syncHandler, { tag: "finn-capture-outbox" });

  assert.equal(worker.uploads.length, 20);
  assert.equal(worker.telemetry.length, 20);
  assert.deepEqual(worker.syncRegistrations, ["finn-capture-outbox"]);
  assert.equal(records.filter((record) => record.state === "pending").length, 1);

  await runWaitUntil(syncHandler, { tag: "finn-capture-outbox" });

  assert.equal(worker.uploads.length, 21);
  assert.equal(worker.telemetry.length, 21);
  assert.equal(records.filter((record) => record.state === "saved").length, 21);
  assert.equal(records.filter((record) => record.file).length, 0);
});
