(function (root) {
  "use strict";

  const DB_NAME = "finn-nancy-capture";
  const DB_VERSION = 2;
  const STORE_NAME = "captures";
  const META_STORE_NAME = "capture_meta";
  const LEASE_MS = 60_000;
  const MAX_AUTO_ATTEMPTS = 5;

  const TRANSITIONS = Object.freeze({
    pending: Object.freeze({ SEND: "sending" }),
    sending: Object.freeze({
      ACK: "saved",
      RETRY: "retry",
      FAIL: "failed",
      LEASE_EXPIRED: "pending",
    }),
    retry: Object.freeze({ SEND: "sending", MANUAL_RETRY: "pending" }),
    saved: Object.freeze({
      PROCESS: "processing",
      NEEDS_REVIEW: "needs_review",
      FILED: "filed",
      FAIL: "failed",
    }),
    processing: Object.freeze({
      NEEDS_REVIEW: "needs_review",
      FILED: "filed",
      FAIL: "failed",
    }),
    needs_review: Object.freeze({ FILED: "filed" }),
    filed: Object.freeze({}),
    failed: Object.freeze({ MANUAL_RETRY: "pending" }),
  });

  function nextState(current, event) {
    const target = TRANSITIONS[current] && TRANSITIONS[current][event];
    if (!target) {
      throw new Error("invalid capture transition: " + current + " + " + event);
    }
    return target;
  }

  function retryDelay(attempt, randomValue) {
    const safeAttempt = Math.max(1, Number(attempt) || 1);
    const random = Math.max(0, Math.min(1, Number(randomValue) || 0));
    const exponential = Math.min(1_000 * Math.pow(2, safeAttempt - 1), 30_000);
    return Math.round(Math.min(exponential + exponential * 0.35 * random, 30_000));
  }

  function isTransientStatus(status) {
    return status === 408 || status === 425 || status === 429 || status >= 500;
  }

  function createCaptureRecord(file, metadata, idGenerator, now) {
    if (!file || typeof file.size !== "number") {
      throw new Error("capture file is required");
    }
    const makeId =
      idGenerator ||
      function () {
        if (!root.crypto || typeof root.crypto.randomUUID !== "function") {
          throw new Error("secure UUID generation is unavailable");
        }
        return root.crypto.randomUUID();
      };
    const createdAt = new Date(now === undefined ? Date.now() : now).toISOString();
    const meta = metadata || {};
    return {
      id: makeId(),
      file: file,
      name: String(file.name || "capture"),
      type: String(file.type || "application/octet-stream"),
      size: Number(file.size),
      createdAt: createdAt,
      updatedAt: createdAt,
      source: String(meta.source || "file"),
      intent: String(meta.intent || "receipt"),
      sharedTitle: String(meta.sharedTitle || ""),
      sharedText: String(meta.sharedText || ""),
      sharedUrl: String(meta.sharedUrl || ""),
      proofRunId: String(meta.proofRunId || ""),
      deviceCohortId: String(meta.deviceCohortId || ""),
      storagePersistence: String(meta.storagePersistence || "unknown"),
      state: "pending",
      attempts: 0,
      autoRetry: true,
      nextAttemptAt: 0,
      leaseUntil: 0,
      lastError: "",
      serverDocumentId: null,
      serverState: null,
      durableAt: null,
      onlineDurableAckMs: null,
      onlineAckTelemetryRecorded: false,
      offlineStartedAt: meta.offlineStartedAt || null,
      offlineRecoveryMs: null,
      offlineRecoverySequence: 0,
      offlineTelemetryRecorded: true,
    };
  }

  function requestPromise(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () {
        resolve(request.result);
      };
      request.onerror = function () {
        reject(request.error || new Error("IndexedDB request failed"));
      };
    });
  }

  function transactionDone(transaction) {
    return new Promise(function (resolve, reject) {
      transaction.oncomplete = function () {
        resolve();
      };
      transaction.onerror = function () {
        reject(transaction.error || new Error("IndexedDB transaction failed"));
      };
      transaction.onabort = function () {
        reject(transaction.error || new Error("IndexedDB transaction aborted"));
      };
    });
  }

  function openDb() {
    if (!root.indexedDB) {
      return Promise.reject(new Error("IndexedDB is unavailable"));
    }
    return new Promise(function (resolve, reject) {
      const request = root.indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = function () {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          const store = db.createObjectStore(STORE_NAME, { keyPath: "id" });
          store.createIndex("createdAt", "createdAt", { unique: false });
        }
        if (!db.objectStoreNames.contains(META_STORE_NAME)) {
          db.createObjectStore(META_STORE_NAME, { keyPath: "key" });
        }
      };
      request.onsuccess = function () {
        resolve(request.result);
      };
      request.onerror = function () {
        reject(request.error || new Error("IndexedDB open failed"));
      };
    });
  }

  async function putCapture(record) {
    const db = await openDb();
    try {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      transaction.objectStore(STORE_NAME).put(record);
      await transactionDone(transaction);
      return record;
    } finally {
      db.close();
    }
  }

  async function getCapture(id) {
    const db = await openDb();
    try {
      const transaction = db.transaction(STORE_NAME, "readonly");
      const result = await requestPromise(
        transaction.objectStore(STORE_NAME).get(id)
      );
      await transactionDone(transaction);
      return result || null;
    } finally {
      db.close();
    }
  }

  async function listCaptures() {
    const db = await openDb();
    try {
      const transaction = db.transaction(STORE_NAME, "readonly");
      const records = await requestPromise(
        transaction.objectStore(STORE_NAME).getAll()
      );
      await transactionDone(transaction);
      return records.sort(function (left, right) {
        return String(right.createdAt).localeCompare(String(left.createdAt));
      });
    } finally {
      db.close();
    }
  }

  async function deleteCapture(id) {
    const db = await openDb();
    try {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      transaction.objectStore(STORE_NAME).delete(id);
      await transactionDone(transaction);
    } finally {
      db.close();
    }
  }

  async function setProofContext(context) {
    const db = await openDb();
    try {
      const transaction = db.transaction(META_STORE_NAME, "readwrite");
      transaction.objectStore(META_STORE_NAME).put({
        key: "proof_context",
        proofRunId: String((context && context.proofRunId) || ""),
        deviceCohortId: String((context && context.deviceCohortId) || ""),
        expiresAt: Number((context && context.expiresAt) || 0),
      });
      await transactionDone(transaction);
    } finally {
      db.close();
    }
  }

  async function getProofContext(now) {
    const db = await openDb();
    try {
      const transaction = db.transaction(META_STORE_NAME, "readonly");
      const result = await requestPromise(
        transaction.objectStore(META_STORE_NAME).get("proof_context")
      );
      await transactionDone(transaction);
      if (!result || Number(result.expiresAt || 0) <= Number(now || Date.now())) {
        return null;
      }
      return {
        proofRunId: String(result.proofRunId || ""),
        deviceCohortId: String(result.deviceCohortId || ""),
      };
    } finally {
      db.close();
    }
  }

  async function updateCapture(id, updater) {
    const db = await openDb();
    try {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      const current = await requestPromise(store.get(id));
      if (!current) {
        transaction.abort();
        throw new Error("capture not found: " + id);
      }
      const updated = updater(Object.assign({}, current));
      updated.updatedAt = new Date().toISOString();
      store.put(updated);
      await transactionDone(transaction);
      return updated;
    } finally {
      db.close();
    }
  }

  function transitionCapture(id, event, patch) {
    return updateCapture(id, function (record) {
      record.state = nextState(record.state, event);
      return Object.assign(record, patch || {});
    });
  }

  async function claimDueCapture(now, includeFutureRetries) {
    const currentTime = Number(now === undefined ? Date.now() : now);
    const db = await openDb();
    try {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      const records = await requestPromise(store.getAll());
      records.sort(function (left, right) {
        return String(left.createdAt).localeCompare(String(right.createdAt));
      });
      const due = records.find(function (record) {
        if (record.state === "sending" && Number(record.leaseUntil || 0) <= currentTime) {
          return true;
        }
        if (record.state === "pending") {
          return true;
        }
        if (record.state !== "retry" || record.autoRetry === false) {
          return false;
        }
        return includeFutureRetries || Number(record.nextAttemptAt || 0) <= currentTime;
      });
      if (!due) {
        await transactionDone(transaction);
        return null;
      }
      if (due.state === "sending") {
        due.state = nextState("sending", "LEASE_EXPIRED");
      }
      due.state = nextState(due.state, "SEND");
      due.attempts = Number(due.attempts || 0) + 1;
      due.leaseUntil = currentTime + LEASE_MS;
      due.updatedAt = new Date(currentTime).toISOString();
      store.put(due);
      await transactionDone(transaction);
      return due;
    } finally {
      db.close();
    }
  }

  async function recoverExpiredLeases(now) {
    const currentTime = Number(now === undefined ? Date.now() : now);
    const db = await openDb();
    let recovered = 0;
    try {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      const store = transaction.objectStore(STORE_NAME);
      const records = await requestPromise(store.getAll());
      records.forEach(function (record) {
        if (
          record.state === "sending" &&
          Number(record.leaseUntil || 0) <= currentTime
        ) {
          record.state = nextState("sending", "LEASE_EXPIRED");
          record.leaseUntil = 0;
          record.updatedAt = new Date(currentTime).toISOString();
          store.put(record);
          recovered += 1;
        }
      });
      await transactionDone(transaction);
      return recovered;
    } finally {
      db.close();
    }
  }

  async function importDurableCapture(status) {
    const existing = await getCapture(status.client_capture_id);
    if (existing) {
      return existing;
    }
    const storedAt = status.stored_at || new Date().toISOString();
    return putCapture({
      id: status.client_capture_id,
      file: null,
      name: "Shared capture",
      type: "",
      size: 0,
      createdAt: storedAt,
      updatedAt: storedAt,
      source:
        (status.source_metadata && status.source_metadata.source) || "share",
      intent:
        (status.source_metadata && status.source_metadata.intent) || "receipt",
      sharedTitle: "",
      sharedText: "",
      sharedUrl: "",
      proofRunId: "",
      deviceCohortId: "",
      storagePersistence: "server",
      state: "saved",
      attempts: 1,
      autoRetry: false,
      nextAttemptAt: 0,
      leaseUntil: 0,
      lastError: "",
      serverDocumentId: status.source_document_id,
      serverState: status.server_state || "queued",
      durableAt: status.stored_at || storedAt,
      onlineDurableAckMs: null,
      onlineAckTelemetryRecorded: true,
      offlineStartedAt: null,
      offlineRecoveryMs: null,
      offlineRecoverySequence: 0,
      offlineTelemetryRecorded: true,
    });
  }

  const api = Object.freeze({
    DB_NAME: DB_NAME,
    STORE_NAME: STORE_NAME,
    META_STORE_NAME: META_STORE_NAME,
    LEASE_MS: LEASE_MS,
    MAX_AUTO_ATTEMPTS: MAX_AUTO_ATTEMPTS,
    TRANSITIONS: TRANSITIONS,
    nextState: nextState,
    retryDelay: retryDelay,
    isTransientStatus: isTransientStatus,
    createCaptureRecord: createCaptureRecord,
    putCapture: putCapture,
    getCapture: getCapture,
    listCaptures: listCaptures,
    deleteCapture: deleteCapture,
    setProofContext: setProofContext,
    getProofContext: getProofContext,
    updateCapture: updateCapture,
    transitionCapture: transitionCapture,
    claimDueCapture: claimDueCapture,
    recoverExpiredLeases: recoverExpiredLeases,
    importDurableCapture: importDurableCapture,
  });

  root.FinnCaptureStore = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
