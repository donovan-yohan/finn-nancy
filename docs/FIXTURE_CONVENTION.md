# Synthetic fixture convention

Public fixtures must be invented, deterministic, and visibly synthetic. They must never be copied from a household ledger, statement, receipt, account, card, routing record, email inbox, merchant history, or local deployment.

## Required marker

Every structured finance fixture under `fixtures/`, `tests/fixtures/`, or
`tests/evals/data/` must have its exact SHA-256 recorded in
`PUBLIC_RELEASE_MANIFEST.json`. Visible `SYNTHETIC_FIXTURE` markers remain useful
to readers, but they do not bypass manifest review. The public-release audit
rejects unreviewed fixture additions and hash drift.

## Values

- Use invented people, institutions, merchants, and localities, prefixed with `Synthetic`, `Example`, or `Fixture` where practical.
- Use `example.invalid` for example email addresses and documentation-only hosts.
- Use documentation address ranges only for network examples.
- Use deterministic identifiers that are clearly test-only, such as `SYNTHETIC_CARD_9001` or `fixture-txn-001`; never use a value derived from a real account, card, statement, or transaction.
- Preserve parser edge cases by constructing documents in tests rather than retaining source documents.

Run `scripts/public-release-audit` before publication. In Git checkouts the gate
checks cached and non-ignored untracked files; it does not inspect Git history.
Publish only from a history-free repository after separate history review.
