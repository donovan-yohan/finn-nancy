# Evals & verifiability — how we build the AI in finn-nancy

Guiding principle (from [hamel.dev/blog/posts/eval-smell](https://hamel.dev/blog/posts/eval-smell/)):

> **"It's hard to eval" is a product-design smell, not an eval problem.** If a human can't
> easily verify an output, neither can an automated eval. Fix verifiability in the product and
> the eval strategy falls out for free.

Four rules we hold every AI surface to:

1. **Design for user verification first** — surface exactly what a human needs to check.
2. **Show your work** — expose provenance + intermediate steps (progressive disclosure).
3. **Break into verifiable units** — no monolithic outputs; expose the small checkable pieces.
4. **Anchor to existing trust** — retrieve/adapt from already-validated data (similar past
   transactions, prior categorizations) instead of generating from scratch.

This is why the roadmap items — approval UX, the "why?" explainer, streaming the agent's steps,
embeddings over transactions — are one design stance, not separate features.

## Applied to our AI surfaces

| Surface | Verifiable unit | What we show (provenance) | Eval |
|---|---|---|---|
| **Extraction** (`ExtractedReceipt`) | each field, not one blob | source image/page beside fields; low-confidence + `unreadable_fields`; arithmetic `subtotal+tax==total` | labeled receipts → field-level accuracy; arithmetic invariant is a hard assertion |
| **Categorization** (`classify`) | chosen category **+ why** + confidence | "Groceries — because 3 past LOBLAWS txns were Groceries" (alias/similar-txn anchor) | labeled txn→category (seeded from user-approved history); top-1 accuracy; **track Uncategorized %** |
| **Reconciliation** (match) | matched pair + candidate list + score breakdown (amount/date/merchant) + method | statement line, matched txn, why; rejected candidates | labeled pairs (seed from `~/finance/audit-*.md` double-count cases) → precision/recall + **no-double-count invariant** |
| **Reconciliation insight agent** (`recon_analyst`) | coverage totals, cited findings, proposed actions, and node trajectory | row IDs in every finding/action; deterministic tool evidence; guarded payloads | fixture DBs → coverage arithmetic, citation grounding, recurring/subscription warrant, action safety/reversibility, trajectory/tool provenance |
| **Chat / "why?"** | each claim cites the rows it used | stream steps (retrieve → SQL → reason) in a collapsible accordion | golden Q→expected tool-calls/answer; assert numbers come from tool output, not hallucination |

## Workflow — error-analysis first (not framework first)

1. **Look at real traces.** We have 1,331 real transactions + a live pipeline. Start with the
   biggest observed failure: **~23% Uncategorized**.
2. **Label a small set by hand.** The user approving/correcting *is* the labeling loop.
3. **Write cheap assertions** as pytest evals in `tests/evals/`, run alongside unit tests.
4. **Measure → fix the product (usually by making it more verifiable) → re-measure.**
5. **Don't build a generic eval dashboard/framework prematurely.** Simple assertions on real
   failure modes first.

## Where evals live

- `tests/evals/` — pytest; labeled fixtures under `tests/evals/data/`.
- Deterministic assertions run always; LLM-in-the-loop evals are marked (`@pytest.mark.llm`) and run
  on demand against the live endpoint.
- **Product-health metrics we track:** extraction field accuracy · classification top-1 +
  Uncategorized % · reconciliation precision + no-double-count · chat groundedness.

## Backlog approvals seed the classification eval set

Issue #8's backlog suggester never applies categories directly. It queues
recategorization proposals with neighbor evidence, and the human approval route
becomes the labeling loop. When a recategorization is approved through
`POST /actions/{id}/approve` and actually changes the transaction to a category
other than `Uncategorized`, the route upserts a `classification_labels` row with:

- the confirmed transaction/category pair and category name;
- merchant, description, and signed integer cents from the transaction;
- the proposal confidence, `agent_run_id` source, and proposed action id;
- the similar transaction ids that were shown as neighbor evidence.

Export the accumulated labels with:

```bash
fn export-eval-labels --out data/classification_labels.json
```

The JSON rows use the classification-eval fixture shape, including at least
`transaction_id`, `merchant`, `expected_category`, `neighbor_ids`, and
`confidence`, so they can be copied into a scratch eval fixture or loaded by a
future classification metrics test. The default export path is under `DATA_DIR`
to keep generated eval artifacts out of the source tree.

## Reconciliation insight agent

`app/evals/recon_insight.py` keeps the merged reconciliation analyst verifiable before live data:

- Coverage totals are recomputed independently from base `statement_lines` rows in Python, with a separate lookup for matched `Uncategorized` splits, then compared field-by-field.
- Recurring findings must cite a real meaningful delta emitted by the recurring tool, every meaningful delta must be represented, and price-increase/decrease findings must match the delta direction.
- New-subscription findings/actions must match the subscription watchlist evidence; `add_subscription` actions must cite the candidate's own rows and matching merchant/account payload is an extra constraint, not a substitute for evidence.
- Every finding and action must cite at least one real transaction, statement line, or category row.
- Proposed actions are adapted from analyst vocabulary to approval-queue handlers, then enqueued in a scratch DB; implemented handlers must apply and revert cleanly, while stub handlers must not mutate. Unknown analyst kinds are tolerated only when the guard has stripped them to free-text payload keys (`notes`, `rationale`); any structured payload on an unrouted kind fails the eval.
- Traced graph runs must follow the fixed node order and each analyst node's evidence must match the independent deterministic tool output.

Every eval class has a negative-control test: the fixture first passes faithfully, then the test injects one seeded violation (bad ID, empty evidence, wrong total, hallucinated recurring/subscription claim, omitted warranted insight, unsafe action, or write diff) and asserts the eval fails it. Predicate-level controls use fixtures with non-empty ground truth so bad recurring/subscription claims exercise the real matching predicates instead of only the empty-ground-truth path.
