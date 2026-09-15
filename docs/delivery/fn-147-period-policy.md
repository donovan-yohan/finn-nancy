# Immutable period policy

The period policy prevents a closed month from being changed without an explicit, auditable transition.

## States and transitions

A month is `open`, `clean_closed`, `closed_with_exceptions`, or `reopened`. Clean close is derived only when no active typed exception remains. An exception close requires an exact acknowledgement for every current exception; changing the exception identity, evidence, affected rows, class, or reason invalidates the acknowledgement.

Reopen and override are explicit operations with actor, reason, and idempotency identity. An override reopens every affected closed month in the same database transaction before the requested mutation runs. Reusing an operation key for different semantics fails closed.

## Enforcement

- `app/db/repo_period_policy.py` owns lifecycle transitions and shared writer guards.
- `app/close/period_exceptions.py` derives typed exceptions from current evidence.
- `migrations/036_period_policy.sql` stores append-only cycles, events, snapshots, acknowledgements, and database triggers.
- Relationship writes evaluate every affected period, not only the caller's primary row.
- `BEGIN IMMEDIATE` serializes close decisions against competing writers.

Do not add a period-bound writer that bypasses the shared guard. Do not infer a clean close from a caller request, UI state, or an old generic closed row.

## Verification

Run focused period-policy and migration tests, then `scripts/agentic-harness`, `scripts/lint`, and `scripts/test`. Migration checks must cover fresh, legacy, and copied databases without using production data.
