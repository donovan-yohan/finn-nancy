# Namako finance machine interface

## Authority and design

The interface serves one trusted household. There are no application logins, roles or per-endpoint permission scopes. The shared `API_TOKEN` authenticates direct clients. A valid bearer key can call the same HTTP endpoints and form workflows as the frontend, including `/actions` decision and undo routes. The dedicated finance JSON adapter still creates category proposals; an authenticated client may drive the existing decision workflow itself rather than requiring a separate human merely because it is an agent.

The server distinguishes requests as `web_ui`, validated `api`, or `api_rejected`, not as individual people or named agents. Requests without a bearer key retain the trusted-tailnet UI path; this label is not proof that a real browser or person sent the request. A supplied invalid/duplicate Authorization header is rejected, never silently downgraded to UI authority. Caller-supplied identity headers are not trusted.

Each request receives a server-generated `X-Request-ID` and `X-FN-Caller` response header. Content-free JSON request events use the configured Uvicorn error-log handler and include caller, method, route template, status and elapsed milliseconds. They exclude keys, cookies, bodies, query strings, concrete financial IDs and client addresses. Request provenance is diagnostic metadata, not a new accounting authority or individual identity. Existing domain audit records remain intact.

Two designs were compared: (1) adapters enqueue existing recategorization actions, extending that handler with explicit split selection and expected-state checks; (2) a separate machine category writer plus independent approval/audit/undo storage. The adapter design is used: `app.api.finance.propose_category(...)` → existing `repo_actions` queue → existing human route → `RecategorizationHandler`. This avoids a second ledger writer and preserves period, signed-money and human knowledge-claim rules. No model confidence becomes an accepted knowledge claim; human approval creates the existing split-scoped human claim.

## Composition

Mount `from app.api.finance_routes import router as finance_router` with `app.include_router(finance_router)` in `app/web/app.py`. The router has `Depends(require_api_token)` in addition to the application's existing `ApiGuardMiddleware`; the outer `RequestAuthorityMiddleware` validates supplied keys on UI routes too. No separate JSON approval, undo or SQL route is added by this adapter. Existing frontend action endpoints are accessible with the same bearer key and their ordinary form fields/response formats.

The human label recorder in `app/web/routes/actions.py::_record_recategorization_label` stores transaction-wide evaluation labels. Composition skips multi-split transactions and clears stale transaction-wide labels: one selected split does not label all sibling spending. Split-aware knowledge decisions remain in the shared recategorization handler.

Apply additive migration `040_category_operations.sql` using the normal migration runner. It stores immutable operation key/request/proposal receipts and makes proposal audit rows append-only. Receipts retain keys after approval, edit, rejection and undo. Same key and normalized request returns the same proposal ID/current status; different request returns a conflict even after decision. Proposal creation uses `engine.write_tx` / `BEGIN IMMEDIATE`, with no ledger/category/knowledge mutation.

## JSON API

All JSON API routes require the configured token in the `Authorization` header with the `Bearer` scheme; an unset token disables API access. Do not place tokens in query strings.

The same header also works on frontend endpoints outside `/api`. A validated direct caller does not need browser Origin/CSRF headers for banking commands; unkeyed browser calls retain the original exact-origin/session/CSRF checks. Link completion is still bound to its initiating API key or browser session and provider-verified server Link token; rotating the API key invalidates pending sessions initiated with the old key. A key never bypasses member consent, strict-local/read-only settings, accounting validation or period guards. Browser code never receives the shared API key. Authenticated requests retain each endpoint's existing JSON, HTML, file or redirect contract; endpoint access is not a claim of complete JSON-adapter/remote-CLI parity.

- `GET /api/finance/transactions`: `q` (literal description/counterparty substring, max 200), positive `account_id`, `category_id`, ISO `date_from`/`date_to` (inclusive), `flow_kind`, `review=needs_review|resolved`, `cursor`, `limit=1..500` (default 50). Returns `rows` and nullable `next_cursor`; rows include signed integer cents, explicit currency and flow resolution. Ordering is descending canonical transaction ID; cursors are bound to the same filters, not a frozen ledger snapshot.
- `GET /api/finance/transactions/{id}`: canonical transaction, exact splits, evidence rows/IDs (documents, statement lines/anchors/extracted field claims/reviews/import rows/relationships/merchant claims), flow/category resolution, and `state_digest`.
- `GET /api/finance/categories`: existing categories only.
- `POST /api/finance/category-proposals`: JSON fields `operation_key`, `transaction_id`, `split_id`, `to_category_id`, `expected_state_digest`, `rationale` (1..2000 chars); optional `confidence` (0..1) and `evidence_refs` (max 20 linked `document:ID`, `statement_line:ID`, `transaction:ID`). Extra keys, arbitrary SQL, caller-supplied actors and authority flags are rejected.
- `GET /api/finance/proposals/{id}`: current proposal, immutable receipt/provenance, and chronological decision audit snapshots.
- Existing `GET /api/period-statements/{month}` and `/api/reports/{report}` remain the shared report surfaces.
- `GET /api/finance/schema` and `POST /api/finance/query`: bounded read-only SQL over four explicit financial projections, with named parameters and positional rows. See [Read-only financial SQL](FINANCIAL_SQL.md) for the accounting interpretation and resource boundary; this is not arbitrary ledger write access.

