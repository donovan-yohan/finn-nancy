# Local Plaid Sandbox candidate

This is an isolated, unbooked synthetic-data prototype, not production feed support.
No Production/development environment switch exists. Investments/holdings, ledger
reconciliation, categorization, historical migration, subscriptions, and goals are
not implemented by this packet. Sandbox cannot certify a Canadian institution's
production coverage or refresh reliability.

## Run and private setup

From a prepared isolated checkout (`uv sync --frozen`):

```sh
scripts/plaid-sandbox --check
scripts/plaid-sandbox --port 8772
# Preferred when the operator has provisioned a separate private handoff:
scripts/plaid-sandbox --port 8772 --credentials-file /path/to/private/plaid-sandbox.json
```

Open `http://127.0.0.1:8772/connections` on the same machine. The launcher pins
loopback and a dedicated `data/plaid-sandbox/sandbox.sqlite` in this checkout.
It does not load `.env`, the normal application settings, financial databases,
workers, chat gateways, Telegram, inbox watchers, or local/cloud models. Inherited
transport/configuration environment variables are discarded. No LAN bind, tunnel,
production service change, or app-level authentication is supplied by this origin.

### Private Tailnet-wide ingress

When the browser is not on the backend host, its `127.0.0.1` is a different
endpoint. Use the separate optional `app.plaid_sandbox.tailnet` gateway rather
than exposing the unauthenticated origin or weakening its Host/Origin checks:

```sh
.venv/bin/python -m app.plaid_sandbox.tailnet
tailscale serve --bg --yes --https=8782 http://127.0.0.1:8773
```

The gateway remains loopback-only on 8773 and only forwards to the unchanged
candidate on 8772. The HTTPS authority is derived from the current node's exact
MagicDNS name, without enumerating peers or mapping users. **Anyone allowed by
Tailscale access rules to reach this Serve route can read AND mutate the shared
sandbox.** There is no per-user access control, additional device/user allowlist,
or application sign-in. Tagged and untagged devices are treated alike; the
`Tailscale-User-Login` header is ignored and stripped before forwarding.

The gateway trusts Tailscale Serve's access-control layer and requires a real
loopback connection, the exact allowed Host, HTTPS forwarding, and exactly one
Serve-overwritten `X-Forwarded-For` address within the RFC 6598 shared-address
IPv4 block (`100.64/10`) or
`fd7a:115c:a1e0::/48`. Missing, duplicate, chained, malformed and non-Tailnet
sources fail closed. Do not place another proxy in this trust path or expose the
gateway directly. No Funnel or public internet endpoint is enabled. The strict
local personal-host boundary remains: this is not protection against other
OS-local processes that already have access to the loopback candidate.

The outer exact HTTPS Origin is validated before translating to the original
loopback Host/Origin; the candidate still validates its own CSRF token and
server-issued session cookie. Cross-site links from chat can open a top-level
document only with GET/HEAD, `Sec-Fetch-Mode: navigate` and
`Sec-Fetch-Dest: document`; any supplied Origin must still exactly match the
current HTTPS origin. Only that verified case has `Sec-Fetch-Site` stripped
upstream. Cross-site API fetches, frames and mutations remain rejected.
Arbitrary forwarding/authentication headers are
not passed upstream, and browser cookies receive the Secure flag. Neither the
gateway nor its startup reads Sandbox credentials or databases. Existing Serve
routes must remain untouched; remove only this route with
`tailscale serve --https=8782 off` and stop only the owned gateway process.

For an operator-provisioned handoff, launch with the explicit private file path.
The file schema is `{environment: "sandbox", client_id: string, secret: string}`;
its parent directory must be 0700 and the regular, single-link file must be 0600,
owned by the current user. Symlinks and oversized/unsafe files fail closed. Status
checks inspect metadata only, not contents. Accept `plaid-sandbox.v1` in the local
UI; only subsequent server-runtime operations load the file. Credentials are not
copied into SQLite. Do not read or display the handoff using agent tools.

Without a handoff path, **Private sandbox setup** provides an alternative masked
local form. Enter the Sandbox client ID and Sandbox secret, then explicitly accept
`plaid-sandbox.v1`. This form is absent and its write route refuses when the
explicit handoff is selected. Never paste keys in
chat, shell arguments, logs, URLs, or source files. Setup fields are masked and
cleared before submission; values cannot be displayed again. These opaque keys
cannot be identified as Sandbox keys by their spelling: provision only the
Sandbox secret from Plaid's dashboard. The only data API host is hardcoded to
`https://sandbox.plaid.com`, so a mistaken key cannot dispatch to Production.

