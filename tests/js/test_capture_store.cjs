const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const store = require(
  path.resolve(__dirname, "../../app/web/static/capture-store.js")
);

test("capture state machine only marks Saved after durable ACK", function () {
  assert.equal(store.nextState("pending", "SEND"), "sending");
  assert.equal(store.nextState("sending", "ACK"), "saved");
  assert.equal(store.nextState("saved", "PROCESS"), "processing");
  assert.equal(store.nextState("processing", "NEEDS_REVIEW"), "needs_review");
  assert.equal(store.nextState("needs_review", "FILED"), "filed");

  assert.throws(
    function () {
      store.nextState("pending", "ACK");
    },
    /invalid capture transition/
  );
  assert.throws(
    function () {
      store.nextState("retry", "ACK");
    },
    /invalid capture transition/
  );
});

test("retry backoff is bounded and recognizes only retryable statuses", function () {
  assert.equal(store.retryDelay(1, 0), 1_000);
  assert.equal(store.retryDelay(2, 0), 2_000);
  assert.equal(store.retryDelay(100, 1), 30_000);
  assert.ok(store.retryDelay(4, 0.5) >= 8_000);
  assert.ok(store.retryDelay(4, 0.5) <= 30_000);

  for (const status of [408, 425, 429, 500, 503]) {
    assert.equal(store.isTransientStatus(status), true);
  }
  for (const status of [200, 400, 401, 404, 409, 422]) {
    assert.equal(store.isTransientStatus(status), false);
  }
});

test("a stable client id and Blob metadata exist in Pending before upload", function () {
  const file = {
    name: "receipt.jpg",
    type: "image/jpeg",
    size: 512,
  };
  let generated = 0;
  const record = store.createCaptureRecord(
    file,
    {
      source: "camera",
      intent: "receipt",
      storagePersistence: "persistent",
    },
    function () {
      generated += 1;
      return "2f6bd910-180d-4a57-831c-37e09a195577";
    },
    Date.UTC(2026, 6, 26)
  );

  assert.equal(generated, 1);
  assert.equal(record.id, "2f6bd910-180d-4a57-831c-37e09a195577");
  assert.equal(record.file, file);
  assert.equal(record.name, "receipt.jpg");
  assert.equal(record.source, "camera");
  assert.equal(record.intent, "receipt");
  assert.equal(record.storagePersistence, "persistent");
  assert.equal(record.state, "pending");
  assert.equal(record.attempts, 0);
  assert.equal(record.serverDocumentId, null);
  assert.equal(record.durableAt, null);
  assert.equal(record.onlineDurableAckMs, null);
  assert.equal(record.onlineAckTelemetryRecorded, false);
  assert.equal(record.offlineRecoveryMs, null);
  assert.equal(record.offlineTelemetryRecorded, true);
});

test("killed sending leases return to Pending with the same id", function () {
  assert.equal(store.nextState("sending", "LEASE_EXPIRED"), "pending");
  assert.equal(store.nextState("retry", "MANUAL_RETRY"), "pending");
  assert.equal(store.nextState("failed", "MANUAL_RETRY"), "pending");
});
