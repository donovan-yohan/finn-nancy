# Context map

This map points contributors to the executable contracts before they change behavior.

## Entrypoints and runtime

- `app/cli.py` — the `fn` command-line interface.
- `app/web/app.py` and `app/web/routes/` — FastAPI application assembly and HTTP/UI routes.
- `app/workers/` — durable background-job execution.
- `app/config.py` — environment-backed configuration and local/egress controls.
- `docs/UI_DESIGN.md` — navigation, Finn/Nancy colors, and durable upload presentation.

## Persistence and accounting

- `app/db/engine.py` and `app/db/migrate.py` — SQLite connection, transaction, and migration boundaries.
- `app/db/repo_*.py` — persistence operations; do not bypass these with route-local SQL writers.
- `migrations/` — ordered, additive schema changes.
- `app/accounting/flows.py` — flow semantics and transaction relationships.
- `docs/ACCOUNTING_CONTRACT.md` — signed-cents, evidence, reconciliation, and reporting invariants.

## Ingestion and reconciliation

- `app/ingest/` — receipt, PDF, CSV, and OFX ingestion.
- `app/reconcile/` — deterministic candidate scoring, review policy, matching, and promotion.
- `app/agents/merchant_research/` — optional merchant research; this is an explicit egress boundary.
- `app/channels/` — local and opt-in capture transports.

## Close and reporting

- `app/close/` — close checklist, exceptions, and immutable snapshots.
- `app/db/repo_period_policy.py` — the shared closed-period mutation boundary.
- `app/reporting/period_statements.py` — one evidence-backed period-statement model for UI and exports.
- `docs/STATEMENT_EXPECTATIONS.md` — expected-statement evidence lifecycle.

## Verification

- `scripts/agentic-harness` — repository contract checks.
- `scripts/public-release-audit` — public-source privacy and fixture gate.
- `scripts/lint`, `scripts/test`, `scripts/build`, and `scripts/smoke` — canonical deterministic gates.
- `docs/EVALS.md` and `docs/FN149_PRECISION_HARNESS.md` — controlled reconciliation evaluations.

Repository fixtures are synthetic by contract. See `docs/FIXTURE_CONVENTION.md` and `PUBLIC_RELEASE_MANIFEST.json`. Environment-specific paths, identities, service topology, financial records, and rollout instructions do not belong in this repository.
