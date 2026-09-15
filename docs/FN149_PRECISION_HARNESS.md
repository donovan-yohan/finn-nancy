# FN-149 precision harness

This harness is executable backpressure for reconciliation decisions. It does
not implement a production matcher, merchant database, model call, web lookup,
or ledger write.

## Run

```bash
scripts/reconciliation-eval
scripts/reconciliation-eval --pretty --output /tmp/fn149-report.json
scripts/descriptor-resolution-eval
scripts/descriptor-resolution-eval --pretty --output /tmp/fn149b-report.json
```

Both commands validate manifest checksums before reading cases and exit
non-zero when an evaluator-health gate fails. Each output is one
machine-readable JSON object.

## Contract data

`tests/evals/data/fn149/` contains deterministic generated developer and sealed
JSONL splits, controlled-reference predictions, a checksum manifest, and an
exact approved-version policy. All rows and merchants are invented and carry
synthetic prefixes. Descriptor families and exact descriptors may occur in
only one split.

Regenerate the complete bundle with:

```bash
scripts/generate-reconciliation-corpus
```

The committed generator output is byte-for-byte tested. Never copy production
descriptors, receipt text, account data, or hashes of private rows into this
corpus.

## Decisions and metrics

Same-event candidates consume an arbitrary group of source row IDs, so one-to-
many, many-to-one, split, partial, transfer/refund, pending/final, repeated
amount, tip/fee, duplicate, and no-match cases use the same evaluator. An
automatic group is correct only when it equals a labeled candidate outcome;
each source row may be consumed once.

Canonical merchant and expense category are evaluated independently by source
row. Neither claim inherits correctness from a same-event decision. Unresolved
and unsupported labels require abstention.

For all three claims the report includes precision, recall, coverage, false
assignments, and a one-sided 95% Wilson lower bound. The policy requires:

- precision and its lower bound at least 99.5%;
- zero false automatic assignments;
- 100% required abstention;
- zero double-used source rows;
- full versioned coverage for each supported cohort; and
- category totals equal to the signed golden expense totals exactly.

Unresolved rows are excluded from resolved category totals.

## Controlled-reference truth

The original FN-149A generator's `prediction_for(case)` reads `case["gold"]`
and emits the corresponding controlled prediction. Its perfect metrics prove
that the evaluator, corpus invariants, abstention checks, signed totals, and
mutation tests are wired correctly. They do not measure an independent matcher
and must never be described as production precision.

FN-149B adds a separate descriptor-resolution packet under
`tests/fixtures/fn149b/` with checksum-bound public templates, synthetic
knowledge, evaluator-only labels, and policy. Its scorer-visible projection
contains only case identity, descriptor, and synthetic scope. Static and
mutation tests prohibit expected/gold/label fields, filesystem access, and
FN-149A `prediction_for` access in the scorer path. A physical gold-file
mutation must leave controlled scorer decisions unchanged.

Merchant and category results remain independent. The FN-149B report emits a
separate automation receipt; controlled-reference output is required to say
that production authority is disabled. Its repeated descriptor variants are
diagnostic rows, not independent statistical trials. Production confidence is
reported separately at the descriptor-template cluster level, where the
current packet is intentionally insufficient.

## Authority boundary

The committed FN-149A and FN-149B policies intentionally say, respectively:

```text
automation_authority=disabled_foundation_only
automation_authority=disabled_pending_approved_production_gate
```

Passing controlled predictions proves the harness can detect mutations; it
does not establish production precision or enable automatic writes. FN-149B
adds trusted scoped merchant knowledge and a central policy whose same-event,
canonical-merchant, and expense-category decisions are all independently
false. A later integration must supply an exact reviewed production
scorer/corpus/knowledge/policy tuple, enough independent resolution clusters,
and an independently verifiable gold-blind production-run receipt while
preserving these gates.

The mode fields are controlled placeholders with both local-model and web
search disabled. Live web or local-model behavior does not belong in this
deterministic CI path.