Missing objects return 404; key/stale submission conflicts return 409; input/policy failures return 422. Human stale apply/revert failures retain the existing HTML route's 400 response.

## Remote CLI

`fn api METHOD /endpoint` uses `FN_API_URL` and the privately supplied `FN_API_KEY` to call the deployed application over HTTP. It does not open a local database or load application settings. JSON and existing frontend form workflows share the same server authority and domain guards; category decisions and undo are available through their existing routes. See [Remote API CLI](REMOTE_API.md) for JSON/stdin/form input, structured status/correlation output, bounds and uncertain-outcome handling. No machine-only approval requirement or per-key permission system is introduced.

## Local CLI JSON

`fn finance --help` lists the supported commands. Examples use operator-selected generated or local paths, never an implicit production target:

```sh
fn finance transactions --db /path/to/ledger.sqlite --q 'Market' --date-from 2026-06-01 --date-to 2026-06-30 --limit 50
fn finance transaction 21 --db /path/to/ledger.sqlite
fn finance categories --db /path/to/ledger.sqlite
fn finance propose-category --db /path/to/ledger.sqlite --operation-key namako:meal:21:1 --transaction-id 21 --split-id 21 --to-category-id 5 --expected-state-digest <digest-from-inspection> --rationale 'Meal portion only'
fn finance proposal 1 --db /path/to/ledger.sqlite
fn finance period-statement 2026-06 --db /path/to/ledger.sqlite
```

Success writes JSON only. Service validation failures write `{"error":"..."}` and exit 1. The `fn finance` commands are local database capabilities, not bearer HTTP transport; use `fn api` for a remote instance.

## Narrow MCP registration

`fn mcp-serve --read-only --db /path/to/ledger.sqlite` exposes only safe reads: legacy `query_finances`, `reconcile_status`, plus `search_transactions`, `inspect_transaction`, `list_categories`, `inspect_proposal`, `period_statement`.

`fn mcp-serve --proposals --db /path/to/ledger.sqlite` exposes those reads and only `propose_category`. It does **not** register legacy `add_transaction`/`ingest_document`, approval or undo. `--proposals` and `--read-only` are incompatible. The pre-existing unflagged legacy MCP surface is preserved for compatibility; it is not an acceptable workaround for a proposal-only client. Global `READ_ONLY=true` also rejects proposal creation. This change configures no profiles.

## Guard and undo contract

Inspection hashes transaction-critical fields, full ordered splits and canonical source/evidence state. Creation checks the caller's digest inside its write transaction. Human apply rechecks it before any category/claim effect. Closed periods remain governed by the shared period policy. Unknown or relationship-incomplete flow, pending/inactive source rows, unreviewed source extraction/statement or non-home source currency cannot be guessed from amount sign into approval. Target categories must already exist and be expense-purpose; split sum must equal signed transaction amount.

Bank evidence participates in the same snapshot: accepted associations and revocations, accepted/current observation and binding identities, current source values, attached receipt evidence and append-only separate-purchase receipt decisions. Inspection exposes the decision's actor/reason, original and reviewed JSON, candidate identities and linked documents/extractions. Adding a decision changes the digest without changing ledger money or extraction bytes. A provider change leaves the ledger amount intact but invalidates a pending category proposal. A fresh proposal remains blocked until changed bank evidence is reviewed.

An explicit split edits **only that split's category**. Amounts, transaction fields, siblings and evidence are unchanged. Undo requires the exact post-apply snapshot (including source and split shape/categories/amounts and claim events), restores only the selected category, and appends the existing knowledge undo event. Any intervening change refuses undo. Legacy payloads without `split_id` retain exactly-one-split validation and existing legacy revert payload behavior.

## Focused verification

`tests/test_namako_finance.py` uses only generated databases: inspection → retry/conflict → authenticated JSON proposal → human HTML approval → shared period report/detail → human undo; split preservation, stale transaction/source/sibling/shape/amount checks, source review/currency/pending failures, closed stages, immutable receipts/audit, read-only/concurrent retry checks, and rollback on knowledge-claim failure. Existing `test_proposed_actions.py`, `test_api.py` and `test_mcp.py` cover compatibility. No provider authorization or private-record acceptance is claimed.