The candidate directory is mode 0700 and database/lock files are 0600. Tokens and
configuration remain server-side; Link/public tokens exist transiently in browser
memory for the official flow but never in browser storage. All candidate responses
are `no-store`; the candidate neither registers nor serves a service worker. Use a
fresh loopback origin/profile rather than a port previously running another PWA.
The normal app only has a disabled Connections notice; it mounts no Sandbox API.

## Product views

The candidate has four real destinations, not redirects to a setup page:

- **Home** (`/`): import counts, local sync status and recent current observations.
- **Activity** (`/activity`): searchable, paginated imported rows, with connection-owner,
  account, date, lifecycle and account-selection filters. Source identifiers and
  accounting limitations are available on demand rather than repeated on every row.
- **Accounts** (`/accounts`): discovered accounts, independent economic-ownership
  mapping, inclusion settings and direct links to each account's activity.
- **Connections** (`/connections`): official Link, sync, connection health and
  contextual recovery controls. Household members and private setup are secondary.

### Adding and managing accounts

Connections keeps **Add bank** easy to find. First use reveals the missing
prerequisite inline: enable Sandbox access, add a household member, then choose
the connection owner and confirm fresh consent. Finishing setup or adding a
member never opens Plaid automatically. With manual credentials, a disabled
Sandbox still requires the existing setup form; there is no hidden re-enable API.

Connected rows expose sync and account/activity links. Login-required repair
asks for separate consent; an ordinary sync failure does not imply that a new
connection is needed. Uncertain exchanges/revocations keep their existing
recovery controls and cannot be replayed into a clean-looking success.

Accounts keeps connection identity, account owner and Included/Excluded visible.
Edit one account, choose its household owner and inclusion, then Save changes or
Cancel. These settings are local mappings, not a bank disconnect, and remain
available when network access is disabled. Draft edits belong to the complete
connection/account identity; saving one account must not discard another's edits.
An untouched open editor follows freshly loaded saved values. Only fields actually
changed by the user remain in its draft, so a refresh does not manufacture a stale
overwrite. After saving, cancelling or repairing, keyboard focus returns to a
visible control on the affected account or connection.
Disabling a button during a pending operation is presentation of the existing
single-operation boundary, not permission to queue another provider action.

Activity shows current posted and pending observations from **all** imported accounts
by default, even when an account is not selected. Inclusion is a filter, not permission
to conceal evidence. Removed and replaced observations remain inspectable history.
An account filter always carries its connection identity; provider account IDs are
not globally unique. Amounts retain their signed cents and individual currency;
unknown accounting meaning stays unknown. These pages do not invent income, spending,
balances, approved categories, statement completeness or booked-ledger totals.

### Merchant display and provider suggestions

Activity and Home prefer Plaid's `merchant_name` when it is supplied, then fall
back to the retained `name` description. Transfers and other transactions without
a merchant keep that fallback; no counterparty is guessed into a merchant.
Expanded rows distinguish the provider description from the original bank
description. A missing original bank description is shown as not supplied, not
reconstructed from another field.

Merchant entity IDs and counterparties are available as plain-text source
details. Plaid personal-finance categories appear only as **Suggested by Plaid**
in the interpretation accent. They do not approve a category, resolve a merchant
in the local knowledge system, establish spender/flow meaning, or book a row.
No remote logos or website requests are introduced.

The API adds a bounded `enrichment` projection while retaining `name`. It exposes
only merchant name/entity ID, original description, selected counterparty fields
and a category suggestion with provider confidence/taxonomy version when supplied.
Malformed or oversized optional enrichment is omitted from the display projection;
complete received provider payloads remain in immutable observations. Search matches
the normalized merchant and original description as well as the retained provider
and account descriptions, before pagination and exact result counting.
Search retains SQLite's existing `LIKE` behavior: ASCII case-insensitive matching,
but accented/non-ASCII characters may require the same case as the source.

The isolated candidate schema adds a nullable enrichment projection to staged rows.
It is replaced atomically with each added/modified provider row and cursor, including
deduplicated observation replays, and retained with removed/superseded history.
Source-field updates from older code invalidate the display projection rather than
leaving a stale merchant attached to a changed transaction. Such a row falls back
to its retained provider description until a current-code update supplies enrichment.
Legacy recovery uses only observations whose persisted source/financial fields
match the staged row and whose normalized enrichment is unambiguous. Ambiguous
history stays absent. Reads do not backfill, fetch credentials or contact Plaid.

Future syncs request `options.include_original_description: true`. Existing saved
descriptions cannot be invented, and an ordinary incremental sync does not promise
to enrich unchanged historical rows. This update does not reset cursors, recreate
Items or call the separate paid Enrich product.

The read-only `/api/sandbox/overview` response contains safe connection/account
metadata, import counts and at most eight recent rows; `/api/sandbox/activity` returns
bounded pages with exact filtered counts. Neither triggers a provider refresh or
loads provider credentials. Both require the same private browser session and ingress
boundary as the existing state API. The original state API remains compatible.

