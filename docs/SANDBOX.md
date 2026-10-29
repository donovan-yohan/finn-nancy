# Disposable local sandbox

Use the existing locked `.venv`. The launcher does not install dependencies.

```sh
scripts/sandbox setup --scenario household-v1
scripts/sandbox replay --scenario household-v1 --journey match-receipt-v1
# The accepted seven-transaction regression fixture remains available.
scripts/sandbox setup --scenario showcase-v1
scripts/sandbox replay --scenario showcase-v1 --journey match-receipt-v1
```

`setup` prints one JSON readiness record with the local `origin`, `run_id`,
`manifest_path`, fixed month and semantic fixture IDs. It stays in the foreground.
Ctrl-C or SIGTERM stops and reaps its child process group, then removes its own
new workspace. An interrupted setup exits 130. Starting the command again is a
reset into a different workspace and database identity, not a destructive reseed.
The parent checks real `/healthz` and `/manage` HTTP responses before readiness.
If the child dies, setup fails and cleans up rather than advertising a dead URL.

`replay` starts its own fresh instance, GETs reconciliation, POSTs the existing
`/recon/line/{id}/confirm` action and follows its response. It reads back SQLite
and the real period-statement CSV export. Success JSON appears only after the
assertions and verified resource cleanup. A workload or cleanup failure is not
success. There is no new application endpoint, global sandbox flag or mock model.

Only `empty-v1`, `showcase-v1` and `household-v1` exist. The journey is supported
for `showcase-v1` and `household-v1`. Unknown names/options and abbreviated options
fail before state creation. There are no caller database, source, state, port, credential,
service-URL or reset-path arguments.

## Shared scenario contract

`showcase-v1` is generated synthetic data for June 2026. It adapts the existing
media household recipe into repository-owned code. It contains two local
accounts, Generated Alex and Generated Jo, three budgets, accepted synthetic
merchant/category decisions and seven transactions with seven signed splits.
The savings legs have an explicit validated `transfer_pair` relationship through
the shared flow service, not a relationship inferred from signs.

The generated market original is attached to the existing -6842-cent purchase.
A generated CSV statement stages one corresponding line for review without
booking it. Matching clears that same transaction and retains both originals.
It does not create an eighth expense. Pantry and cafe images under `artifacts/`
are available for later upload demonstrations. They are generated images, not
claims that extraction ran. Workers are disabled, so uploaded documents do not
magically finish ingestion.

| Verified report control | Cents |
| --- | ---: |
| Income | 425000 |
| Net money out | 169787 |
| Transfer neutrality | 0 |
| Signed ledger movement | 255213 |
| Cash ledger movement from zero | 205213 |
| Savings ledger movement from zero | 50000 |

These are ledger/report values, not asserted statement closing balances. The
period starts open with unresolved evidence. A demo is not a clean-close proof.
`empty-v1` migrates a fresh database without sample households or transactions.

## Household for public media

All people, accounts, employers, merchants and documents in this demo are
fictional. Put this disclosure once in the surrounding published caption or demo
context. The receipt images and private transaction notes retain synthetic
provenance without repeating placeholder prefixes in the ordinary ledger UI.

`household-v1` is a separate June 2026 scenario. `showcase-v1` keeps its original
seven transactions, seven splits and literal totals unchanged. The household
has active local members Alex and Jo, four local accounts, seventeen canonical
transactions with seventeen signed splits, and six category budgets. Every
purchase split has explicit accepted merchant and category evidence. Income
uses `income`; the three validated `transfer_pair` relationships join six
`internal_transfer` legs. Shared purchases are booked once, on Household
expenses. Transfers into that account are not income or additional spending.

| Report control | Cents |
| --- | ---: |
| Income | 790000 |
| Net money out | 244147 |
| External cash movement / signed ledger movement | 545853 |
| Transfer neutrality | 0 |
| Alex chequing ledger movement from zero | 260950 |
| Jo chequing ledger movement from zero | 230860 |
| Household expenses ledger movement from zero | 24043 |
| Household savings ledger movement from zero | 30000 |
| Shared household purchases, included once above | 220957 |
| Unresolved purchase splits / excluded expense cents | 0 / 0 |

Alex chequing contains Harbor Studio payroll of 425000 cents and 14050 cents of
transport spending. Jo chequing contains Juniper Library payroll of 365000 cents
and 9140 cents of book/cafe spending. Alex contributes 150000 cents and Jo
125000 cents into Household expenses. That account pays rent, groceries,
utilities and internet, then transfers 30000 cents into Household savings.

The three shared budgets are Groceries (65000 cents), Home and utilities
(200000) and Internet (9000). Alex is the actual persisted budget owner for
Transport (15000). Jo is the actual persisted budget owner for Books and hobbies
(10000) and Dining out (8000).

**Local attribution limit.** Accounts have no member-owner foreign key. These
are coherent named local accounts, not persisted personal ownership or access
permissions. The account/person associations in the test's `STORY` manifest are
demo associations only. Budget ownership is real but is not transaction
ownership. Canonical provider economic-owner bindings require bank/provider
records; this scenario creates none, and creates no consent or credentials.
Connections can honestly show the two local members, not a bank connection,
authenticated multi-user logins or economic-owner permissions. Do not imply
those unsupported features in media.

