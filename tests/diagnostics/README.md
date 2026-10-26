# First-Chrome readiness observation

Diagnostic-only lane for `ci/browser-readiness-io`. Shipping CI and application/test
source are unchanged. Run once after the original general-test prelude, on one
fresh native VM; no mode comparison, warmup, browser-cache restore or retry.

```sh
./scripts/test --ignore=tests/test_ui_theme_browser.py
export CHROMIUM_BIN=/usr/bin/google-chrome
export TMPDIR="$RUNNER_TEMP"
PYTHON_DOTENV_DISABLED=1 .venv/bin/python tests/diagnostics/readiness-probe.py \
  --root "$PWD" --output "$RUNNER_TEMP/readiness-observation"
```

The probe loads the existing three journeys in their original order. Its only
launcher delta is Node `--require` plus an observation-file destination inside the
existing reconstructed minimal environment. The Linux session wrapper, owned
process group, `communicate(timeout=60)`, Chrome arguments, fresh profiles,
readiness 5000 ms aggregate deadline and cleanup grace remain intact.

## Observations and interpretation

- `listener-announcement-observed`: monotonic **parent receipt upper bound**, via
  a watch on the existing stderr regular file; not a child emission timestamp.
  The source says whether file-watch or boundary catch-up observed it. File-watch
  callbacks can lag. Never conclude listener absence from a missing event.
- `readiness-request` / `http-request-created` / `http-client-connected` /
  `http-request-send-headers`: application and Undici client stages. The client
  connected event follows HTTP parser initialization; it is **not** TCP SYN/ACK.
  send-headers is a dispatch boundary, not proof Chrome processed the request.
- `http-response-headers` / `readiness-headers` / `readiness-body` /
  `page-websocket-handoff`: distinguish client delivery/parsing, empty target
  lists and successful page selection. No response bodies, headers or target URLs
  are collected. Request IDs correlate each poll.
- One owned process/thread snapshot at page readiness or immediately before
  failure cleanup: original stat/wchan/syscall/I/O/scheduler observations, plus
  instruction-address mapping category, executable/library basename and offset.
  Raw maps are read only locally, never written/uploaded. Mapping a stopped IP
  into Chrome is **not** identification of a faulting data page or a stack.
  Denied/missing proc files are explicit; no sudo, ptrace or policy changes.
- `boundaryElapsedMs` precedes snapshot reads; event `elapsedMs` follows them.
  `snapshotDurationMs` exposes the collector's delay. No periodic proc sampling.

Chrome 155's `StartServerOnHandlerThread` flushes the announcement on its handler
thread, then posts `ServerStartedOnUI`. `ServerWrapper::OnHttpRequest` posts
`/json` work to the UI thread; `OnJsonRequest(list)` enumerates targets and
`SendJson` posts the response to the handler thread. Therefore an announcement can
coexist with a live, I/O-blocked UI thread and no list headers before deadline.
Source: `content/browser/devtools/devtools_http_handler.cc` at `155.0.8059.39`.

## Controls

```sh
TMPDIR=/your/approved/scratch python3 tests/diagnostics/readiness-controls.py
```

Generated controls exercise delayed listener/headers/body, empty then page,
stalled headers/body, refusal, early exit, signal, ignored SIGTERM, exact helper
composition and nonzero propagation. On Linux they also bind the predicted
singleton socket and test 107/108-byte and UTF-8 guard cases. They use no Chrome.
Do **not** run controls before the hosted observation: preserve the cold case.

Upload only named JSON/XML/Chrome-stderr logs; never runtime directories,
profiles, raw environment, mappings, source or financial data. Runtime is a short
separate TMPDIR child, preflighted before pytest/Chrome and removed afterward.

A pass qualifies the observed journeys, not causality or reliability. A failing
trace classifies the next boundary; a successful trace is inconclusive for this
intermittent failure. Stop after the one lane. Before any subsequent differential,
predeclare an interleaved four-VM schedule and review the intervention separately.
