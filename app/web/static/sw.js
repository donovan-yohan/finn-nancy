importScripts("/static/capture-store.js");

const CACHE_NAME = "finn-nancy-__CACHE_VERSION__";
const APP_SHELL = [
  "/",
  "/upload",
  "/processing",
  "/manifest.webmanifest",
  "/static/app.css",
  "/static/capture-store.js",
  "/static/capture-outbox.js",
  "/static/htmx.min.js",
  "/static/manifest.webmanifest",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/icon-maskable-512.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(function (cache) {
        return cache.addAll(APP_SHELL);
      })
      .then(function () {
        return self.skipWaiting();
      })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (names) {
        return Promise.all(names.map(function (name) {
          if (name !== CACHE_NAME) {
            return caches.delete(name);
          }
          return undefined;
        }));
      })
      .then(function () {
        return self.clients.claim();
      })
  );
});

function cacheFirst(request) {
  return caches.match(request).then(function (cached) {
    if (cached) {
      return cached;
    }
    return fetch(request).then(function (response) {
      if (response && response.ok) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then(function (cache) {
          cache.put(request, copy);
        });
      }
      return response;
    });
  });
}

function networkFirstPage(request) {
  return fetch(request).catch(function () {
    return caches.match(request).then(function (cached) {
      if (cached) return cached;
      const pathOnly = new URL(request.url).pathname;
      return caches.match(pathOnly).then(function (pathCached) {
        return pathCached || caches.match("/");
      });
    });
  });
}

async function mutateCapture(id, expected, event, patch) {
  return self.FinnCaptureStore.updateCapture(id, function (record) {
    if (record.state !== expected) return record;
    record.state = self.FinnCaptureStore.nextState(record.state, event);
    return Object.assign(record, patch || {});
  });
}

function workerRetryPatch(record, message) {
  const automatic =
    Number(record.attempts || 0) < self.FinnCaptureStore.MAX_AUTO_ATTEMPTS;
  return {
    leaseUntil: 0,
    autoRetry: automatic,
    nextAttemptAt: automatic
      ? Date.now() +
        self.FinnCaptureStore.retryDelay(record.attempts, Math.random())
      : 0,
    lastError: message,
  };
}

