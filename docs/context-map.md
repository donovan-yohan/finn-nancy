# Context map

This map points contributors to the executable contracts before they change behavior.

## Entrypoints and runtime

- `app/cli.py` — the `fn` command-line interface.
- `docs/REMOTE_API.md` and `app/api/remote.py` — remote shared-key HTTP/form CLI without local ledger access.
- `docs/FINANCIAL_SQL.md` and `app/api/analysis.py` — bounded read-only financial SQL over explicit projections, without raw settings/provider/filesystem access.
- `docs/INITIAL_RECONCILIATION.md` and `tests/test_remote_reconciliation.py` — first-import overlap, new activity, retained legacy history and category apply/undo through the installed remote CLI.
- `app/web/app.py` and `app/web/routes/` — FastAPI application assembly and HTTP/UI routes.
- `app/workers/` — durable background-job execution.
- `app/config.py` — environment-backed configuration and local/egress controls.
- `docs/UI_DESIGN.md` — navigation, Finn/Nancy colors, and durable upload presentation.
- `docs/HERMES_CHAT.md` and `app/hermes_chat/` — native Hermes chat bridge, authority and recovery contract.
- `docs/NAMAKO_FINANCE.md` and `app/api/finance.py` — machine inspection and split-category proposals shared by HTTP, local CLI and MCP; keyed clients may also drive the existing decision/undo workflows.
- `docs/BANKING.md` and `app/banking/` — opt-in Production Plaid transport, per-member consent, account bindings and immutable provider observations.

## Persistence and accounting

- `app/db/engine.py` and `app/db/migrate.py` — SQLite connection, transaction, and migration boundaries.
- `docs/HOUSEHOLD_RECOVERY.md` and `tests/test_household_restore.py` — complete ledger/originals/private-reference restore scope and generated isolated recovery proof.
- `app/db/repo_*.py` — persistence operations; do not bypass these with route-local SQL writers.
- `migrations/` — ordered, additive schema changes.
- `app/accounting/flows.py` — flow semantics and transaction relationships.
- `docs/ACCOUNTING_CONTRACT.md` — signed-cents, evidence, reconciliation, and reporting invariants.

## Ingestion and reconciliation

- `app/ingest/` — receipt, PDF, CSV, and OFX ingestion.
- `app/reconcile/` — deterministic candidate scoring, review policy, matching, and promotion.
- `docs/BANK_FEED_CONTRACT.md` and `app/reconcile/feed.py` — reviewed feed booking and corroboration, late receipt attachment and bank-evidence close blockers. Sync is not a statement or a booking decision.
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
