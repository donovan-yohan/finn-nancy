# Architecture

`finn-nancy` is a single-process FastAPI application backed by SQLite. It renders server-side HTML with Jinja2 and HTMX, runs migrations from ordered SQL files, and uses a durable SQLite jobs table for slow ingestion and reconciliation work.

## Core data flow

1. A user imports a document or creates a record.
2. The application stores the original outside Git and creates a staged record.
3. Deterministic parsing and validation run first; configured LLM features may provide structured suggestions.
4. Reconciliation either matches evidence, creates a review item, or promotes a clearly unmatched line with a signed split.
5. SQL views power dashboards, budgets, reports, and read-only assistant tools.

Money is stored as integer cents. Expenses are negative, income is positive, and report views read `transaction_splits`.

## Local-first boundary

The application keeps its database and originals on the host by default. "Local-first" does not mean "no egress": a configured LLM endpoint can receive prompts and document-derived content; explicit merchant research can send sanitized descriptor queries; and enabled Telegram capture sends content through Telegram. See the README for the current feature controls.

The browser UI intentionally has no app-level authentication. It must bind to loopback unless an authenticated private overlay is enforcing access. A public or LAN listener is outside the supported threat model.

## Reliability boundaries

- SQLite uses WAL mode and a process-level write path.
- Migrations are applied once through the migration ledger.
- The durable job queue supports retry and recovery, not distributed processing.
- Statement parsing and reconciliation treat source documents as untrusted and route ambiguous outcomes to review.
- Assistant output is advisory; it does not replace accounting review.

## Synthetic data

Repository samples and evaluation corpora use deterministic, clearly synthetic fixture namespaces. Production exports, statements, receipts, databases, backups, and environment files are excluded from the source release. See [FIXTURE_CONVENTION.md](FIXTURE_CONVENTION.md).
