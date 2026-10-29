# Screenshot catalog

These real application screens use the fictional June 2026 `household-v1` scenario: Alex and Jo, four local accounts, seventeen transactions and signed splits, nine accepted purchases, six budgets, and three validated transfer pairs. No bank connection or provider authorization is represented.

| Screenshot | Task and visible state |
| --- | --- |
| [Activity](media/activity-desktop.png) | [Trace a purchase](DEMO_WALKTHROUGHS.md#period-report): personal and shared activity, with both originals on Cedar Market after matching. |
| [Phone Processing](media/processing-phone.png) | [Preserve an original](DEMO_WALKTHROUGHS.md#receipt-capture): device-owned Pending beside a server-acknowledged Saved file; extraction has not run. |
| [Reconciliation](media/reconciliation-desktop.png) | [Match existing evidence](DEMO_WALKTHROUGHS.md#statement-reconciliation): a pre-staged statement line awaits confirmation against the existing expense. |
| [Household accounts](media/household-accounts.png) | [Organize local accounts](DEMO_WALKTHROUGHS.md#household-accounts): four named accounts; Telegram blocked by strict-local mode. |
| [Period statement](media/period-statement.png) | [Explain the month](DEMO_WALKTHROUGHS.md#period-report): open report, with all four accounts excluded from verified liquid position for missing stated as-of balances. |
| [Planning](media/planning.png) | [Review category budgets](DEMO_WALKTHROUGHS.md#planning): Shared, Alex, and Jo targets beside recorded actuals. |

## Reproduce the scenario

From the checkout with its existing locked `.venv`, start a disposable loopback server:

```sh
scripts/sandbox setup --scenario household-v1
```

Open the printed `origin` in a fresh disposable browser profile. Use the returned fixture IDs rather than assuming row numbers. The setup command remains in the foreground; Ctrl-C stops its server and removes its own workspace. See [sandbox isolation and Python consumers](SANDBOX.md) for the complete contract.

For the verified receipt-to-statement journey in a separate fresh instance:

```sh
scripts/sandbox replay --scenario household-v1 --journey match-receipt-v1
```

Reconciliation starts with the Cedar Market statement line already staged. The replay confirms its match through the real HTTP action and reads back ledger counts, original hashes, and report totals. To recreate the Activity image interactively, confirm that candidate in the setup instance and expand Cedar Market in Activity. Matching retains seventeen transactions; it does not book another purchase.

The phone image shows upload custody only. Its capture browser has background sync disabled; a Pending file stays device-owned, while Saved requires the actual server acknowledgement. Sandbox workers remain off, with staged jobs at zero extraction attempts. These uploads do not demonstrate extraction or new expenses.

The period remains open and close is blocked. The report's zero included liquid subtotal does not mean the household has a zero balance. Named account/person associations describe the scenario, not persisted ownership permissions; budget ownership is separate.

## Public asset boundary

Only the six PNGs above are included. `PUBLIC_RELEASE_MANIFEST.json` records their exact SHA-256 hashes alongside the existing approved assets. The unchanged release gate requires minimal PNG chunks, valid CRCs, no metadata, and dimensions no larger than 4096 pixels:

```sh
scripts/public-release-audit
```

Activity is cropped only below its last content row, retaining all visible evidence and a bottom margin. Other images retain their original dimensions. No originals, private capture logs, databases, or fixture-sidecar records accompany these screenshots.
