(function () {
  "use strict";

  const store = window.FinnCaptureStore;
  if (!store) {
    return;
  }

  const UPLOAD_TIMEOUT_MS = 12_000;
  const POLL_BASE_MS = 2_000;
  const POLL_MAX_MS = 30_000;
  let draining = false;
  let retryTimer = null;
  let pollTimer = null;
  let pollDelay = POLL_BASE_MS;
  let captureError = "";
  let backgroundSyncSupported = false;
  let storagePersistence = "unknown";
  let proofContext = null;
  let toastTimer = null;

  function element(id) {
    return document.getElementById(id);
  }

  function announce(message) {
    const region = element("capture-announcer");
    if (!region) return;
    region.textContent = "";
    window.setTimeout(function () {
      region.textContent = message;
    }, 20);
  }

  function notifyUpload(message, persistent) {
    const region = element("toast");
    if (!region) return;
    window.clearTimeout(toastTimer);
    const notice = document.createElement("div");
    notice.className = "capture-toast";
    let hovering = false;
    const link = document.createElement("a");
    link.className = "capture-toast-link";
    link.href = "/processing";
    link.textContent = message + " · View processing";
    const dismiss = actionButton("Dismiss", "capture-toast-dismiss", function () {
      window.clearTimeout(toastTimer);
      notice.remove();
    });
    notice.append(link, dismiss);
    function expire() {
      window.clearTimeout(toastTimer);
      if (!persistent) toastTimer = window.setTimeout(function () {
        if (!hovering && !notice.contains(document.activeElement)) notice.remove();
      }, 8_000);
    }
    notice.addEventListener("pointerenter", function () { hovering = true; window.clearTimeout(toastTimer); });
    notice.addEventListener("focusin", function () { window.clearTimeout(toastTimer); });
    notice.addEventListener("pointerleave", function () { hovering = false; expire(); });
    notice.addEventListener("focusout", expire);
    region.replaceChildren(notice);
    expire();
  }

  function displayStatus(record) {
    const evictionProtected =
      record.storagePersistence === "persistent" ||
      storagePersistence === "persistent";
    if (record.state === "pending") {
      return {
        label: "Pending",
        detail:
          navigator.onLine === false
            ? evictionProtected
              ? "Stored in protected device storage; waiting for a connection."
              : "Written to this browser's outbox; keep the original until Saved because storage eviction is possible."
            : evictionProtected
              ? "Stored in protected device storage; waiting to send."
              : "Written to this browser's outbox; keep the original until Saved because storage eviction is possible.",
      };
    }
    if (record.state === "sending") {
      return {
        label: "Pending",
        detail: "Uploading. Keep the original until Saved.",
      };
    }
    if (record.state === "retry") {
      return record.autoRetry === false
        ? {
            label: "Retry needed",
            detail: "The original remains on this device. Retry when ready.",
          }
        : {
            label: "Pending",
            detail: "The original remains on this device; retrying automatically.",
          };
    }
    if (record.state === "saved") {
      return {
        label: "Saved",
        detail: "The server stored the original; it is queued for processing.",
      };
    }
    if (record.state === "processing") {
      return {
        label: "Processing",
        detail: "Reading the file.",
      };
    }
    if (record.state === "needs_review") {
      return {
        label: "Needs review",
        detail: "The original is safe; confirm the extracted details.",
      };
    }
    if (record.state === "filed") {
      return {
        label: "Filed",
        detail: "The saved capture has been added to the ledger.",
      };
    }
    return record.file
      ? {
          label: "Failed",
          detail: "The original remains on this device. Retry or remove it.",
        }
      : {
          label: "Failed after Saved",
          detail: "The server has the original, but processing needs attention.",
        };
  }

  function actionButton(label, className, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", handler);
    return button;
  }

  function captureItem(record) {
    const item = document.createElement("article");
    item.className = "capture-status-item state-" + record.state;
    item.setAttribute("role", "listitem");
    item.dataset.captureId = record.id;
    const idBase = "capture-" + record.id;

    const copy = document.createElement("div");
    copy.className = "capture-status-copy";

    const title = document.createElement("strong");
    title.id = idBase + "-name";
    title.textContent = record.name || "Capture";
    copy.appendChild(title);

    const status = displayStatus(record);
    const state = document.createElement("span");
    state.id = idBase + "-state";
    state.className = "capture-state";
    state.textContent = status.label;
    copy.appendChild(state);

    const detail = document.createElement("span");
    detail.id = idBase + "-detail";
    detail.className = "capture-detail";
    detail.textContent = status.detail;
    copy.appendChild(detail);

    if (record.lastError && record.state !== "filed") {
      const error = document.createElement("span");
      error.id = idBase + "-error";
      error.className = "capture-error";
      error.textContent = record.lastError;
      copy.appendChild(error);
      item.setAttribute(
        "aria-describedby",
        idBase + "-detail " + idBase + "-error"
      );
    } else {
      item.setAttribute("aria-describedby", idBase + "-detail");
    }
    item.setAttribute("aria-labelledby", idBase + "-name " + idBase + "-state");
    item.appendChild(copy);

    const actions = document.createElement("div");
    actions.className = "capture-status-actions";
    if (
      ["pending", "retry", "failed"].indexOf(record.state) !== -1 &&
      record.file
    ) {
      actions.appendChild(
        actionButton(
          "Retry " + (record.name || "capture"),
          "ghost compact-action",
          function () {
            manualRetry(record.id);
          }
        )
      );
    }
    if (record.state === "needs_review") {
      const link = document.createElement("a");
      link.className = "link compact-action";
      link.href = "/review";
      link.textContent = "Review";
      actions.appendChild(link);
    } else if (record.state === "filed") {
      const link = document.createElement("a");
      link.className = "link compact-action";
      link.href = "/activity";
      link.textContent = "View";
      actions.appendChild(link);
    } else if (record.state === "failed" && !record.file) {
      const link = document.createElement("a");
      link.className = "link compact-action";
      link.href = "/processing";
      link.textContent = "Refresh status";
      actions.appendChild(link);
    }

    const removable =
      record.state !== "sending" &&
      (record.file ||
        record.state === "filed" ||
        record.state === "needs_review" ||
        record.state === "failed");
    if (removable) {
      actions.appendChild(
        actionButton(
          record.file ? "Remove" : "Dismiss",
          "ghost compact-action",
          function () {
            removeCapture(record);
          }
        )
      );
    }
    item.appendChild(actions);
    return item;
  }

  async function render() {
    const center = element("capture-center");
    const list = element("capture-status-list");
    const count = element("capture-count");
    const syncNote = element("capture-sync-note");

    let records;
    try {
      records = await store.listCaptures();
    } catch (error) {
      captureError =
        "Device storage is unavailable. Keep your originals; uploads cannot start.";
      if (center && syncNote) {
        center.hidden = false;
        syncNote.textContent = captureError;
      }
      notifyUpload(captureError, true);
      announce("The capture outbox is unavailable.");
      return;
    }
    const navCount = element("processing-nav-count");
    if (navCount) {
      const unfinished = records.filter(function (record) { return record.state !== "filed"; }).length;
      navCount.textContent = String(unfinished);
      navCount.hidden = unfinished === 0;
    }
    if (!center || !list || !count || !syncNote) return;
    // Keep device-owned originals until acknowledgement. A visible server row
    // can represent an acknowledged file without showing it twice.
    const serverIds = new Set(Array.from(document.querySelectorAll("[data-server-document-id]"))
      .map(function (node) { return Number(node.dataset.serverDocumentId); }));
    records = records.filter(function (record) { return record.file || !serverIds.has(record.serverDocumentId); });
    center.hidden = records.length === 0 && !captureError;
    count.textContent = String(records.length);
    // Device-owned originals are never capped or hidden behind history paging.
    list.replaceChildren(...records.map(captureItem));

    const deviceOwned = records.some(function (record) {
      return (
        record.file &&
        ["pending", "sending", "retry", "failed"].indexOf(record.state) !== -1
      );
    });
    const notices = [];
    if (captureError) notices.push(captureError);
    if (deviceOwned && storagePersistence !== "persistent") {
      notices.push(
        "Browser storage is not protected from eviction. Keep the original until status reaches Saved."
      );
    }
    if (deviceOwned && backgroundSyncSupported) {
      notices.push(
        "Pending originals retry automatically when the browser allows it."
      );
    } else if (deviceOwned) {
      notices.push(
        "Background retry is unavailable here. Keep this page open or use Retry after reconnecting."
      );
    }
    syncNote.textContent = notices.join(" ");
  }

  async function mutateState(id, expected, event, patch) {
    return store.updateCapture(id, function (record) {
      if (record.state !== expected) return record;
      record.state = store.nextState(record.state, event);
      return Object.assign(record, patch || {});
    });
  }

  async function registerBackgroundSync() {
    backgroundSyncSupported = false;
    if (!("serviceWorker" in navigator)) return false;
    try {
      const registration = await navigator.serviceWorker.ready;
      if (!registration.sync) return false;
      await registration.sync.register("finn-capture-outbox");
      backgroundSyncSupported = true;
      return true;
    } catch (_error) {
      return false;
    }
  }

  async function refreshStoragePersistence(requestProtection) {
    if (!navigator.storage || !navigator.storage.persisted) {
      storagePersistence = "unsupported";
      return storagePersistence;
    }
    try {
      let persistent = await navigator.storage.persisted();
      if (
        !persistent &&
        requestProtection &&
        typeof navigator.storage.persist === "function"
      ) {
        persistent = await navigator.storage.persist();
      }
      storagePersistence = persistent ? "persistent" : "best_effort";
    } catch (_error) {
      storagePersistence = "best_effort";
    }
    return storagePersistence;
  }

  function retryPatch(record, message) {
    const automatic = Number(record.attempts || 0) < store.MAX_AUTO_ATTEMPTS;
    return {
      leaseUntil: 0,
      autoRetry: automatic,
      nextAttemptAt: automatic
        ? Date.now() + store.retryDelay(record.attempts, Math.random())
        : 0,
      lastError: message,
    };
  }

  async function sendCapture(record) {
    if (!record.file) {
      await mutateState(record.id, "sending", "FAIL", {
        leaseUntil: 0,
        autoRetry: false,
        lastError: "The device copy is unavailable.",
      });
      return;
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

    const controller = new AbortController();
    const timeout = window.setTimeout(function () {
      controller.abort();
    }, UPLOAD_TIMEOUT_MS);
    const requestStartedAt = Date.now();
    let response;
    try {
      response = await fetch("/captures", {
        method: "POST",
        body: form,
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
    } catch (error) {
      window.clearTimeout(timeout);
      const message =
        error && error.name === "AbortError"
          ? "The acknowledgement timed out; retrying with the same capture id."
          : "Could not reach the server.";
      await mutateState(record.id, "sending", "RETRY", retryPatch(record, message));
      await registerBackgroundSync();
      return;
    }
    window.clearTimeout(timeout);

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
      await mutateState(record.id, "sending", "ACK", {
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
      await reportClientEvent(
        record.id,
        "online_durable_ack",
        onlineDurableAckMs,
        Number(record.attempts || 1),
        "onlineAckTelemetryRecorded"
      );
      if (offlineRecoveryMs !== null && offlineRecoveryMs !== undefined) {
        await reportClientEvent(
          record.id,
          "offline_recovered",
          offlineRecoveryMs,
          offlineRecoverySequence,
          "offlineTelemetryRecorded"
        );
      }
      announce("Saved " + record.name + ". The server stored the original.");
      if (payload.server_state && payload.server_state !== "queued") {
        await applyServerStatus(record.id, payload);
      }
      pollDelay = POLL_BASE_MS;
      await scheduleServerPoll(POLL_BASE_MS);
      return;
    }

    const detail =
      payload && typeof payload.detail === "string"
        ? payload.detail
        : "The server did not acknowledge this capture.";
    if (store.isTransientStatus(response.status)) {
      await mutateState(record.id, "sending", "RETRY", retryPatch(record, detail));
      await registerBackgroundSync();
    } else {
      await mutateState(record.id, "sending", "FAIL", {
        leaseUntil: 0,
        autoRetry: false,
        nextAttemptAt: 0,
        lastError: detail,
      });
      announce("Capture failed. The original remains on this device. Retry is available.");
    }
  }

  async function scheduleRetry() {
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
    const records = await store.listCaptures();
    const times = records
      .filter(function (record) {
        return record.state === "retry" && record.autoRetry !== false;
      })
      .map(function (record) {
        return Number(record.nextAttemptAt || Date.now());
      });
    if (!times.length) return;
    const wait = Math.max(0, Math.min.apply(Math, times) - Date.now());
    retryTimer = window.setTimeout(drain, wait);
  }

  async function reportClientEvent(
    id,
    eventName,
    durationMs,
    sequenceNo,
    recordedField
  ) {
    const form = new FormData();
    form.append("event", eventName);
    form.append("duration_ms", String(Math.max(0, Math.round(durationMs))));
    form.append("sequence_no", String(Math.max(1, Number(sequenceNo) || 1)));
    try {
      const response = await fetch(
        "/captures/" + encodeURIComponent(id) + "/client-event",
        {
          method: "POST",
          body: form,
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        }
      );
      if (!response.ok) return false;
      await store.updateCapture(id, function (record) {
        record[recordedField] = true;
        return record;
      });
      return true;
    } catch (_error) {
      return false;
    }
  }

  async function flushClientTelemetry() {
    if (navigator.onLine === false) return;
    const records = await store.listCaptures();
    for (const record of records) {
      if (
        record.onlineAckTelemetryRecorded === false &&
        record.onlineDurableAckMs !== null &&
        record.onlineDurableAckMs !== undefined &&
        Number.isFinite(Number(record.onlineDurableAckMs))
      ) {
        await reportClientEvent(
          record.id,
          "online_durable_ack",
          Number(record.onlineDurableAckMs),
          Number(record.attempts || 1),
          "onlineAckTelemetryRecorded"
        );
      }
      if (
        record.offlineTelemetryRecorded === false &&
        record.offlineRecoveryMs !== null &&
        record.offlineRecoveryMs !== undefined &&
        Number.isFinite(Number(record.offlineRecoveryMs))
      ) {
        await reportClientEvent(
          record.id,
          "offline_recovered",
          Number(record.offlineRecoveryMs),
          Number(record.offlineRecoverySequence || 1),
          "offlineTelemetryRecorded"
        );
      }
    }
  }

  async function markPendingOffline(now) {
    const when = Number(now || Date.now());
    const records = await store.listCaptures();
    for (const record of records) {
      if (
        record.file &&
        !record.offlineStartedAt &&
        ["pending", "sending", "retry", "failed"].indexOf(record.state) !== -1
      ) {
        await store.updateCapture(record.id, function (current) {
          if (current.file && !current.offlineStartedAt &&
              ["pending", "sending", "retry", "failed"].includes(current.state)) {
            current.offlineStartedAt = new Date(when).toISOString();
          }
          return current;
        });
      }
    }
  }

  async function markPendingReconnected(now) {
    const when = Number(now || Date.now());
    const records = await store.listCaptures();
    for (const record of records) {
      const started = Date.parse(record.offlineStartedAt || "");
      if (
        record.file &&
        ["pending", "retry", "failed"].indexOf(record.state) !== -1 &&
        Number.isFinite(started)
      ) {
        await store.updateCapture(record.id, function (current) {
          // The snapshot can race another online event or durable acknowledgement.
          // Fence the same offline episode inside the IndexedDB update.
          if (!current.file || current.offlineStartedAt !== record.offlineStartedAt ||
              !["pending", "retry", "failed"].includes(current.state)) return current;
          current.offlineStartedAt = null;
          current.offlineRecoveryMs = Math.max(0, when - started);
          current.offlineRecoverySequence =
            Number(current.offlineRecoverySequence || 0) + 1;
          current.offlineTelemetryRecorded = false;
          return current;
        });
      }
    }
  }

  async function drain() {
    if (draining || navigator.onLine === false) {
      await render();
      return;
    }
    draining = true;
    try {
      await store.recoverExpiredLeases(Date.now());
      for (let index = 0; index < 20; index += 1) {
        const record = await store.claimDueCapture(Date.now(), false);
        if (!record) break;
        await render();
        await sendCapture(record);
        await render();
      }
      await scheduleRetry();
      await flushClientTelemetry();
      await scheduleServerPoll(POLL_BASE_MS);
      const remaining = await store.listCaptures();
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
        window.setTimeout(drain, 0);
      }
    } catch (error) {
      announce("Capture recovery needs attention.");
    } finally {
      draining = false;
    }
  }

  async function applyServerStatus(id, status) {
    const record = await store.getCapture(id);
    if (!record) return false;
    const previousState = record.state;
    const patch = {
      serverDocumentId: status.source_document_id,
      serverState: status.server_state,
      lastError: status.server_state === "failed" ? "Server processing failed." : "",
    };
    if (status.server_state === "processing" && record.state === "saved") {
      await mutateState(id, "saved", "PROCESS", patch);
    } else if (
      status.server_state === "needs_review" &&
      (record.state === "saved" || record.state === "processing")
    ) {
      await mutateState(id, record.state, "NEEDS_REVIEW", patch);
      announce(record.name + " needs review.");
    } else if (
      status.server_state === "logged" &&
      ["saved", "processing", "needs_review"].indexOf(record.state) !== -1
    ) {
      await mutateState(id, record.state, "FILED", patch);
      announce(record.name + " is filed.");
    } else if (
      status.server_state === "failed" &&
      (record.state === "saved" || record.state === "processing")
    ) {
      await mutateState(id, record.state, "FAIL", patch);
      announce(record.name + " failed during server processing.");
    } else {
      await store.updateCapture(id, function (current) {
        return Object.assign(current, patch);
      });
    }
    const updated = await store.getCapture(id);
    return Boolean(updated && updated.state !== previousState);
  }

  function stopServerPoll() {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  async function scheduleServerPoll(delay) {
    stopServerPoll();
    if (document.hidden || navigator.onLine === false) return;
    const records = await store.listCaptures();
    const hasServerOwnedWork = records.some(function (record) {
      return record.state === "saved" || record.state === "processing";
    });
    if (!hasServerOwnedWork) return;
    pollTimer = window.setTimeout(
      pollServerStates,
      Math.max(0, Number(delay) || 0)
    );
  }

  async function pollServerStates(includeNeedsReview) {
    stopServerPoll();
    if (document.hidden || navigator.onLine === false) return;
    const records = await store.listCaptures();
    const candidates = records.filter(function (record) {
      return (
        record.state === "saved" ||
        record.state === "processing" ||
        (includeNeedsReview === true && record.state === "needs_review")
      );
    });
    if (!candidates.length) return;

    let changed = false;
    await Promise.all(
      candidates.map(async function (record) {
        try {
          const response = await fetch("/captures/" + encodeURIComponent(record.id), {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          });
          if (!response.ok) return;
          changed =
            (await applyServerStatus(record.id, await response.json())) || changed;
        } catch (_error) {
          // The durable server copy already exists; polling failure is not data loss.
        }
      })
    );
    await render();
    pollDelay = changed
      ? POLL_BASE_MS
      : Math.min(Math.max(POLL_BASE_MS, pollDelay * 2), POLL_MAX_MS);
    await scheduleServerPoll(pollDelay);
  }

  async function manualRetry(id) {
    const record = await store.getCapture(id);
    if (!record || !record.file) return;
    if (record.state === "retry" || record.state === "failed") {
      await mutateState(id, record.state, "MANUAL_RETRY", {
        autoRetry: true,
        nextAttemptAt: 0,
        leaseUntil: 0,
        lastError: "",
      });
    }
    announce("Retrying " + record.name + ".");
    await render();
    await registerBackgroundSync();
    await render();
    drain();
  }

  async function removeCapture(record) {
    if (
      record.file &&
      !window.confirm(
        "Remove " +
          record.name +
          "? The server has not confirmed a durable copy, so this discards the pending original."
      )
    ) {
      return;
    }
    await store.deleteCapture(record.id);
    announce((record.file ? "Removed pending " : "Dismissed ") + record.name + ".");
    await render();
  }

  async function enqueueFiles(files, metadata) {
    const selected = Array.from(files || []);
    if (!selected.length) return false;
    let added = 0;
    try {
      const persistence = await refreshStoragePersistence(true);
      for (const file of selected) {
        const record = store.createCaptureRecord(
          file,
          Object.assign({}, metadata, proofContext || {}, {
            storagePersistence: persistence,
            offlineStartedAt:
              navigator.onLine === false
                ? new Date().toISOString()
                : null,
          })
        );
        await store.putCapture(record);
        added += 1;
        announce(
          "Pending " +
            record.name +
            ". The original is written to this browser's device outbox."
        );
      }
      captureError = "";
    } catch (error) {
      captureError =
        "The selected original could not be saved on this device. Free browser storage, then select it again.";
      announce(
        captureError
      );
      notifyUpload(added + " of " + selected.length + " files added. " + captureError, true);
      await render();
      if (added) drain();
      return false;
    }
    notifyUpload(added + (added === 1 ? " file" : " files") + " queued", false);
    await render();
    await registerBackgroundSync();
    await render();
    drain();
    return true;
  }

  function bindCaptureInputs() {
    document.querySelectorAll("[data-capture-picker]").forEach(function (button) {
      button.addEventListener("click", function () {
        const input = element(button.dataset.inputId);
        if (input) input.click();
      });
    });
    document.querySelectorAll("[data-capture-input]").forEach(function (input) {
      input.addEventListener("change", async function () {
        const accepted = await enqueueFiles(input.files, {
          source: input.dataset.source || "file",
          intent: input.dataset.intent || "receipt",
        });
        if (accepted) {
          input.value = "";
          const open = document.querySelector("details.fab[open]");
          if (open) open.open = false;
        }
      });
    });
    document.querySelectorAll("[data-capture-form]").forEach(function (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        const input = form.querySelector('input[type="file"]');
        const accepted = await enqueueFiles(input && input.files, {
          source: form.dataset.source || "file",
          intent: form.dataset.intent || "receipt",
        });
        if (accepted && input) input.value = "";
      });
    });
  }

  async function importFallbackShareCaptures() {
    const nodes = Array.from(document.querySelectorAll("[data-server-capture-id]"));
    for (const node of nodes) {
      const id = node.dataset.serverCaptureId;
      if (!id) continue;
      try {
        const response = await fetch("/captures/" + encodeURIComponent(id), {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!response.ok) continue;
        const status = await response.json();
        await store.importDurableCapture(status);
        notifyUpload("Shared files saved", false);
        announce("Saved shared capture. The server stored the original.");
      } catch (_error) {
        announce("Could not load the shared capture status.");
      }
    }
  }

  async function resumeServerTracking(includeNeedsReview) {
    try {
      await render();
      pollDelay = POLL_BASE_MS;
      await pollServerStates(includeNeedsReview);
    } catch (_error) {
      announce("Capture status refresh needs attention.");
    }
  }

  async function init() {
    const proofForm = document.querySelector("[data-capture-form]");
    if (
      proofForm &&
      proofForm.dataset.proofRunId &&
      proofForm.dataset.deviceCohortId
    ) {
      proofContext = {
        proofRunId: proofForm.dataset.proofRunId,
        deviceCohortId: proofForm.dataset.deviceCohortId,
      };
      await store.setProofContext({
        ...proofContext,
        expiresAt: Date.now() + 4 * 60 * 60 * 1000,
      });
    } else {
      proofContext = await store.getProofContext(Date.now());
    }
    bindCaptureInputs();
    window.addEventListener("online", async function () {
      announce("Back online. Retrying pending captures.");
      try {
        await markPendingReconnected(Date.now());
      } catch (_error) {
        announce("Back online. Recovery timing could not be recorded.");
      }
      drain();
      flushClientTelemetry();
      resumeServerTracking(true);
    });
    window.addEventListener("offline", async function () {
      announce(
        "Offline. Pending originals remain in the browser outbox; keep the original until Saved."
      );
      stopServerPoll();
      try {
        await markPendingOffline(Date.now());
      } catch (_error) {
        announce("Offline. Capture status could not be updated.");
      }
      render();
    });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        stopServerPoll();
      } else if (navigator.onLine !== false) {
        resumeServerTracking(true);
      }
    });
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.addEventListener("message", function (event) {
        if (event.data && event.data.type === "capture-outbox-updated") {
          resumeServerTracking(false);
        }
      });
    }
    await store.recoverExpiredLeases(Date.now());
    if (navigator.onLine === false) {
      await markPendingOffline(Date.now());
    }
    await importFallbackShareCaptures();
    await refreshStoragePersistence(false);
    await registerBackgroundSync();
    await render();
    await flushClientTelemetry();
    drain();
    resumeServerTracking(true);
  }

  document.addEventListener("DOMContentLoaded", function () {
    init().catch(function () {
      announce("Durable capture could not start in this browser.");
      notifyUpload("Uploads unavailable. Keep your originals", true);
    });
  });
})();