The Cedar Market purchase (`ids.transactions["market"]`) is -6842 cents. Its
receipt is already linked; one corresponding statement line
(`ids.statement_lines["market"]`) is pre-staged as `needs_review`, not booked.
Use the real confirm action to attach that evidence. Ledger count, split bytes,
report totals and both original hashes remain unchanged, and the shared Activity
source disclosure exposes both originals after matching. A primary report-ready
screenshot may perform this match first. For a reconciliation clip, start a fresh
run to retain the awaiting-confirm state. Say that the statement was staged
before the clip; no parser/model success is claimed.

`artifacts/market-receipt.png` is the already-booked Cedar Market original.
`artifacts/pantry-receipt.png` and `artifacts/cafe-receipt.png` depict their
already-booked purchases with matching dates and amounts. They may demonstrate
upload acknowledgement only in a separate fresh run. Workers remain off. Do not
suggest they produced additional ledger effects or automatic extraction.

All four account statement policies remain unconfigured. No statement balance
assertions, policy exemptions, close acknowledgements or clean-close snapshot
are invented. The report excludes all four accounts from verified liquid
position as `missing_stated_as_of_balance`; its zero included liquid subtotal is
**not** a zero household ledger balance. The actual ledger movements are above.
Matching the one line does not make the month complete or enable automation.

Final media consumers must use `launch_sandbox("household-v1")`, its returned
IDs/root and a fresh browser profile, not a second seed server. This command
supplies data and HTTP actions, not screenshots, videos or browser acceptance.

## Python consumers

From this checkout using the installed environment:

```python
import httpx
from tools.sandbox.runner import launch_sandbox

with launch_sandbox("household-v1") as run:
    fixture = run.fixture
    market_id = fixture.ids.transactions["market"]
    with httpx.Client(base_url=run.origin, trust_env=False) as client:
        assert client.get("/activity", params={"month": fixture.month}).status_code == 200
# run.cleanup records process_reaped/root_removed after successful cleanup.
```

`ScenarioFixture` returns version, month, generated root, typed ID maps, expected
integer-cent totals and relative routes. `SandboxRun` adds origin, run identity
and the private sidecar path. IDs are actual inserted IDs. Do not assume row 1.
Every call starts a separate process. Production settings and socket functions
are never patched in the calling pytest process.

## Isolation and limits

The supervisor allocates a new private `finn-sandbox-*` directory through the
platform temporary allocator (`TMPDIR` selects its scratch parent). It never
adopts or deletes a caller path. The root's inode and ownership receipt are
checked before recursive removal. Cleanup stops the child first and refuses
ownership mismatches. SIGKILL of the supervisor, machine failure or filesystem
failure can leave state; there is intentionally no arbitrary-path janitor.

The actual socket binds `127.0.0.1:0` and remains open across handoff to Uvicorn.
There is no close/rebind port race. The child uses isolated Python startup,
a new cwd/HOME/temp/cache/checkpoint directory and an explicit environment
allowlist. It does not inherit Settings, proxies, tokens, `PYTHONPATH` or the
operator's home. Dotenv reads are refused and Pydantic dotenv loading is disabled
before importing the app module, which has an import-time app instance.

The unmodified FastAPI factory runs with Uvicorn `lifespan="off"`. No lifespan
worker, warmup, Telegram or inbox watcher starts. Plaid, search, embeddings, chat
and credential paths are unconfigured/off. A real provider-service start attempt
must return `egress_blocked` during startup.

A child-only Python network guard refuses DNS, connect/connect_ex, datagrams,
sendmsg and outgoing stream sends, including loopback, IPv6 and Unix sockets.
Only accepted inbound HTTP sockets may send responses. The one precreated
asyncio loop's own socketpair sender may wake that loop; this is an exact object
exception, not a loopback or Unix egress exemption. Startup executes guarded
attempts and checks that local TCP/UDP sentinels received no packets. CPython
audit hooks also refuse raw socket connects and subprocess launch. This protects
against accidental Python egress in a trusted harness. It is **not an OS sandbox**
against malicious/native code and does not qualify arbitrary plugin execution.

No browser opens automatically. Use a fresh disposable browser profile/private
context, not your ordinary browser profile or installed PWA. Automated browser
consumers must create fresh storage and enforce foreign-origin blocking, then
close their browser before its profile is removed. Ephemeral ports can be reused;
a fresh database does not reset service workers, IndexedDB or pending captures.
Never clear a person's ordinary storage. Browser/media integration is deferred.

This unauthenticated local app is not approved for public hosting, a tunnel,
tailnet publishing, LAN exposure or a persistent service. Container isolation,
workers/models, provider integration, device access and stress/migration scenarios
remain separate qualification. This command does not claim those are tested.

## Focused verification

```sh
scripts/test tests/test_sandbox.py tests/test_sandbox_demo_household.py
```

The focused suite executes the real setup/replay commands, simultaneous instances,
HTTP reconciliation, persisted signed totals and original hashes, restart,
hostile inherited environment, generated dotenv refusal, input rejection,
SIGINT/SIGTERM cleanup, child failure and readiness deadline cleanup. Run it with
the repository's usual dotenv-safe generated test environment. It is not a
full-suite, browser or production-lifespan acceptance run.
