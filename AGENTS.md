# finn-nancy agent guide

## Mission

Finn Nancy is a local-first personal-finance application. The core user loop is:

1. preserve a receipt quickly from a phone;
2. ingest every expected account statement;
3. reconcile each statement line without double-booking;
4. close the period only when its evidence and balances are explainable; and
5. produce row-linked income, money-out, cash-movement, and account reports.

Start with [docs/context-map.md](docs/context-map.md). Delivery and release rules
live in [docs/DELIVERY.md](docs/DELIVERY.md).

## Canonical commands

Run repository gates through these wrappers so local and CI behavior stays
aligned:

```bash
scripts/dev
scripts/lint
scripts/test
scripts/build
scripts/smoke
scripts/smoke-capture-browser
scripts/soak-capture
scripts/dogfood-capture-phone --help
scripts/agentic-harness
scripts/reconciliation-eval
scripts/descriptor-resolution-eval
scripts/llm-eval smoke
scripts/backup-manifest --help
scripts/release-audit --help
```

Pass pytest selectors through `scripts/test`, for example:

```bash
scripts/test tests/test_reconcile.py
```

## Accounting and persistence invariants

- Money is stored as signed integer cents. Expenses are negative and income is
  positive, but sign alone is not sufficient to determine accounting meaning.
- Every transaction writer supplies a validated `flow_kind`; `unknown` is an
  explicit review state, not permission for reports to infer meaning from sign.
- Relationship edges use the shared flow service and remain append-only;
  corrections revoke with actor/reason instead of deleting audit history.
- Every transaction writer must create correctly signed splits whose total
  equals the transaction amount. Reports read splits.
- Captured originals are immutable evidence. Corrections preserve the original
  value and add auditable corrected state.
- A device capture id is created before network send and reused for every
  retry. `Pending` remains device-owned; `Saved` is reserved for the committed
  server acknowledgement, after which the device Blob may be released.
- Capture provenance and reliability events are append-only and content-free.
  Identical bytes share one document/job/ledger effect while each distinct
  channel occurrence retains its own origin row.
- Capture channels must exist in the exhaustive transport registry; never
  silently coerce an unknown channel/source to local-only. Retry events carry
  stable client/job stage identity and lifetime attempt ordinals.
- Statement rows are staged first. Only reconciliation may match or promote
  statement rows into ledger transactions.
- Expected-statement policy, period requirement, and evidence lifecycle are
  separate axes. Change them only through
  `app/db/repo_statement_expectations.py`; every real transition is audited.
- A receipt and its later statement line describe one expense, not two.
- Ambiguous matching, currency, flow meaning, or statement completeness fails
  into review. It must not be guessed into a clean close.
- FN-149 precision fixtures are deterministic synthetic contract data. A green
  controlled-reference report never enables production automation; only an
  exact approved production scorer/corpus/policy/knowledge tuple, sufficient
  independent resolution clusters, and an independently verifiable gold-blind
  run receipt may do that in later integration.
- Closed-period mutation must use the shared period guard. Do not add a new
  writer that bypasses it.
- Migrations `001_tables.sql` and `002_views.sql` are immutable baselines.
  Schema changes are additive, numbered migrations and must be idempotently
  testable on fresh, legacy, and copied databases.

## Privacy and data boundaries

- Never read, print, copy into git, or modify `.env` or credential files.
- Never put real financial data, receipt text, account numbers, API tokens, or
  production database rows in tests, logs, fixtures, prompts, PRs, or commits.
- Repository tests and smokes use generated fixtures and temporary databases.
- Real-data verification uses a consistent copy outside git; never mutate the
  production database for acceptance testing.
- The web/PWA, local drop folder, and local inference path are strict-local.
  Telegram or future web-search tools are explicit opt-in transports with
  provenance and disclosure.
- `STRICT_LOCAL_MODE=true` is the fail-closed default. Telegram must pass both
  that kill switch and current versioned consent before polling or sending.

## Development workflow

- Keep the production checkout on a clean exact `origin/main`. Implement each
  issue in a sibling worktree and a focused branch.
- Preserve unrelated work. Never use `git add -A`, destructive resets, or broad
  cleanup commands.
- Stage only the files owned by the current work packet.
- Add tests at the interface where the invariant can fail. An instruction in a
  document is not enforcement.
- Run targeted tests while iterating, then the complete canonical gate set.
- Use `scripts/smoke-capture-browser` for isolated Chromium evidence; it never
  targets a caller-supplied database or production service.
- Use one broad adversarial review before expensive full or device gates, batch
  valid findings once, and perform one focused re-review of the changed hunks.

## Exact-head handoff

Every PR handoff records:

- issue and intent;
- head SHA and tree SHA;
- changed files and migrations;
- exact commands, exit codes, pass/skip counts, and artifacts;
- review findings and disposition;
- browser, phone, copied-database, local-model, or deploy evidence required by
  the changed risk;
- rollback and remaining caveats.

An older green head is not merge evidence for a runtime, security, persistence,
or protocol change.

## Deployment

- Production is a single systemd instance behind Tailscale Serve. Never run
  system and user instances against the same database.
- Release only a clean exact merged SHA after a WAL-safe manifest-backed
  database backup and a successful migration/smoke on its proof copy.
- `/healthz` proves basic database liveness. `/version` proves the loaded build
  SHA, opaque database identity, schema digest, and complete ordered migration
  sequence digest.
- `scripts/release-audit` is read-only and fails closed unless the selected
  service owns the audited loopback listener; use it before and after the
  documented rollout in `docs/DELIVERY.md`.
