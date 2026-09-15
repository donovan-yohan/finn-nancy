# Accounting contract

The executable source of truth is
`tests/fixtures/accounting/golden_month.json`, validated by
`app/accounting/contract.py`.

V1 uses signed integer cents in one home currency:

- Purchases and fees have one negative account leg.
- Income has one positive account leg.
- Refunds, reimbursements, and reversals have one positive leg linked to the
  event they offset. They reduce spending; they are not income.
- Internal transfers and card payments have linked equal-and-opposite legs on
  two owned accounts. They affect account activity but contribute zero income,
  spending, and external cash movement.
- Opening balances and adjustments are distinct report buckets. They do not
  silently become income or spending.
- Receipt evidence may create one provisional transaction. A statement
  corroborates that row or supplies one missing row; it never creates a second
  expense.
- Transaction splits must sum exactly to the signed transaction amount.
- Source identity is durable: receipt transactions link to receipt documents;
  promoted statement rows link to their source row; edited extraction retains
  the original extracted value and human-edit provenance.
- Exact duplicate imports have one statement row and one ledger effect.
- A missing expected statement or a closed-period mutation blocks clean close.
- Blank, malformed, or non-home currency remains reviewable evidence but cannot
  be promoted or aggregated. FX conversion and multi-home-currency reporting
  are out of scope.

## Persisted flow semantics

`transactions.flow_kind` is a closed, validated vocabulary:

`unknown`, `purchase`, `income`, `refund`, `reimbursement`,
`internal_transfer`, `card_payment`, `fee`, `interest`, `reversal`,
`adjustment`, and `opening`.

Category remains the purpose of a movement; `flow_kind` determines its report
effect. Unknown rows are durable review work and do not enter income or spending
totals. A known relationship-dependent flow is also excluded until its required
active provenance edge exists. Existing history is backfilled only when an old
writer contract proves the meaning: negative receipts on a persisted CAD account
become purchases, opening rows become openings, and reconciliation adjustments
become adjustments. Foreign/blank currency, sign, category, manual source, and
statement source do not prove meaning by themselves.

Relationships are append-only auditable edges. `transfer_pair` requires
equal-and-opposite `internal_transfer` or `card_payment` legs on different
owned accounts, and each leg belongs to at most one active pair.
`refund_of`/`reimbursement_for` share an aggregate cap equal to the purchase or
fee they offset. V1 `reversal_of` is one exclusive positive reversal of one
negative purchase/fee. `payment_for` remains an auditable supporting edge. A bad
edge, self-edge, duplicate active pair, provenance-free required flow, or
transaction edit that would invalidate an active edge fails closed. Corrections
revoke an edge with actor, reason, and timestamp rather than deleting history.

Every period-bound financial or evidentiary mutation passes the shared FN-147
policy boundary. A closed affected month rejects the mutation unless the same
transaction first performs an explicit audited reopen or an audited override
which reopens every affected old/new month. Relationship writes evaluate both
legs. Database triggers cover direct SQL and cascades, while
`BEGIN IMMEDIATE` linearizes close decisions against queued writers.

A clean close is derived only with zero active typed exceptions. Manual
adjustments force an exception close. Every exception requires an exact,
durable acknowledgement before the exception-close event is written; changing
its class, affected IDs, reason, or evidence invalidates the prior pre-close
acknowledgement. Acknowledgement never resolves an exception or upgrades the
historical snapshot. See
`docs/delivery/fn-147-period-policy.md`.

The shared writer and relationship implementation is
`app/accounting/flows.py` plus `app/db/repo_ledger.py`. Migration
`028_flow_semantics.sql` provides the database constraints, durable review
queue, backfill, audit rows, and typed report projection.
