# Optional Plaid setup

Finn Nancy works without Plaid. Use manual transactions and statement imports if you do not want to authorize an external bank-data service.

This guide covers the canonical **Production** connection in Connections, not the separate [Plaid Sandbox workspace](PLAID_SANDBOX.md). Trial uses real Production data; it is not a sandbox.

## Is Trial available to you?

[Plaid's billing documentation](https://plaid.com/docs/account/billing/#trial-plans), checked October 29, 2026, says:

- Free Trial is available to new **US/Canada teams created on or after April 15, 2026**.
- A Trial team can create **10 Production Items over its lifetime**. Removing an Item does not restore a slot; after ten creations, more require a paid plan.
- An **Item is a login at a financial institution**, not an individual account. One login can cover chequing and savings accounts. Separate household logins can consume separate Items. See [Plaid's Item explanation](https://plaid.com/docs/quickstart/#create-your-first-item).
- **Transactions**, including Transactions Refresh, is among the Trial products. Finn Nancy requests Transactions, not every available product.
- Upgrading can start subscription charges for Transactions Items already created during Trial. On upgrade, retain access only to products explicitly requested in the Production application.

This is not a promise of unrestricted free access, eligibility, approval, or coverage for your institution. Check the current [Trial limitations and billing terms](https://plaid.com/docs/account/billing/#trial-plan-limitations) before connecting, deleting, or upgrading. Preserve the private token store: losing it does not erase created Items or refund quota.

**Application coverage is narrower than Trial eligibility:** the current Finn Nancy Link request uses `country_codes: ["CA"]`, language `en`, and the fixed Production API host. There is no `PLAID_COUNTRY_CODES` or `PLAID_ENV` switch for this connection. US team eligibility does not make this build a US-bank integration.

## 1. Obtain the right provider access

In your own [Plaid Dashboard](https://dashboard.plaid.com/):

1. Create a team and verify your email if you do not already have an account. Check eligibility rather than assuming an older team qualifies.
2. Open the [Trial application](https://dashboard.plaid.com/trial-plan), describe your actual personal/household financial-management use case, and complete the requested application steps. Wait for Production access to be enabled; a Sandbox key is not Production access.
3. Confirm **Transactions** entitlement and coverage for the institutions you need. Complete any product, application-profile, or use-case requirements shown for your team. This app does not supply a configurable data-use-case identifier in its Link request; if Plaid requires one, stop and resolve compatibility rather than inventing a setting.
4. Review institution/OAuth access. [Plaid's OAuth guide](https://plaid.com/docs/link/oauth/) says Trial teams need not complete the compliance-center registration requirements until upgrading to a paid plan. That is not a guarantee of institution support. Check your team's access status and complete applicable requirements when upgrading.

You do not need to paste keys into a browser page or run generic Quickstart API requests to connect Finn Nancy.

## 2. Prepare the server

First complete [local installation](GETTING_STARTED.md). Real Hosted Link requires the application's **matching HTTPS origin and completion URL**. Put the loopback service behind an authenticated, access-controlled private overlay with HTTPS, reachable by the browser returning from Plaid. Do not expose the app publicly to obtain a callback URL.

Configure these existing settings through your private operator environment, then restart the single server:

| Setting | Required value or meaning |
| --- | --- |
| `PLAID_ENABLED` | `true` |
| `STRICT_LOCAL_MODE` | `false`, deliberately allowing bank egress |
| `READ_ONLY` | `false` for consent, connections, and account edits |
| `PLAID_PRIVATE_DIR` | Absolute path to a private token directory outside every Git checkout; owner-only permissions (0700) |
| `PLAID_CREDENTIALS_FILE` | Absolute path to a private JSON file outside every Git checkout; owner-only permissions (0600), containing `client_id` and the **Production** `secret` |
| `PLAID_ORIGIN` | Exact HTTPS browser origin: scheme and hostname, with port if needed; no path, query, fragment, or trailing slash |
| `PLAID_COMPLETION_URI` | Same scheme and host/port as the origin, with `/connections` as the return path; no query or fragment |

Use paths owned by the service user; symlinks, including symlinked ancestors, are refused. Ignoring a file in Git does not make a path inside a checkout acceptable. Supply secrets using your private custody process, not shell history, source files, chat, screenshots, or this guide. SQLite stores opaque references; back up the matching token directory separately with its permissions intact.

Turning strict-local mode off also removes that global block for other optional transports. Keep `MERCHANT_SEARCH_ENABLED=false`, leave Telegram credentials unset, and review any already-configured integrations if you intend to enable only Plaid. Strict-local mode does not govern the separate model endpoint.

## 3. Connect and map accounts

1. Open Connections through the configured HTTPS origin. Add your own household member name; add other members only as needed.
2. Select the named member, read the current disclosure, and grant that member's explicit consent. Adding a person alone neither grants consent nor opens Plaid.
3. Choose Add bank and complete the external Plaid Hosted Link authorization. Return to the **same browser session** and let Finn Nancy verify completion. A redirect alone does not prove success.
4. Map each returned account explicitly: personal or shared ownership, the owner when personal, the ledger account, and household inclusion. Who connected a bank is not automatically who owns all its accounts.
5. For the same joint account observed through two members' connections, intentionally map both to the same ledger account and select primary/secondary roles. Do not create two economic accounts for one shared account.
6. Sync, then review Bank feed. Sync creates observations, not an automatic booking decision or a complete account statement. Reconcile the period's statements before closing.

See [bank-feed accounting](BANK_FEED_CONTRACT.md) for reviewed booking and evidence rules.

<details>
<summary>Hosted Link, OAuth, and webhooks: what this app actually sends</summary>

Finn Nancy sends a `hosted_link` object with `completion_redirect_uri` in `/link/token/create`. It saves the Link token privately and checks `/link/token/get` on the server before exchanging a verified public token. It does not trust a public token supplied by the browser.

There is **no public webhook requirement** in this implementation. Plaid recommends webhooks for Hosted Link, but also documents polling via `/link/token/get`; Finn Nancy uses that server-verification path. Do not open public ingress or add an invented webhook setting.

The Hosted Link completion URL returns to Finn Nancy; it is not an application-supplied OAuth `redirect_uri`. This implementation sends no `redirect_uri`, SMS/email delivery configuration, or configurable OAuth redirect setting. Do not copy those fields from Plaid's generic examples. Follow current [Hosted Link guidance](https://plaid.com/docs/link/hosted-link/) and resolve any institution-specific requirements with Plaid before real use.

</details>

<details>
<summary>If a connection cannot complete</summary>

- **Disabled / strict-local / read-only:** resolve the named server setting; do not retry authorization repeatedly.
- **Invalid configuration:** check absolute private paths, ownership/modes, and the matching HTTPS origin/return URL. The setup overview does not prove credentials are valid; they are checked when connecting.
- **Access or institution unavailable:** verify Production Transactions entitlement, Trial quota, and institution requirements in Plaid. Do not switch in a Sandbox secret.
- **Interrupted browser flow:** return to the same session. Browser recovery is bounded; a server restart does not make an arbitrary token acceptable.
- **Exchange or removal uncertain:** stop. Do not repeatedly reconnect, exchange, or remove to force success. Verify the remote status with Plaid/bank support; a local timeout can conceal a completed remote action.

Connection removal does not refund Trial quota. Deleting local state is not confirmed remote revocation. Keep token custody until the remote outcome is known. See the [banking lifecycle contract](BANKING.md).

</details>
