# Hermes-powered chat

## Ownership

The web chat uses the native Hermes JSON-RPC WebSocket gateway. Finn Nancy
remains responsible for documents, bookkeeping, reconciliation, approvals of
financial changes, and durable ingestion jobs. Hermes owns conversations,
model execution, tool calls, and transcript persistence.

The browser connects only to `/chat/socket` on the Finn Nancy origin. It cannot
select a gateway, profile, model, working directory, or Hermes session ID, and
cannot forward arbitrary JSON-RPC methods. The server pins these choices.
The former `/chat/stream` and `/chat/ping` endpoints are removed; unavailable
Hermes does not silently fall back to the legacy agent. Existing LangGraph
checkpoints are preserved but are not imported into Hermes conversations.

Implemented interactions:

- Streaming replies and expandable tool activity.
- Canonical transcript and in-flight recovery after reconnect.
- New conversation and explicit Stop.
- Single and batched clarification questions.
- Matching pending approval requests, restricted to Allow once or Deny.
- Existing transaction “why?” links prefill a new composer without sending.

Unsupported requests, including secret/password collection and desktop control,
receive a JSON-RPC error immediately. They are not silently approved.
Attachments continue through Upload and Processing; chat is not another intake
pipeline. Replacing chat does not change statement extraction timeouts.

## Configuration and authority boundary

Chat is disabled until `HERMES_CHAT_URL` is set. Configure:

| Setting | Meaning |
| --- | --- |
| `HERMES_CHAT_URL` | Native gateway URL, for example `ws://127.0.0.1:9119/api/ws` |
| `HERMES_CHAT_PROFILE` | Server-selected finance profile; default `namako-finance` |
| `HERMES_CHAT_TOKEN` | Server-only gateway session credential |
| `HERMES_CHAT_TOKEN_FILE` | Alternative private file containing that credential |

Set exactly one credential source. Literal loopback addresses with an explicit
port are required. Remote hosts, DNS aliases, URL credentials, query strings,
fragments and redirects are rejected. The transport disables environment
proxies. Never send a gateway credential to the browser or put it in source
control. Treat gateway logs as private because the native loopback WebSocket
authentication protocol carries the credential in its upgrade query string.

Use a dedicated, finance-scoped Hermes backend, not an unrestricted shared
admin gateway. A typical launch is:

```sh
hermes -p namako-finance serve --isolated --host 127.0.0.1 --port 9119 --skip-build
```

Provision the same generated secret into the backend's
`HERMES_DASHBOARD_SESSION_TOKEN` and one Finn Nancy credential source through
the operator's secret-management/service mechanism. Keep the native backend on
loopback. `--isolated` is important: a profile launch otherwise may attach to
the machine-wide backend. Do not stop or migrate an unrelated messaging
multiplexer to enable this integration.

**The bridge is not a sandbox for the Hermes agent.** The selected profile's
tools, memory providers, plugins, model endpoints and fallbacks determine its
actual authority and egress. Finn Nancy's `STRICT_LOCAL_MODE` does not configure
those separate Hermes subsystems. Before using real financial data:

1. Pin primary and all auxiliary model routes to the approved local endpoint;
   clear cloud fallback chains and set the correct wire protocol explicitly.
2. Review or disable external memory providers and plugin hooks. A local model
   does not make an external memory provider local.
3. Restrict the dedicated backend to clarification and the read-only finance
   MCP server below. Do not grant terminal, file, browser, arbitrary network,
   delegation, or general-purpose execution tools to this web-facing profile.
4. Verify the resolved tool inventory and a synthetic report call before
   enabling the production configuration. Configuration intent alone is not
   evidence of effective runtime permissions.

Use `hermes -p PROFILE config set` with individual dotted keys. Passing a JSON
object to the special `model` setting can store it as a model-name string;
verify actual types and effective routing. For an OpenAI-compatible local
endpoint, explicitly set `model.api_mode` to `chat_completions` as well as
`model.provider`, `model.default` and `model.base_url`.

The application itself still has no user authentication. Serve it only on
loopback or behind the existing authenticated private overlay. The opaque,
HttpOnly, SameSite=Strict conversation cookie is a bearer capability, not a
multi-user account/tenant system. TLS requests receive Secure cookies. Incoming
WebSocket upgrades require an exact same-origin header.

## Read-only finance tools

Install the optional MCP group and launch the bounded tool surface:

```sh
uv sync --frozen --group mcp
fn mcp-serve --read-only --db /path/to/finance.sqlite
```

Configure that command as a stdio MCP server in the dedicated Hermes profile.
`--read-only` registers only `query_finances` and `reconcile_status`;
`add_transaction` and `ingest_document` are absent, including direct MCP calls.
The existing unrestricted MCP surface is unchanged for separately authorized
clients. Disable MCP sampling unless explicitly needed and reviewed.

