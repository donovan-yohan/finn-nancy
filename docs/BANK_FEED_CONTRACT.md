# Canonical bank-feed contract

## Decision

Use a first-class reviewed feed subject, separate from statement lines. A corroboration-only connector cannot provide daily canonical spending when no receipt or statement has arrived. Do not turn arbitrary sync windows into monthly statements and do not modify the intentionally isolated Sandbox connector.

`app/banking/` owns production transport, consent, token references, connection lifecycle, account bindings and immutable observations. `app/reconcile/feed.py` owns reviewed association and new booking. Existing receipt/statement reconciliation and reporting remain accounting authorities at their existing boundaries.

## Caller shapes

- Connection: integer local ID, production Item identity, canonical household member, private token reference, consent version, lifecycle, cursor and generation.
- Binding: provider account scoped to connection, canonical account, inclusion, economic ownership, primary/secondary role and revision. One active primary per canonical account. Ownership is not access control.
- Observation: immutable added/modified/removed revision with scoped provider transaction identity, exact signed cents, currency, date, pending predecessor, normalized merchant and retained description. Provider categories are suggestions.
- Feed subject: current observation pointer and state. Latest provider evidence does not overwrite previously accepted evidence.
- Review plan: subject/binding/target revisions, all eligible same-amount recent candidates, actionable blockers and fingerprint.
- Decision: match existing, explicitly record new with flow meaning, or exclude with reason. Operation key and expected plan fingerprint are required. New despite candidates requires explicit acknowledgement.
- Association: accepted observation and binding revisions, canonical transaction, actor, reason and operation identity. Corrections revoke with history; never delete evidence.

## Workflow

1. Member gives explicit versioned Plaid consent. The shared private household interface records that attribution; it does not claim individual authentication.
2. Start server-bound Link, finish through a server-verifiable completion flow, then discover accounts. Unknown exchange outcomes remain uncertain rather than retrying one-shot operations.
3. Bind each account explicitly, including joint accounts. One primary source may create new bookings. Secondary sources may only corroborate existing transactions. Never infer identity from name or suffix.
4. Sync fetches outside write locks, validates the complete bounded window, then atomically activates observations and cursor against the original generation/cursor. Sync never books.
5. Review a posted home-currency subject. Match existing receipts or cleared statement transactions without clearing/reassigning them. Or explicitly record a new uncleared transaction with validated flow meaning and a balanced Uncategorized split. Do not automatically accept provider category suggestions.
6. Receipt-first, feed-first and statement-first arrival orders each retain one economic booking. Late receipt candidates enter review rather than independent automatic insertion. Later statement reconciliation may clear an uncleared feed booking.
7. Changed/removed/superseded associated observations create durable review work while ledger history remains unchanged. Unresolved discrepancies block clean close but feed synchronization never satisfies statement expectations.
8. Namako inspects and proposes categories for the canonical split through the shared machine interface, then may drive the existing approval/undo endpoints with the shared API key. The operator selected UI-equivalent workflow access, not an extra agent-only approval requirement. Ordinary evidence and period checks remain.

For the initial import into an existing ledger, follow [remote history reconciliation](INITIAL_RECONCILIATION.md): explicitly map existing accounts, stage available history, match overlap, book only reviewed missing activity, retain legacy-only history and leave ambiguity unresolved rather than double-booking.

## Safety

Production host is fixed. Disable redirects and environment proxies, bound bytes/time/pagination, validate exact cents, and sanitize errors. Check strict-local mode, read-only mode and current consent at each outbound dispatch. Credentials and access/Link tokens live only in an atomic private store outside Git, with opaque references in SQLite. Never expose them to Namako or status responses.

Use `BEGIN IMMEDIATE` for review activation, operation replay and period checks. Guard both evidence/association changes and old/new financial dates. Add SQL backstops for direct-writer paths. Closed-period provider observations may be retained, but their accounting activation requires the shared reopen/guard workflow. Existing associated account/amount changes must not silently invalidate accepted evidence.

## Acceptance

Generated fixtures must prove receipt→feed→statement, feed→receipt→statement and statement→feed preserve one booking; equal legitimate charges remain ambiguous; primary/secondary joint feeds do not double-book; pending/foreign currency fail closed; stale plans, replayed operations and cursor races are safe; late modifications/removals preserve accepted history and block clean close; secrets never appear in browser/API/logs; strict-local and consent withdrawal stop every dispatch.

Production entitlement, supported institutions, credential provisioning, real partner consent, mobile OAuth return and physical-device acceptance remain external qualification. Passing generated provider tests is not real-bank acceptance.
