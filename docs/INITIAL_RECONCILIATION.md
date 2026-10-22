# Initial history reconciliation through the remote CLI

## Purpose

Preserve the existing canonical ledger and immutable documents when adding a bank feed. Staged provider history may overlap receipts and statements already recorded. Sync is evidence ingestion, not permission to book everything again or delete older history.

Use the application's URL and privately supplied `FN_API_KEY` with [the remote CLI](REMOTE_API.md). No server-side assistant runtime, SSH session or writable SQLite mount is needed. Every command below uses existing frontend/API routes and normal accounting guards. Institution login and each household member's actual consent remain account-holder operations.

## 1. Inspect and map accounts before decisions

```sh
fn api GET /connections/data/overview
fn api GET '/api/finance/transactions?account_id=1&limit=50'
```

Use actual local IDs returned by the application, not these example IDs. Follow `next_cursor` until null with the same filters. Compare each provider account against existing canonical accounts; names and suffixes are clues, not identity proof. Explicitly select personal/shared ownership, owner where personal, and primary/secondary role. One primary feed may book new activity for a canonical account; secondary joint feeds only corroborate existing transactions.

Submit a JSON object to the existing binding endpoint:

```sh
fn api POST /connections/data/bind --data reviewed-binding.json
```

Required fields: `connection_id`, `provider_account_id`, `account_id`, `included`, `ownership`, `role`, `expected_revision`. Personal ownership also requires `owner_member_id`. Use the current account binding revision from overview, or 0 for an unbound account. Do not create a new canonical account merely because the provider discovered it again.

Finalize mappings before accepting evidence. A binding revision invalidates existing review plans, and accepted associations can prevent a binding change that would invalidate their provenance. Repair associations through explicit supported review rather than rewriting historical bindings or audit rows.

## 2. Stage available provider history

```sh
fn api POST /connections/data/sync --data selected-connection.json
fn api GET /connections/data/feed
```

The sync body contains `connection_id`. A complete accepted window preserves immutable added/modified/removed observations. It does not create transactions or satisfy expected monthly statements. Actual provider coverage varies by account; absence from this feed does not prove that a legacy transaction is invalid.

Keep four groups in the review notes: known overlap, genuinely missing activity, retained legacy-only history, and unresolved ambiguity. Record the observed date range per included account, not an assumed complete history range. Large feeds must fit the generic CLI's bounded response; do not interpret a size error as an empty result.

## 3. Inspect each subject and decide explicitly

```sh
fn api GET /connections/data/plan/7
fn api POST /connections/data/decision/7 --data reviewed-decision.json
```

The plan returns `subject`, `binding`, `association`, `candidates`, `blockers` and `fingerprint`. Candidates include equal signed amounts in the mapped account within seven days, including cleared statement transactions; they are not ranked proof of identity. Inspect canonical details and row-linked evidence before choosing one:

```sh
fn api GET /api/finance/transactions/21
```

Every decision requires the exact current `fingerprint`, a stable unique `operation_key`, `action` and an explanatory `reason`.

- `match`: include the explicitly selected candidate `transaction_id`. This appends evidence without changing the original transaction, splits or cleared state.
- `new`: include explicit supported `flow_kind` (`purchase`, `fee`, `income` or `interest`). This creates one uncleared booking and a balanced Uncategorized split. If eligible candidates exist, new booking requires deliberate `acknowledge_candidates: true` after establishing that this is a distinct economic event. Never add that flag automatically.
- `exclude`: retain source evidence with the explicit exclusion reason; revoke an active association first when necessary.
- `revoke`: append a revocation of the current association with reason, retaining its audit history and canonical transaction.

Pending, foreign-currency, removed, changed-associated and relationship-dependent movements need their corresponding review path. Do not infer accounting meaning from sign or select an equal charge merely to empty the queue. Unresolved rows may remain close blockers; that is not a failed import.

An exact decision retry with the same operation key/content returns the original result. Different content with that key returns a conflict. A stale fingerprint returns `stale_review_plan`: inspect again, do not refresh the fingerprint blindly and resubmit the old judgment. After an uncertain network outcome, read current state before choosing whether an explicit retry is appropriate.

## 4. Categorize the resulting canonical transaction

```sh
fn api GET /api/finance/categories
fn api GET /api/finance/transactions/21
fn api POST /api/finance/category-proposals --data reviewed-category.json
fn api POST /actions/12/approve --form
fn api GET /api/finance/transactions/21
fn api GET '/api/reports/monthly_spend_by_category?month=2026-06'
```

The proposal body requires `operation_key`, `transaction_id`, selected `split_id`, `to_category_id`, `expected_state_digest` from inspection and `rationale`. Use actual proposal and transaction IDs from responses. Approval is the existing frontend workflow, available to a valid shared key without an extra agent-only permission layer. Provider category suggestions do not become accepted bookkeeping without this decision.

Read back the split and report after approval. A successful proposal response alone is not a category change. Reports distinguish accepted category spending from unresolved assignments; do not present an accepted-category subtotal as all household spending.

If the exact applied state has not changed, undo uses:

```sh
fn api POST /actions/12/revert --form
fn api GET /api/finance/proposals/12
```

Undo restores the selected split category and appends audit; it does not erase provider observations, operation receipts or the new economic booking. Stale state and closed periods retain their existing refusal/reopen rules. Reversing categorization is not reversing a feed booking.

## Executed generated acceptance

`tests/test_remote_reconciliation.py` starts the canonical application on owned loopback, injects only a generated provider transport and invokes the installed `fn` command as separate processes without database/provider configuration. Setup and independent SQL readback touch only a generated temporary ledger.

The journey covers existing cleared overlap, a genuinely missing purchase, older legacy-only history, two equal ambiguous candidates, pending and foreign-currency subjects, stale binding fingerprints, exact replay and operation-key conflict. It verifies one new booking/two associations, unchanged original transaction and split rows, unchanged document metadata/bytes, category approval/report readback/exact undo, and a durable unresolved ambiguity. Actual institution authorization, real history coverage and physical-device behavior remain separate qualification.
