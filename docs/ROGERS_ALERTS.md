# Rogers Bank email alerts

Finn Nancy can read forwarded purchase alerts from a mailbox you control and show them under **Bank feed → Rogers Bank alerts**. These are card authorizations, not posted transactions. Confirming an account or dismissing an alert records a review reason; neither action changes the ledger, clears a transaction, or counts as a statement. Continue importing and reconciling statements.

## Choose a forwarding destination

1. Create a dedicated alerts-only mailbox with any provider that offers certificate-validated IMAP over TLS (`IMAPS`) and a separate credential. A self-hosted mailbox works too. Use a provider-issued app password or read-only mailbox credential if available. Finn Nancy uses read-only mailbox commands, but the provider may grant the credential broader rights. Do not give it your personal mailbox password, app password, or OAuth access. A plus address or a folder in your ordinary inbox does not isolate credentials.
2. Enable the desired email purchase alerts in [Rogers Bank Manage Alerts](https://www.rogersbank.com/en/how_to_set_up_alerts/) for each card you want to track. Rogers describes alerts for selected purchase locations and declines; do not assume all online, recurring or other purchases generate email. Leave fraud, account recovery, and other security mail out of the forwarding rule.
3. In your existing email service, verify the forwarding address if required. Forward only the confirmed issuer sender **and the approved purchase-alert subject**, and keep the source copy. Do not turn on forwarding for all mail. Keep the destination empty before setting up forwarding. Automatic redirect or rules that preserve the original signed message can work. A rewritten `Fwd:` message, an attached message, or an unsigned message is not supported. Test the exact forwarding rule first: the original signature must still verify in the destination.

Finn Nancy does not need a public HTTP endpoint, a public SMTP server, or Gmail OAuth. The destination address is chosen in your mail service; the app connects to the configured destination mailbox, not to the personal inbox where the rule runs.

## Configure Finn Nancy

Before enabling regular polling, qualify an actual delivered alert privately. Confirm the exact `From` address, original `To` address, issuer-controlled DKIM signing domain (`d=`), exact positive-purchase subject, and decoded body format. The same issuer signature must cover the original `To`, not the forwarding address or a relay's assertion. This prevents an authentic alert sent to someone else from mapping to a local card with the same last four digits. Keep originals, sender details from your account, and the configured card suffixes out of Git, tickets and logs. No shipping format or sender is assumed: the example below is generated and is **not** a Rogers Bank template. If your real forwarding route or message format cannot be verified, keep alerts off and use statements.

Create an absolute JSON config file outside your Git checkout. Store it in a private directory owned by the service user, with mode `0600` on the file. For the app container, bind-mount the **directory** read-only, with the file owned by UID `10001`. Do not mount the file individually: atomic replacement or removal on the host must also change what the container sees. Do not set a password in an environment variable. Configure the mailbox host, port (default `993`), ASCII folder (default `INBOX`), dedicated credentials, and the qualified alert format. Example values are fictitious:

```json
{
  "consent_version": "rogers-forwarded-alerts-v1",
  "host": "imap.example.invalid",
  "port": 993,
  "username": "bank-alerts@example.invalid",
  "password": "replace-in-private-file",
  "folder": "INBOX",
  "format": {
    "sender": "cards@example.invalid",
    "recipient": "original@example.invalid",
    "signing_domain": "example.invalid",
    "subject": "Example purchase authorization",
    "body_pattern": "Approved on (?P<authorized_on>[0-9-]{10})\\nCard ending (?P<card_last4>[0-9]{4})\\nAmount: (?P<currency>[A-Z]{3}) (?P<amount>[0-9]+\\.[0-9]{2})\\nMerchant: (?P<merchant>[^\\n]+)"
  },
  "card_accounts": {"0123": 1}
}
```

The pattern must match the entire normalized plain-text body and supply the five named captures shown. This includes the plain-text part of a signed `multipart/alternative` with one plain-text part and one HTML part. HTML is never rendered or fetched; HTML-only alerts and attachments are not supported. Use an explicit purchase-only subject and literal wording that identifies a successful authorization, not a decline, payment, or transfer. The date must be ISO `YYYY-MM-DD`, the currency an explicit uppercase three-letter code and the amount positive with two decimal digits; Finn Nancy stores an exact negative signed-cent amount. If an alert only supplies an ambiguous symbol, date or card identifier, do not generalize its format to guess. Map each known card suffix to its existing Finn Nancy credit account ID. Supplementary cards can map to the same account; never configure a suffix shared by two different accounts. A missing or inactive mapping stays unresolved. Inspect the matched alert in the app before confirming the account.

By setting the current consent version and enabling the integration, the operator authorizes Finn Nancy to log in to the configured mailbox, search messages by sender, retrieve bounded matching email bodies, and use DNS to verify signatures. No mail is sent, marked read, moved, deleted, or submitted to an LLM or other processing service. The app retains only the extracted authorization and review history; the mailbox remains the owner of the original. Use an expiration policy and backups appropriate for the sensitivity of these messages.

Only after accepting that disclosure, set these runtime settings and restart the service through its normal deployment path:

```sh
ROGERS_ALERTS_ENABLED=true
ROGERS_ALERTS_CONFIG_FILE=/private/mounted/rogers-alerts.json
STRICT_LOCAL_MODE=false
```

Turning strict-local mode off removes the global block for other integrations too. Explicitly keep unused integrations disabled, for example `MERCHANT_SEARCH_ENABLED=false`, and leave unused transport credentials unset. Read-only mode also prevents mailbox polling. On a self-hosted instance, the Bank feed review actions use the same trusted browser origin/session protection as local account mapping; set `PLAID_ORIGIN` to the exact private UI origin when not accessing a loopback-bound app directly. That setting does not enable Plaid.

## Verify and stop

Wait for a normal poll, open **Bank feed**, and confirm that a real forwarded purchase appears once with the expected date, currency, amount, merchant and masked card suffix. Review it, refresh, and check that it still says **Not booked**. Repeat for each card or alert format you intend to use. Compare your first statement for omissions and authorization-versus-posting differences. The implementation handles one configured mailbox and one qualified purchase template per instance.

Polling searches bounded IMAP UID windows and starts with messages already in the dedicated folder. If UIDVALIDITY changes, Finn Nancy discards obsolete UID pointers and rechecks retained messages. When DNS is temporarily unavailable, the affected UID goes onto a durable retry list without blocking later mail. Keep originals in the dedicated mailbox until processed. Unsupported or unverified messages contribute only a count, not a review row. Investigate a rising skip count or a repeating failure at the mailbox, without copying email into logs. To replay mail that was rejected before a format correction, redeliver the intact signed original as a new mailbox occurrence; a manual inline forward is not a substitute. If you move to another mailbox, an intact copy that verifies there can restore the account mapping without creating a second alert or replacing its review history.

To withdraw consent, remove the private config file or atomically replace it without the current `consent_version`. In-flight polls recheck the file before new network operations and before staging. For a lasting stop, disable `ROGERS_ALERTS_ENABLED` (or restore `STRICT_LOCAL_MODE=true`) and restart. Remove the forwarding rule and revoke the mailbox credential if retiring the integration. Reviews and existing alerts remain in the database. Closed periods can receive provisional alerts without re-opening, but changing the review in a closed month requires an explicit reopen. Do not drop the additive schema as a rollback step.
