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

## Prepared writable-runtime differential

`browser-runtime-storage.yml` is a separate push-only diagnostic workflow for
`ci/browser-runtime-storage`. Its finite matrix creation order is **disk1,
tmpfs1, tmpfs2, disk2**, with `max-parallel: 2` and `fail-fast: false`. These
are four independent fresh VMs, never two modes on one VM. Actual runner start
and first-spawn times must be retained: creation order is not guaranteed start
order, and interleaving does not remove shared-host/storage noise. Complete and
retain all four attempts; do not rerun failed jobs or extend until a pattern
appears. Parent review precedes the single hosted cohort; this preparation is not
authority to execute it.

The captured observational run reproduced a live first-browser deadline with a
late listener receipt, HTTP dispatch but no readiness headers, and large cold
read/major-fault counts. Later successful launches had much lower main-process
reads/faults, but are not cold controls. A saved IP in Chrome/ld-linux is not the
fault address; host I/O pressure was higher at a later success than at failure.
This justifies one limited storage discriminator, not a browser or shipping fix.

Only the observation step's existing `TMPDIR` injection differs by mode:

- **disk:** original `$RUNNER_TEMP` runtime parent;
- **tmpfs:** existing Linux `/dev/shm`, without mounting/remounting or policy edits.

The unchanged probe makes a short unique child, places pytest basetemp there,
and removes it afterward. Consequently generated site files, browser profiles,
child TMPDIR, Linux HOME and XDG config/cache/data move together. This is a
**writable/generated runtime bundle**, not an isolated Chrome-profile or write-only
experiment. The executable, libraries, installed sandbox policy, Node, Chrome
arguments, private bus wrapper, owned group, selectors/order, observer and all
readiness/outer/cleanup budgets remain unchanged. The complete general-test
prelude and dependency setup remain on their original storage, before selection.
Observation JSON/XML stay on disk under `$RUNNER_TEMP`; profiles are not uploaded.
No browser cache is restored, and there is no prefetch, warmup or retry.

Before the unchanged probe, bounded admission checks record selected mount type,
options, capacity, sample and wall time. Refuse non-disk evidence/control roots,
non-tmpfs `/dev/shm`, symlink runtime parents, less than 256 MiB free or fewer than
1024 free inodes; never silently fall back. The probe then performs its existing
actual Unix-socket bind/path-length guard before pytest/Chrome. `noexec` on tmpfs
is not relaxed: the original Node/Chrome/session executables stay on disk, and
only generated data is relocated. This does not move the daemon's compiled-in
socket root or Chrome's existing shared-memory choices. Any ENOSPC, permission,
socket, sandbox, crash or missing-trace failure is a setup/invalid-comparison
outcome, not the original readiness symptom. Capacity admission is not a claim
that the generated workload can never exhaust storage.

Predeclared interpretation uses **only each VM's first-install journey** for the
cold comparison; retain later focus journeys as warm follow-ons, not extra cold
samples. Compare image revision, exact browser/Node/kernel/AppArmor identities,
source hashes, complete prelude, readiness phase and trace completeness first.
Different identities or unavailable/missing primary evidence limit comparison.

| Four-VM first-install outcome | Interpretation |
| --- | --- |
| Both disk fail with original signature; both tmpfs pass | Supports a writable-runtime/storage-placement contribution in this small cohort; not executable cold-page attribution, exclusivity, reliability or a shipping repair. |
| All four pass | Inconclusive: the intermittent failure did not recur. No extension. |
| Both modes fail with original signature | Moving generated runtime is insufficient; cold executable/shared-library/resource paging remains a stronger competitor. No claim storage is excluded. |
| Disk pass; tmpfs fail | Against this intervention as a remedy; classify original versus new tmpfs/setup failures before causal interpretation. |
| Mixed samples within a mode, or only latency shifts | Inconclusive/noisy at this sample size. Retain all phases; no post-hoc thresholds or extra attempts. |
| Prelude/admission fails, image mismatch, missing trace or new fatal signature | Attempt retained but not a valid matched original-symptom comparison. No replacement VM without a new reviewed decision. |

Generated Linux path/shell controls qualify admission, placement and failure
propagation only. They do not qualify native Chrome on `/dev/shm` under the hosted
AppArmor policy. Even a successful hosted differential is diagnostic evidence,
not the shipping CI check or permission to change the harness/application.