async function uploadCapture(record) {
  if (!record.file) {
    await mutateCapture(record.id, "sending", "FAIL", {
      leaseUntil: 0,
      autoRetry: false,
      lastError: "The device copy is unavailable.",
    });
    return true;
  }
  const form = new FormData();
  form.append("client_capture_id", record.id);
  form.append("source", record.source || "file");
  form.append("intent", record.intent || "receipt");
  form.append("shared_title", record.sharedTitle || "");
  form.append("shared_text", record.sharedText || "");
  form.append("shared_url", record.sharedUrl || "");
  form.append("accepted_at", record.createdAt || "");
  form.append("client_attempts", String(record.attempts || 1));
  form.append("proof_run_id", record.proofRunId || "");
  form.append("device_cohort_id", record.deviceCohortId || "");
  form.append("file", record.file, record.name || "capture");

  const requestStartedAt = Date.now();
  let response;
  try {
    response = await fetch("/captures", {
      method: "POST",
      body: form,
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
  } catch (_error) {
    await mutateCapture(
      record.id,
      "sending",
      "RETRY",
      workerRetryPatch(record, "Could not reach the server.")
    );
    return false;
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = null;
  }
  if (
    response.ok &&
    payload &&
    payload.durable === true &&
    payload.client_capture_id === record.id
  ) {
    const onlineDurableAckMs = Math.max(0, Date.now() - requestStartedAt);
    const offlineStartedAtMs = Date.parse(record.offlineStartedAt || "");
    const offlineRecoveryMs = Number.isFinite(offlineStartedAtMs)
      ? Math.max(0, requestStartedAt - offlineStartedAtMs)
      : record.offlineRecoveryMs;
    const offlineRecoverySequence =
      Number.isFinite(offlineStartedAtMs)
        ? Number(record.offlineRecoverySequence || 0) + 1
        : Number(record.offlineRecoverySequence || 0);
    await mutateCapture(record.id, "sending", "ACK", {
      file: null,
      leaseUntil: 0,
      autoRetry: false,
      nextAttemptAt: 0,
      lastError: "",
      serverDocumentId: payload.source_document_id,
      serverState: payload.server_state || "queued",
      durableAt: payload.stored_at || new Date().toISOString(),
      onlineDurableAckMs: onlineDurableAckMs,
      onlineAckTelemetryRecorded: false,
      offlineStartedAt: null,
      offlineRecoveryMs: offlineRecoveryMs,
      offlineRecoverySequence: offlineRecoverySequence,
      offlineTelemetryRecorded:
        offlineRecoveryMs === null || offlineRecoveryMs === undefined,
    });
    async function report(eventName, durationMs, sequenceNo, recordedField) {
      const eventForm = new FormData();
      eventForm.append("event", eventName);
      eventForm.append("duration_ms", String(Math.round(durationMs)));
      eventForm.append("sequence_no", String(Math.max(1, Number(sequenceNo || 1))));
      try {
        const eventResponse = await fetch(
          "/captures/" + encodeURIComponent(record.id) + "/client-event",
          {
            method: "POST",
            body: eventForm,
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          }
        );
        if (eventResponse.ok) {
          await self.FinnCaptureStore.updateCapture(record.id, function (saved) {
            saved[recordedField] = true;
            return saved;
          });
        }
      } catch (_error) {
        // The page retries this content-free telemetry; durability is unaffected.
      }
    }
    await report(
      "online_durable_ack",
      onlineDurableAckMs,
      Number(record.attempts || 1),
      "onlineAckTelemetryRecorded"
    );
    if (offlineRecoveryMs !== null && offlineRecoveryMs !== undefined) {
      await report(
        "offline_recovered",
        offlineRecoveryMs,
        offlineRecoverySequence,
        "offlineTelemetryRecorded"
      );
    }
    return true;
  }

  const detail =
    payload && typeof payload.detail === "string"
      ? payload.detail
      : "The server did not acknowledge this capture.";
  if (self.FinnCaptureStore.isTransientStatus(response.status)) {
    await mutateCapture(
      record.id,
      "sending",
      "RETRY",
      workerRetryPatch(record, detail)
    );
    return false;
  }
  await mutateCapture(record.id, "sending", "FAIL", {
    leaseUntil: 0,
    autoRetry: false,
    nextAttemptAt: 0,
    lastError: detail,
  });
  return true;
}

async function notifyCaptureClients() {
  const clients = await self.clients.matchAll({
    type: "window",
    includeUncontrolled: true,
  });
  for (const client of clients) {
    client.postMessage({ type: "capture-outbox-updated" });
  }
}

async function drainCaptureOutbox() {
  await self.FinnCaptureStore.recoverExpiredLeases(Date.now());
  for (let index = 0; index < 20; index += 1) {
    const record = await self.FinnCaptureStore.claimDueCapture(Date.now(), true);
    if (!record) return;
    const completed = await uploadCapture(record);
    await notifyCaptureClients();
    if (!completed) {
      throw new Error("capture upload remains pending");
    }
  }
  const remaining = await self.FinnCaptureStore.listCaptures();
  const now = Date.now();
  const hasDueUpload = remaining.some(function (record) {
    return (
      record.state === "pending" ||
      (record.state === "sending" &&
        Number(record.leaseUntil || 0) <= now) ||
      (record.state === "retry" &&
        record.autoRetry !== false &&
        Number(record.nextAttemptAt || 0) <= now)
    );
  });
  if (hasDueUpload) {
    await registerCaptureSync();
  }
}

async function registerCaptureSync() {
  if (!self.registration.sync) return;
  try {
    await self.registration.sync.register("finn-capture-outbox");
  } catch (_error) {
    // The open page exposes manual Retry when Background Sync is unavailable.
  }
}

async function requestCaptureStoragePersistence() {
  if (!self.navigator.storage || !self.navigator.storage.persisted) {
    return "unsupported";
  }
  try {
    let persistent = await self.navigator.storage.persisted();
    if (!persistent && typeof self.navigator.storage.persist === "function") {
      persistent = await self.navigator.storage.persist();
    }
    return persistent ? "persistent" : "best_effort";
  } catch (_error) {
    return "best_effort";
  }
}

async function acceptSharedFiles(request) {
  const form = await request.formData();
  const files = form.getAll("files").filter(function (value) {
    return (
      value &&
      typeof value.size === "number" &&
      typeof value.arrayBuffer === "function"
    );
  });
  const storagePersistence = await requestCaptureStoragePersistence();
  const proofContext =
    (await self.FinnCaptureStore.getProofContext(Date.now())) || {};
  const metadata = {
    source: "share",
    intent: "receipt",
    sharedTitle: String(form.get("title") || "").slice(0, 200),
    sharedText: String(form.get("text") || "").slice(0, 2_000),
    sharedUrl: String(form.get("url") || "").slice(0, 2_000),
    proofRunId: proofContext.proofRunId || "",
    deviceCohortId: proofContext.deviceCohortId || "",
    offlineStartedAt:
      self.navigator.onLine === false ? new Date().toISOString() : null,
    storagePersistence: storagePersistence,
  };
  for (const file of files) {
    const record = self.FinnCaptureStore.createCaptureRecord(file, metadata);
    await self.FinnCaptureStore.putCapture(record);
  }
  await notifyCaptureClients();
  await registerCaptureSync();
  const destination = new URL("/upload", self.location.origin);
  destination.searchParams.set("shared", String(files.length));
  return Response.redirect(destination.href, 303);
}

self.addEventListener("sync", function (event) {
  if (event.tag === "finn-capture-outbox") {
    event.waitUntil(drainCaptureOutbox());
  }
});

self.addEventListener("message", function (event) {
  if (event.data && event.data.type === "drain-capture-outbox") {
    event.waitUntil(drainCaptureOutbox());
  }
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  const url = new URL(request.url);

  if (
    request.method === "POST" &&
    url.origin === self.location.origin &&
    url.pathname === "/share-target"
  ) {
    event.respondWith(acceptSharedFiles(request));
    return;
  }

  if (request.method !== "GET") {
    return;
  }

  if (url.origin !== self.location.origin) {
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(cacheFirst(request));
    return;
  }

  const accept = request.headers.get("accept") || "";
  if (request.mode === "navigate" || accept.indexOf("text/html") !== -1) {
    event.respondWith(networkFirstPage(request));
  }
});
