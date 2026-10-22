# Canonical banking integration

Composition contract (owned by the canonical application assembler):

```python
from app.banking.service import BankingService
from app.banking.web import build_router

banking = BankingService(
    db_path=settings.db_path,
    private_dir=settings.plaid_private_dir,
    credentials_file=settings.plaid_credentials_file,
    origin=settings.plaid_origin,
    completion_uri=settings.plaid_completion_uri,
    enabled=lambda: get_settings().plaid_enabled,
    strict_local=lambda: get_settings().strict_local_mode,
    read_only=lambda: get_settings().read_only,
    home_currency=settings.home_currency,
)
app.include_router(build_router(banking))
```

All paths and policy callbacks are explicit; no environment or dotenv reads in this subsystem. Disabled installations may pass `None` for private paths. Remove the previous `/connections` notice route when mounting. New browser endpoints use `/connections/data/*`, not the bearer-authenticated `/api` namespace. New templates and assets are banking-prefixed and do not import Sandbox code. Migration 041 is canonical and independent of the Sandbox database.

The shared trusted-household operator selects a member for consent attribution. This is not individual authentication. Hosted Link performs provider authorization; the server polls its own saved Link token, never a browser-supplied public token. Secrets stay in private files; only opaque references enter SQLite. Production host is fixed, with no proxy or redirect following.

Validated shared API-key clients may use the same `/connections/data/*` endpoints without browser CSRF/Origin headers. Unkeyed browser calls still require the exact configured origin and a server-minted session/CSRF token. API Link sessions use an opaque key-derived binding, not a fabricated browser cookie; their completion cannot be taken over by an unkeyed browser or a rotated key. Provider verification, consent revision, one-shot uncertainty and egress gates are unchanged. Request caller metadata distinguishes API from UI for diagnostics, not individual consent identity.

Generated acceptance and source-freeze evidence will be recorded below after execution. No real provider authorization or rollout is part of local acceptance.

## Frozen producer schema for category fingerprints

Migration `041_bank_feed.sql` is the sole schema authority. Accepted evidence is
`v_bank_associations` (append-only `bank_associations` minus `bank_revocations`).
Each accepted row has `id`, `subject_id`, `observation_id`, `binding_id`,
`transaction_id`, `operation_key`, `actor`, `reason`, `created_at`.

For each transaction include all historical `bank_associations` rows and matching
`bank_revocations` (including revocation identity/actor/reason), accepted
`bank_observations` rows, `bank_subjects.observation_id` and that latest
observation row. Include accepted `bank_bindings` plus latest
`v_bank_bindings` by `bank_account_id`. Immutable observation fields include
scoped connection/provider transaction/account IDs, `kind`, signed
`amount_cents`, `currency`, `posted_on`, `pending`, `pending_predecessor`,
`merchant`, `description`, `suggestion_json`, window and creation identity.
`bank_receipt_evidence` links extraction/document/transaction with operation,
actor, reason and creation identity; include those plus the linked extraction
and source-document evidence through the existing category-state machinery.
Do not include `bank_connections.token_ref`, `bank_link_sessions.token_ref`,
credentials, access tokens or Link tokens in machine responses/digests.

New booking permits only explicitly reviewed purchase, fee, income or interest;
unknown, relationship-dependent and system-balance flows remain review. The
canonical ledger stays the authority for statement clearing and category writes.

## Executed local acceptance