Unsupported normal-app workflows are not advertised in the candidate's navigation.
The normal application shell and its functionality remain unchanged.

## Exercise the slice

1. Add two household members using display names. These are not Finn/Nancy roles.
2. Select one **connection owner**, explicitly consent, and open official Plaid
   Link Sandbox. The official script is loaded directly from Plaid's CDN only on
   this person-driven action, after network enablement. Use only Plaid's synthetic
   test login, never a real bank login.
3. Repeat separately for the other member. Account/institution metadata comes from
   authenticated backend responses, not browser callback labels.
4. In **Accounts**, set each discovered account's ownership to a member,
   joint/shared, or unassigned. Persist inclusion separately; the Activity selection
   filter can inspect included or excluded accounts. This selection does not control
   Plaid's Item-wide fetch or remove server-side evidence.
   Identical display names/suffixes and even provider account IDs in different
   Items remain separate; this candidate does not merge jointly linked accounts.
5. Sync in **Connections**, open **Activity**, inspect source details, search/filter
   by account or connection owner, and sync again. Amounts are signed integer cents
   (Plaid positive outflow becomes negative),
   but flow meaning and spender remain unknown. Nothing reaches a ledger, statement
   completeness policy, reporting totals, or period close.
6. Repair requires fresh owner consent and uses Link update mode with the existing
   Item/access token, not a new Item exchange. Last bank update and last local sync
   remain distinct; an unavailable bank timestamp is unknown, not the local time.
7. Disconnect first records an auditable revocation-in-progress state. A lost
   acknowledgement or failed local commit becomes revocation-uncertain, not connected.
   **Reconcile uncertain revocation** checks existing access before another remove.
   Invalid access does not prove remote deletion: it clears unusable active access
   but leaves an explicit revocation-unconfirmed status requiring Sandbox dashboard
   verification. Local observations, mappings and audit survive every case.
   Disable network access separately to retain the candidate without further egress.

Before exchange, the server verifies the public token against provider-authenticated
completion data for the exact stored Link token. Swapped/unrelated tokens and missing
completion evidence fail closed without exchange or attribution. Plaid may require
account enablement for `/link/token/get` event data on standard Web Link sessions;
this is a real live-acceptance prerequisite, not something generated fixtures prove.

A one-shot exchange is durably marked uncertain before network dispatch. An
uncertain response is not replayed. A successful retry returns the recorded local
connection. If an uncertain attempt remains, inspect it in the Plaid Sandbox
dashboard before creating another Item. **Link recovery** requires a separate,
explicit operator acknowledgement of that check before allowing a fresh new-item
Link for the affected member. The check is recorded with its actor and timestamp
in append-only audit history. It does not prove remote deletion or exchange failure;
an orphaned Sandbox Item may still exist. The original attempt remains uncertain,
its token digest is retained, and exchange is never replayed. Starting another
connection still requires fresh member consent. There is no automated recovery
claim for an access token lost during an uncertain exchange. The Trial allowance
is **10 cumulative Production Items**; deletion does not restore it, and Sandbox
Items do not consume it.

Sync collects the entire bounded update window before committing observations,
staged projection and cursor together. Pagination mutation restarts from the
original cursor with bounded retries. Pending replacement, modification and
removal retain observations; no ledger write or financial interpretation occurs.
This candidate's private experimental schema is separate from the canonical
application migration sequence and is not a production migration.

## Verification

```sh
scripts/test tests/test_plaid_sandbox*.py
node tests/browser/plaid_sandbox.cjs
node tests/browser/plaid_sandbox.cjs --generated
node tests/browser/plaid_sandbox.cjs --generated --handoff
scripts/public-release-audit
scripts/agentic-harness
scripts/lint
scripts/test
scripts/build
scripts/smoke
scripts/smoke-capture-browser
```

Browser checks use the existing repository's Chromium/CDP approach. Set
`CHROMIUM_BIN` where required and an isolated `TMPDIR`; `SANDBOX_QA_ARTIFACTS`
selects the screenshot/report directory. `--generated` explicitly intercepts the
official script URL with a generated callback double and uses a generated httpx
transport. It is **not** live official Link or live Sandbox API evidence. No real
credentials are used in tests. Real proof requires privately provisioned Sandbox
keys, two official Link flows, discovered accounts/transactions, repeat sync and
repair. Do not mark the goal complete until those checks have actually run.

## Stop and restart

Stop the foreground process with Ctrl-C, or send SIGTERM to the verified listener
PID owned by this candidate; confirm port release before restart. Do not kill an
unrelated listener. Restart using the same launcher and port; private configuration
and cursor state persist in the dedicated candidate root. Keeping or discarding
this private root is independent from reverting the uncommitted code. Do not
restore, delete, or migrate a production finance database to roll back a prototype.
