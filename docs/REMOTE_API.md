# Remote API CLI

`fn api` is a small HTTP client for an existing Finn Nancy deployment. It needs no SQLite file, writable database mount, SSH session or assistant runtime on the server. It does not load application settings or a repository `.env`.

## Connection

Set `FN_API_URL` to the application origin and privately provide `FN_API_KEY` with the server's shared `API_TOKEN`. Do not pass the key as a command argument, place it in a URL, or paste it into chat. Prefer HTTPS over the private tailnet. HTTP is supported for explicitly trusted local/private deployments; it is not encrypted.

There are no per-key permission scopes or app logins in this household MVP. A valid key accesses the same endpoints as the trusted frontend. Responses expose server-generated request IDs and `api` caller provenance for troubleshooting, not individual identity.

## Queries and workflows

```sh
fn api GET '/api/finance/transactions?date_from=2026-06-01&date_to=2026-06-30&limit=50'
fn api GET /api/finance/categories
fn api GET /connections/data/feed
fn api GET /connections/data/plan/7
fn api POST /connections/data/decision/7 --data reviewed-decision.json
fn api POST /actions/12/approve --form
fn api POST /actions/12/revert --form
```

`--url` optionally overrides `FN_API_URL`. The key remains environment-only.

For first-time bank history against an existing ledger, use [Initial history reconciliation](INITIAL_RECONCILIATION.md). It covers mapping, staged overlap decisions, ambiguous/legacy-only history and category apply/report/undo without direct SQL writes.

`--data FILE` reads one JSON object, bounded to 1,000,000 input bytes. `--data -` reads it from stdin. Normal mode sends JSON without changing explicit booleans or nulls. `--form` sends scalar fields using the frontend's form encoding; null fields are omitted and nested objects/arrays are refused. Use each route's actual field names and current fingerprint/operation key. This generic client does not invent a second financial writer or bypass route validation.

## Output and retries

Output is one JSON object with:

- `status_code`: actual HTTP status;
- `request_id`: server correlation ID, if provided;
- `caller`: server caller provenance, if provided;
- `data`: decoded JSON, text for HTML/text responses, or null for an empty body.

HTTP 400+ and local/transport failures exit 1. HTTP redirects are returned without following them, including normal 303 frontend success redirects. Inspect the result or issue a deliberate subsequent read; the client does not replay a mutation. A timeout or connection failure reports an uncertain outcome: inspect current server state and reuse a documented operation key where supported before deciding to retry.

The client disables ambient proxies and redirects, uses no automatic connection retries, bounds response bodies to 4,000,000 bytes and uses normal TLS certificate verification. It rejects credential-bearing base URLs and endpoints that attempt to select another authority. Do not put secrets in query strings or application payloads merely because transport authentication is private.

## Limits

This is a generic HTTP/form adapter, not a promise that all endpoints return JSON or have dedicated friendly commands. The older `fn finance --db ...` commands remain local-database tools; `fn api` is the remote path. Use [Read-only financial SQL](FINANCIAL_SQL.md) for bounded queries via `/api/finance/query`, not direct storage access or SQL writes. Uploaded binary files, downloaded binary exports and persistent browser/chat cookies are not implemented by this command. Provider Link completion is still server-verified and account holders must perform institution login/authorization themselves.
