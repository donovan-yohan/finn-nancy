# Plaid Sandbox candidate — local walkthrough

This is an uncommitted local candidate, not a production integration or release.
See [PLAID_SANDBOX.md](PLAID_SANDBOX.md) for the isolation and API contracts.
Operator-specific paths, process IDs, current state and verification logs belong
in the private, Git-ignored `data/qa/` handoff, not this document.

## Start and private configuration

From the isolated candidate worktree:

```sh
scripts/plaid-sandbox --port 8772 --credentials-file /path/to/private/sandbox.json
```

The launcher binds only to loopback. Open
<http://127.0.0.1:8772/connections>. Do not expose the unauthenticated candidate
through LAN binding, public tunnels or an external proxy.

Provision **Sandbox-only** client credentials privately using a masked setup
helper or the candidate's masked local setup form. For file provisioning, the
JSON object contains `environment` set to `sandbox`, `client_id`, and `secret`.
Use a regular current-user-owned file with mode `0600`, in a private directory
with mode `0700`. Never put credential values in shell arguments, chat, logs,
URLs or source files. Explicit local consent is required before network use;
provisioning a file does not itself authorize provider calls.

## Person-driven Sandbox acceptance

1. Enable the private Sandbox handoff with the versioned consent checkbox.
2. Add two synthetic household members and choose the first connection owner.
3. Confirm that owner's consent and open official Plaid Link. Use only the
   [documented synthetic Sandbox test login](https://plaid.com/docs/sandbox/test-credentials/),
   never real bank credentials. Automated password entry must use the approved
   browser vault. A vault is not required for the supported OAuth test below.
4. Complete official Link separately for the second member. Check authenticated
   account discovery and explicit ownership: member, joint/shared or unassigned.
5. Open **Accounts** for inclusion/ownership settings, then **Connections** to sync.
   Open **Activity** to inspect unbooked synthetic rows; current imports are visible
   even before account selection. Search, filter by connection owner/account/date,
   inspect source details and repeat sync. Confirm identities and counts rather
   than assuming a successful API response proves idempotence. **Home** shows real
   import counts and recent activity, not inferred spending or balances.
6. Authorize repair on an existing connection. Complete Link update mode and
   verify that it reuses the Item. Bank refresh time and local sync time remain
   distinct; an unavailable bank refresh time is unknown.
7. If testing disconnect, verify retained observations and audit. An uncertain
   remote acknowledgement must remain visibly uncertain, not falsely deleted.

An uncertain **new-connection exchange** has a separate recovery path. Inspect the
attempt in the Plaid Sandbox dashboard, then explicitly record that check in
**Link recovery**. The audit retains the actor, timestamp and original uncertain
attempt. This acknowledges the risk of an orphaned Sandbox Item; it does not
claim that remote access was revoked or the original exchange failed. Only then
can that member start a new connection with fresh consent. Do not replay the old
exchange, create duplicate members, or edit the database to bypass recovery.

Completion evidence must come from Plaid for the exact issued Link session.
If it is unavailable, exchange fails closed. Generated tests are not live
Sandbox proof; Sandbox does not establish Canadian production-bank reliability.

### Supported passwordless OAuth Sandbox walkthrough

Plaid's [OAuth testing guide](https://plaid.com/docs/link/oauth/#testing-oauth)
explicitly permits blank inputs in its sample OAuth flow. This is a provider
test feature, not a real-bank authentication bypass or a simulated Link callback.

1. In official Link, choose **Continue without phone number**. Search for
   **Platypus**, select **Platypus OAuth Bank**, then its normal OAuth institution
   (not Trusted Auth), and continue to its login page.
2. On Plaid's sample OAuth page, leave username and password blank and select
   **Sign in**. Select **Get code**, leave the sample code blank, then **Submit**.
   No passwords or one-time codes need to be typed, generated, or stored.
3. Select the synthetic **Plaid Checking** and **Plaid Credit Card** accounts,
   continue, accept the sample consent, and connect the account information.
4. Return to official Link, choose **Share data**, then **Finish without saving**.
   Wait for the local candidate to confirm its authenticated token exchange and
   account discovery. Do not start a second Link session while this is pending.
5. Repeat for the second household member. The same supported sample OAuth flow
   works in update mode for **Repair existing Sandbox Item**; no new Item should
   appear after repair.

This verifies a real connection to Plaid's Sandbox service, not a real Canadian
bank. It does not establish Canadian institution-specific login behavior.

## Stop and restart

Inspect `lsof -nP -iTCP:8772 -sTCP:LISTEN` and verify the listener belongs to
this candidate. Send SIGTERM only to that verified PID, then confirm the port
is released. Never kill a listener solely because it occupies the preferred
port. Restart through `scripts/plaid-sandbox` with the explicit private file.
Keep current PID and restart details in the private handoff.

The private `data/plaid-sandbox/` root retains connection/cursor state separately
from the canonical financial database. Discarding it is not a remote revocation
and is not a safe substitute for reconciling uncertain provider operations.

## Scope limits

- Investments and holdings are deferred.
- Identical account names/suffixes across Items retain separate identities;
  duplicate joint-account consolidation is not implemented.
- No ledger booking, statement-completeness acceptance, period close,
  categorization automation or production connection is authorized.
- Trial capacity is ten cumulative Production Items; deletion does not restore
  it. Sandbox Items do not consume that allowance.
- Record actual gate results and baseline failures in the private handoff;
  neither a mock integration nor a healthy listener completes live acceptance.
