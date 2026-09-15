# Evidence-backed period statements

A period statement is one deterministic model consumed by the UI, API, CSV export, and PDF export. The model must make every reported amount traceable to persisted source rows.

## Contract

- `app/reporting/period_statements.py` builds and validates the canonical model.
- `app/reporting/models.py` defines typed totals, flow buckets, account sections, close evidence, and provenance sets.
- `app/db/repo_period_statements.py` provides the read projection.
- `app/web/routes/close.py` exposes the model without independently recomputing totals.

Income, money out, offsets, transfers, adjustments, opening balances, and liquid position remain separate concepts. Unknown or unsupported-currency rows are not silently folded into home-currency totals. Evidence sets retain transaction, split, statement-line, source-document, relationship, assertion, exception, acknowledgement, and close-snapshot identities where applicable.

A closed period uses its immutable statement snapshot. If a legacy snapshot predates this contract or its canonical digest fails, the system reports that it cannot produce a truthful frozen statement rather than rebuilding history from mutable current state.

## Verification

Changes require focused model/export tests plus the canonical repository gates. Verify that UI, API, CSV, and PDF consume the same model and that tampered or incomplete frozen snapshots fail closed.