- Focused canonical command: `scripts/test tests/test_banking_client.py tests/test_banking_feed.py tests/test_banking_lifecycle.py tests/test_banking_web.py tests/test_banking_composition.py tests/test_banking_category_evidence.py tests/test_close.py tests/test_ingest.py tests/test_reconcile.py` — 67 passed, zero skips; five upstream PyMuPDF/SWIG deprecation warnings.
- `node tests/browser/banking.cjs` — seven generated acceptance groups passed against `app.web.app.create_app`, injected only through `app.state.banking_service.transport = httpx.MockTransport(...)`. Two members give consent, open a generated intercepted external Hosted Link UI, return to the same HttpOnly browser session, finish server-verifiable Link, explicitly map one shared account primary/secondary, sync, record a purchase and match the secondary observation. Readback contains one uncleared canonical purchase and two associations. Dashboard and bearer-authenticated finance API both include the booking.
- Chromium geometry/captures cover Connections, feed and review at 320/390/768/1440 in light/dark, no horizontal overflow and no browser runtime exceptions. External provider navigation is intercepted, all actual provider HTTP requests use MockTransport, and server sockets deny non-loopback. This is not real institution, Safari/VoiceOver, OAuth or physical-phone qualification.
- `scripts/public-release-audit` and `git diff --check` passed. Parent owns full gates and final aggregate freeze.

Receipt dedupe required the narrow `app/ingest/pipeline.py` status branch in addition to the approved promotion/review seam: its previous unconditional `processed` write would erase a legitimate `needs_review` result. A late receipt stays pending, attaches through the reviewed feed action without modifying extracted JSON, and never approves its model category. Clean and exception closes are both refused by a SQL event trigger while feed blockers remain; sync is allowed to retain closed-period observations but association/exclusion/receipt evidence cannot mutate closed-period accounting.

## Operational qualification remaining

Production Transactions entitlement, real credentials, supported institutions and explicit actual member consent are external prerequisites. Hosted Link is an explicit external Plaid flow using `hosted_link` and server `/link/token/get`; no webhook ingress is required. Completion redirects only restore the browser UI and never prove success. Credentials JSON requires `client_id` and `secret`, mode 0600; private token directory requires mode 0700, outside Git. Configuration and real authorization are not performed by acceptance fixtures.

Exchange/revocation uncertainty is deliberately non-retryable. Current UI exposes the uncertain state, not an automated repair that might exchange twice or falsely promise revocation. Operator verification with Plaid/bank support is required for uncertain one-shot outcomes. Ordinary account discovery is independently retryable after a committed exchange. Link session/browser recovery is bounded to the current one-hour process-local browser session and a 30-minute Link window; a process restart fails closed rather than accepting an arbitrary browser token.

## Lifecycle/security review A regression boundary

Every connection-scoped dispatch checks the captured lifecycle, generation,
cursor and token reference again immediately before HTTP. Disconnect claims its
own `removing` generation; entering repair and completing repair also advance
generation. No database write lock spans a provider call. Account discovery
revalidates the same snapshot inside its publishing transaction.

Link creation, one-shot claim, exchange dispatch and completion retain the exact
consent row, not just a later granted consent. Withdrawal followed by regrant
requires a new Link session. A changed revision after an exchange claim remains
uncertain and cannot replay the exchange. Removed repair targets cannot complete;
completed-session readback reports the connection's current state.

Private token and credential paths reject any ancestor `.git` directory or file
(including linked worktrees and ignored paths), and bare repository markers,
before secret I/O. The checks inspect markers only, never run Git or read its
metadata. They run again before dispatch and on private-store operations. Existing
symlink, ownership and private-mode restrictions remain in force. Safe sibling
paths outside the repository remain supported; credentials are never relocated.

First inclusion or re-inclusion of an account guards every retained observation
month through the shared period policy in the binding transaction. Reopen a
closed affected month before activating historical evidence. Raw observation
sync remains allowed while a month is closed. Direct-SQL binding backstops belong
to the accounting migration, not this service repair.

Focused canonical acceptance for this batch: `scripts/test tests/test_banking_client.py tests/test_banking_lifecycle.py tests/test_banking_composition.py tests/test_banking_lifecycle_regressions.py tests/test_banking_feed.py` — 66 passed, zero skips, five upstream SWIG deprecation warnings. Generated SQLite, generated private files and `httpx.MockTransport` only; no provider qualification, browser rerun, full gates or deployment is claimed here.

Full relationship entry for transfer/refund/reimbursement is intentionally left to existing guarded flow review; this feed never guesses such meaning. Statement expectation configuration remains a separate close prerequisite. Generated acceptance is local working evidence, not permission to enable production egress or roll out.
