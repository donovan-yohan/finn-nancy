# FN-141A statement expectation truth

## Goal

Persist which account statements should exist for a calendar month without
inferring completeness from whatever documents or transaction rows happen to
be present.

## Intent

- **Current behavior:** statement lines can be staged and reconciled, but the
  database has no independent account × period expectation.
- **Target behavior:** immutable account-policy versions prepare stable
  per-period requirement snapshots. Required periods then follow one audited
  statement-evidence lifecycle.
- **Non-goals:** FN-141A does not change close/signoff, reconciliation month
  scoping, ingest/review callbacks, statement metadata/balance anchors, or
  structured import adapters. Those integrations remain FN-141B and later
  tickets.

## Three independent axes

Do not combine these into a single mega-status.

### 1. Account policy

Policy versions are append-only and effective-dated:

- configuration: `configured | unconfigured`;
- requirement mode when configured: `required | no_statement`;
- cadence: `monthly | quarterly | annual | none`; and
- optional inclusive `active_from` / `active_to` dates.

`required` uses monthly, quarterly, or annual cadence. Quarterly and annual
policies require an anchor month from 1 through 12. `no_statement` uses
`none`. Null activity bounds are unbounded. A later policy version affects only
not-yet-prepared periods; an existing expectation continues to reference its
immutable policy snapshot until an operator explicitly refreshes it.

The ledger's present-tense `accounts.is_active` flag is not historical
evidence. Migration and requirement derivation never turn it into an invented
opening or closing date.

### 2. Per-period requirement

Each prepared account-month has exactly one requirement:

- `required`
- `waived`
- `not_due`
- `exempt`
- `unconfigured`

Activity is evaluated first. Any overlap with the calendar month, including
one day, is active. A month outside the inclusive activity interval is
`not_due`. Within the interval, an unconfigured policy is `unconfigured`,
`no_statement` is `exempt`, and required cadence is either `required` or
`not_due`.

A waiver is allowed only from `required + expected` with no active document
links. It requires actor and nonblank reason. Restoration appends a reversal
event and returns to `required + expected`; it never rewrites waiver history.

### 3. Required-evidence lifecycle

Only a `required` expectation has a lifecycle:

```text
expected --document_attached--> received --review_approved--> reviewed
reviewed --reconciled--> reconciled --unreconciled--> reviewed
reviewed|reconciled --new_evidence--> received
received|reviewed|reconciled --last_document_removed--> expected
```

No other edge is legal. The transition table in
`app/db/repo_statement_expectations.py` is the only production state-change
path. Every real mutation appends an audit event before updating current state,
and the database requires the same unique operation key on both records in the
same transaction. Duplicate prepare and attachment calls are no-ops and append
no history.

Reconciliation requires at least one linked line and every linked line to be
non-pending with a terminal disposition: `matched`, `promoted`, or `ignored`.
An explicit zero-activity statement needs the metadata/balance proof planned
for FN-142; absence of lines does not count as completion.

## Statement document links

- A source document must be a statement to attach.
- One source document may have only one active account-period link.
- One expectation may have multiple active statement documents.
- Automatic attachment requires at least one line and exact agreement across
  every line on one non-null account and one valid statement closing period.
- A manual link may resolve missing identity, but cannot override conflicting
  account or period evidence.
- Repeating an active link is idempotent.
- Active links prevent source deletion or reclassification away from
  `statement`. Once reviewed or reconciled, the source's storage reference and
  digest are also protected.

FN-141B must route ingest approval, rejection, line changes, and document
deletion through audited attach/detach and lifecycle recomputation. FN-141A
intentionally adds no parallel hook in those workflows.

## Conservative migration 030

Migration `030_statement_expectations.sql` follows FN-145's reserved migration
029.

- Every existing cash account receives a configured `no_statement` / `none`
  baseline policy.
- Every existing non-cash account receives an `unconfigured` baseline.
- New accounts receive the same conservative baseline through a database
  trigger, regardless of which application writer created them.
- No cadence, activity date, or statement period is inferred from transactions,
  `posted_on`, account names, last-four matches, or observed volume.
- A legacy statement document is linked only when it has at least one line and
  every line agrees on one non-null account and one valid declared
  `YYYY-MM` statement period.
- Multiple eligible documents for the same account-month share one
  `legacy_document` expectation. Ambiguous, malformed, zero-line, and
  non-statement sources remain unattached for review.
- Migration-created policy, expectation, attachment, and truthful
  `expected → received` events use the named `migration:030` actor.

The schema is additive and forward compatible. Code rollback can leave the new
tables unused; restoring a pre-migration backup is required only if a later
release starts depending on their data.

## Pattern fit

- **Problem shape:** a small finite lifecycle has invalid transitions and
  auditable guards, while policy and requirement are orthogonal dimensions.
- **Selected pattern:** explicit finite state machine from the pinned
  battle-tested-patterns catalog revision
  `08448fc6613d790ae635fa12751e8a3cf9617816`.
- **Rejected shape:** one flattened status would multiply independent policy,
  requirement, and evidence combinations.
- **Repository primitive:** the existing serialized SQLite `write_tx` supplies
  transaction atomicity; a maintained external state-machine dependency is not
  needed for this bounded transition table.
- **Defining invariant:** only enumerated lifecycle edges can change state, and
  each real edge has exactly one append-only audit event in the same
  transaction.
- **Executable proof:** `tests/test_statement_expectations.py` and
  `tests/test_statement_expectation_migration.py`.

## Context map

- Migration and guards: `migrations/030_statement_expectations.sql`
- Domain/repository path: `app/db/repo_statement_expectations.py`
- Account policy UI: `app/web/routes/manage.py`,
  `app/web/templates/manage.html`
- Synthetic fixture: `fixtures/sample.sql`
- Migration proof: `tests/test_statement_expectation_migration.py`
- State-machine, mutation, and UI proof:
  `tests/test_statement_expectations.py`, `tests/test_manage.py`

## Harness and risk seams

Focused:

```bash
scripts/test tests/test_statement_expectation_migration.py \
  tests/test_statement_expectations.py tests/test_manage.py tests/test_admin.py
```

Required exact-head gates:

```bash
scripts/agentic-harness
scripts/lint
scripts/test
scripts/build
scripts/smoke
git diff --check
```

Persistence proof covers fresh, second-run, pre-030 legacy, and copied
databases plus `PRAGMA integrity_check` and `foreign_key_check`. Negative tests
exercise illegal state edges, audit rewrites/deletes, policy rewrites,
duplicate document ownership, ambiguous automatic links, blank waivers,
pending/unmatched reconciliation, and closed-period writes.

The material residual risk is intentionally deferred workflow integration:
review/reconcile/delete writers do not yet synchronize expectation state.
Until FN-141B lands, the new truth model must not be used as a close/signoff
gate.
