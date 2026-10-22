# Read-only financial SQL

Use this interface for ad hoc financial observations without mounting the server's SQLite file or bypassing application writers. The same shared API key authenticates it; there are no new roles, permission scopes or logins.

```sh
fn api GET /api/finance/schema
fn api POST /api/finance/query --data financial-query.json
```

`GET /api/finance/schema` lists the four explicitly exposed relations and their columns:

- `analysis_transactions`: canonical IDs, account/date/description/counterparty, signed cents, flow kind, reconciliation state, source and document ID;
- `analysis_splits`: split/transaction/category IDs, signed cents and memo;
- `analysis_accounts`: account ID/name/kind/currency/active state;
- `analysis_categories`: category ID/name/kind.

These are private financial data, accessible to the trusted key holder, not a public analytics feed. Settings, connection/token references, raw provider observations, raw documents, filesystem paths and SQLite schema internals are not query relations. Use existing evidence/detail and banking workflows where richer inspection is necessary.

## Request and response

The JSON request has `sql`, optional named `parameters` (default empty object), and `limit` (default 100, range 1–500). Extra fields are rejected. Parameters are scalar strings, signed 64-bit integers, finite numbers or null, not SQL fragments. Use named placeholders rather than interpolating descriptions or dates into SQL.

Example request file:

```json
{
  "sql": "SELECT a.name, a.currency, t.flow_kind, SUM(t.amount_cents) AS signed_cents FROM analysis_transactions t JOIN analysis_accounts a ON a.id=t.account_id WHERE t.posted_on>=:start AND t.posted_on<:end GROUP BY a.id,a.name,a.currency,t.flow_kind ORDER BY a.id,t.flow_kind",
  "parameters": {"start": "2026-06-01", "end": "2026-07-01"},
  "limit": 100
}
```

The inner response contains `columns`, positional `rows`, `row_count` and `truncated`. Positional rows preserve duplicate column labels without silently overwriting values. The remote CLI wraps this in its ordinary `status_code`, `request_id`, `caller` and `data` envelope. A true `truncated` means more rows were available; it is not a complete result and this interface has no cursor. Narrow the query or aggregate rather than assuming the returned page is everything.

## Accounting interpretation

Signed account activity is not the same as household income or spending. Keep explicit `flow_kind`, currencies and account identity in analysis. Transfers and card payments need relationship evidence; refunds/reimbursements/reversals are not ordinary income; unknown semantics remain review work. An assigned category ID is not proof that categorization is accepted.

Use `/api/finance/transactions/{id}` for flow/category resolution and row-linked evidence, and canonical report/period-statement endpoints for accounting-qualified totals. SQL aggregates over these four base projections are observations about stored activity; they do not independently establish semantic completeness, accepted category evidence, statement completeness or a clean close. Reports read balanced splits; transaction/split joins can multiply totals when a transaction has multiple splits, so choose the correct grain.

## Read and resource boundary

Each call opens the configured database through SQLite URI `mode=ro`, creates only fixed temporary projections, enables `query_only`, begins a read transaction and installs a default-deny SQLite authorizer. Public projections wrap unpredictable per-connection internal views; caller-authored CTE names cannot authorize direct raw-table reads. One statement may use SELECT, CTEs, joins, grouping and a limited set of scalar/aggregate/date functions. DML, schema changes, PRAGMA, transaction control, ATTACH/DETACH, filesystem/extension functions and unexposed relations are denied. Caller SQL is never passed to an application transaction writer.

Limits are 16,000 UTF-8 SQL bytes, 50 named parameters, 100 characters per parameter name, 10,000 UTF-8 bytes per string parameter, 500 returned rows, 100 columns and 500,000 encoded data bytes, including column labels and the complete inner response envelope even for empty or truncated results. The output budget conservatively counts ASCII-escaped JSON. SQLite also bounds expression depth, compound SELECTs and individual values. Work is interrupted at one million virtual-machine steps or after a one-second execution budget checked every thousand steps. At most four calls execute concurrently in each server process; saturation is refused without queueing. This is a bounded operator research tool, not a general hostile SQL sandbox or a whole-ledger export.

Queries share a per-call read transaction. Different calls are not a frozen snapshot and may observe intervening bookkeeping. No schema migrations or database copy is made by analysis; the source database must already exist. The API remains usable with global `READ_ONLY=true` because it cannot mutate the ledger.

Invalid reads, exhausted work/output/capacity and non-JSON results return sanitized HTTP 422 details. Request-validation failures on the query route return `query input is invalid` without echoing malformed field values; other routes retain their existing validation responses. SQL text, bound parameters, database paths and SQLite diagnostics are not logged by the analysis service. The existing content-free request telemetry records route/status/correlation only. Do not send raw financial inputs to unrelated external models or logs merely because this query interface is local/private.

## Verification

`tests/test_finance_query.py` exercises composed shared-key authentication, exact integer cents, named parameters, CTE/grouping, row truncation, readonly deployment mode, refused write/schema/private/filesystem surfaces, resource limits, nonfinite results and an installed `fn api` query over owned loopback HTTP without database configuration in the client. All fixtures are generated; this does not authorize production-data access or deployment.