For a server named `finn_nancy`, the native toolset is `mcp-finn_nancy`
(hyphen), and the registered tool names include
`mcp__finn_nancy__query_finances` (double underscores). Include that toolset
and `clarify` in the dedicated backend's explicit allowlist. An invalid name
can leave clarification working while the finance tools are absent.
Run `hermes -p PROFILE mcp test finn_nancy` and verify exactly the two read-only
tools before a real-model report check.

MCP subprocess environment values must be strings. The configuration CLI may
coerce `true` into a YAML boolean, which the MCP subprocess schema rejects.
Verify the effective environment types or omit unnecessary entries rather
than treating a successful model greeting as evidence that tools loaded.

`query_finances` accepts only the existing parameterized report names:
`coverage`, `budget_vs_actual`, `recurring_deltas`, `goals_progress`, and
`monthly_spend_by_category`. `reconcile_status` optionally takes a document ID.
Neither accepts arbitrary SQL or a client-supplied database path. The packaged
skill at `integrations/hermes/skills/finn-nancy/SKILL.md` explains the finance
contract; tools and repositories, not the skill's prose, enforce authority.

This first integration intentionally has no chat mutation/reprocessing tool.
Approving a generic Hermes prompt does not bypass financial review, period
policy, or the existing durable job interface. Use the relevant application
page for those actions. An idempotent, audited mutation API can be added as a
separate change.

## Persistence and recovery

Hermes stores transcript content in its own profile. Finn Nancy stores only
opaque session mappings and content-free submission receipts under
`DATA_DIR/hermes-chat/`, separate from the financial SQLite database. Preserve
both locations when backing up conversations; no financial migration is added.
Files are created privately and replaced atomically after fsync. A file lock
admits one connected frontend per conversation, including across app processes.

A submission UUID is recorded before the RPC crosses the wire. Repeating an
accepted UUID with the same message does not execute another turn; reusing it
with different text is rejected. An uncertain send is never automatically
retried. A reconnect restores authoritative history, and the browser retains
an uncertain draft for human reconciliation. At most 500 submission receipts
are retained per conversation; reaching the cap requires a new conversation
rather than silently losing deduplication evidence.

Closing the browser detaches without interrupting the Hermes turn. Stop is a
separate command. Native backend restarts may interrupt model execution;
recovery follows Hermes' stored-session semantics. An empty session that was
never durably recorded by Hermes may require New chat after a backend restart.
Changing the gateway URL/profile invalidates existing mappings rather than
resuming them against a different authority.

Resume omits the unbounded native transcript. The server reads one recent saved
history page from the same pinned gateway's session-messages REST endpoint,
using `X-Hermes-Session-Token` authentication. This uses the existing credential;
there is no browser-selected history URL, session, profile or offset. The page
is limited to 20 stored entries, 1 MiB of raw response bytes and a five-second
total deadline. Proxies, redirects and compressed responses are disabled.

The UI labels this as recent saved history, not a complete transcript. Older
messages remain in Hermes. A missing, oversized, incompatible or unauthorized
REST page produces an explicit history-unavailable notice while native live
recovery remains available. The saved history read precedes native resume so
inflight state and open questions are fresh; these two reads are not atomic.
A message persisted between them may appear only on the next reconnect.
History is shown only when both interfaces resolve the same session tip.

The native WebSocket retains its 2 MiB frame bound and bounded queues. A single
oversized live frame still fails closed; older gateways that cannot omit the
full resume transcript require upgrading. Overflow/disconnect requires
snapshot recovery, not silently dropped reply chunks. Provider error bodies
and gateway credentials are not forwarded as application errors. Rendered
content uses text nodes, never provider-supplied HTML.

Terminal turn errors settle the browser's running state; rejected commands do
not masquerade as completed turns. Reconnect snapshots identify currently open
questions, removing prompts that expired while disconnected without resending
answers. User and assistant text within accepted transport frames is preserved
in full, including final caveats. Tool activity is a bounded preview and is
explicitly marked when truncated.

## Verification and rollout

Focused deterministic checks:

```sh
scripts/test tests/test_chat_web.py tests/test_hermes_chat.py tests/test_hermes_transport.py tests/test_hermes_history.py tests/test_mcp.py
node --test tests/js/test_hermes_chat.cjs
```

The Python suite invokes the frontend contract tests, so they also run under the
canonical test gate. Use a disposable profile, generated database and private
loopback preview for real-model and browser acceptance. Verify a tool-backed
answer, clarification, reconnect, Stop, new conversation, disabled/error states,
mobile layout and absence of credentials in browser traffic.

Compare the raw tool result with the canonical report service and separately
inspect the model's explanation. Exact tool equality does not certify the
financial narrative. In particular, unresolved category evidence can produce
zero category rows despite real outflow, and missing currency metadata does
not establish a common base currency or permit a currency conversion.

Follow `docs/DELIVERY.md` for exact-content review, complete canonical gates and
any production rollout. Do not reinterpret a successful synthetic preview as
production activation. Rollback restores the prior code/configuration; do not
restore an old finance database or delete captured originals to roll back chat.
