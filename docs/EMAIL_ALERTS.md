# Forwarded email alerts

Finn Nancy shows forwarded card-purchase authorizations under **Bank feed → Email alerts**. Confirming a mapped account or dismissing an alert records a review reason. Neither action books, matches, or clears a transaction, or satisfies a statement requirement. Keep importing and reconciling statements.

## Prepare a dedicated mailbox

1. Use an alerts-only mailbox with certificate-validated implicit TLS (`IMAPS`), `IMAP4rev1` and the IMAP `LOGIN` command. Use a separate app password or read-only mailbox credential if available. Finn Nancy uses read-only commands, but the credential may grant broader rights. A folder or plus address in your personal inbox does not isolate credentials. Do not give Finn Nancy access to that inbox.
2. Enable the desired purchase alerts with your card issuer. In your mail service, forward only the confirmed issuer sender **and** the exact approved purchase-alert subject, and keep the source copy. Exclude declines, payments, fraud and account-recovery mail. Start with an empty destination folder; the first poll can scan existing mail.
3. Privately check an actual alert delivered by that rule. Record the exact `From`, original `To`, issuer-controlled DKIM signing domain (`d=`), exact purchase subject and decoded plain-text body format. The issuer's signature must still verify and must cover the original `To`, not the forwarding address. A rewritten `Fwd:` message, an attached message or an unsigned message is not supported. If you cannot verify the forwarding path or message format, leave polling off and use statements.

This version supports only IMAP card-purchase alerts. It does not import general receipts or documents and does not support OAuth, STARTTLS-only or IMAP4rev2-only servers. You do not need a public HTTP endpoint or SMTP receiver for Finn Nancy.

## Write `email-alerts.json`

Create a regular file at an absolute, symlink-free path outside any Git checkout. Keep it in a private directory, owned by the service user, with file mode `0600`. For the packaged container, bind-mount the **directory** read-only and ensure the file is owned by UID `10001`. Mounting a single file can hide its replacement or removal on the host. Keep credentials in the private file, not in environment values, Git, tickets or logs.

The example uses fictional addresses, a placeholder password, and a made-up template. Replace the values privately; Finn Nancy does not ship a default template for any issuer.

```json
{
  "consent_version": "forwarded-email-alerts-v1",
  "intakes": [
    {
      "transport": "imap",
      "kind": "card_purchase",
      "host": "imap.example.invalid",
      "port": 993,
      "username": "bank-alerts@example.invalid",
      "password": "example-password-placeholder",
      "folder": "INBOX",
      "format": {
        "sender": "cards@example.invalid",
        "recipient": "original@example.invalid",
        "signing_domain": "example.invalid",
        "subject": "Example purchase authorization",
        "body_pattern": "Approved on (?P<authorized_on>[0-9]{4}-[0-9]{2}-[0-9]{2})\\nCard ending (?P<card_last4>[0-9]{4})\\nAmount: (?P<currency>[A-Z]{3}) (?P<amount>[0-9]+\\.[0-9]{2})\\nMerchant: (?P<merchant>[^\\n]+)"
      },
      "card_accounts": {"0123": 1}
    }
  ]
}
```

- **Mailbox:** Each of the 1–4 intakes has its own connection settings and alert format. `port` defaults to `993`; the ASCII `folder` defaults to `INBOX`. `username` is the login for the destination mailbox, while `format.recipient` is the original signed recipient before forwarding.
- **Format:** Use the exact positive-purchase subject and a pattern that matches the entire normalized plain-text body. It must capture exactly `authorized_on` (`YYYY-MM-DD`), `card_last4` (four digits), `currency` (explicit uppercase three-letter code), `amount` (positive with two decimal digits) and `merchant`. Use non-capturing groups for any other grouping. The app stores the authorization amount as negative integer cents. A signed `multipart/alternative` with one plain-text part and one HTML part is supported; HTML is not rendered or fetched. HTML-only mail and attachments are not supported. Do not guess an ambiguous date, currency or card identifier.
- **Account mapping:** In each intake, map known card suffixes (strings, including leading zeroes) to existing Finn Nancy credit account IDs (integers). Supplementary cards can map to the same account. Do not map a suffix that could refer to two accounts within an intake. Missing or inactive mappings stay unresolved; inspect the alert before confirming an account.
- **Multiple sources:** You can use different issuer senders in one mailbox, or use separate mailboxes. Each distinct source has its own saved IMAP progress and retry queue. The source is identified by the mailbox host, port, username and folder plus the issuer sender, original recipient and DKIM signing domain. Two intakes with the same source are rejected even if their subjects or patterns differ: they would share a cursor, not get independent polling progress.

## Enable and verify polling

By setting `consent_version` and enabling the integration, you authorize Finn Nancy to log in to all configured mailboxes, search by sender, retrieve bounded matching messages and use DNS to verify signatures. There is one consent version for the file, not a separate consent for each intake. The app does not send, mark read, move or delete messages, or submit them to a model. It retains extracted authorization evidence and review history; the original stays in your mailbox.

After accepting that disclosure, set these runtime settings and restart the service through your normal deployment path:

```sh
EMAIL_ALERTS_ENABLED=true
EMAIL_ALERTS_CONFIG_FILE=/private/mounted/email-alerts.json
STRICT_LOCAL_MODE=false
```

Remove both legacy settings (`ROGERS_ALERTS_ENABLED` and `ROGERS_ALERTS_CONFIG_FILE`) when using this configuration. The two settings families cannot coexist; if both are configured, polling fails closed. Strict-local mode defaults to on and blocks polling; read-only mode also blocks polling. Turning strict-local mode off lifts the global block for other integrations too, so explicitly keep unused integrations disabled and leave their credentials unset. On a self-hosted instance not accessed directly on loopback, configure `PLAID_ORIGIN` with the exact private UI origin for browser review actions. This does not enable Plaid.

Wait for a poll, then check **Bank feed → Email alerts**. For each intake, verify that a privately qualified purchase appears once with the expected date, currency, amount, merchant and masked card suffix. Review it, refresh and confirm that it is still **Not booked**. Check the first statement for omitted purchases or amounts that differ from the authorization.

Polling scans bounded IMAP UID windows. A UIDVALIDITY change resets progress for that source and rechecks retained mail. Temporary DNS failures can leave a durable retry pointer without retaining the message body. Unsupported or unverified messages do not create review rows. The rejection count is per intake: a message can fail one intake and pass another. Keep originals until processed. If a format correction is needed, a previously rejected message can be retried by delivering the intact signed original as a new mailbox occurrence, not by forwarding it inline.

## Migrate an existing Rogers setup

Migration is optional. The [legacy flat file and settings](ROGERS_ALERTS.md#configure-finn-nancy) still support one mailbox and one purchase template. To switch:

1. Keep the existing database and a private copy of the legacy file. In a new private file, set `consent_version` to `forwarded-email-alerts-v1`, add the `intakes` array and place the legacy mailbox fields, `format` and `card_accounts` inside its first entry. Add `"transport": "imap"` and `"kind": "card_purchase"` to that entry. The old consent version does not belong inside the entry. Read the shared disclosure above before enabling it.
2. Keep the old source tuple exactly the same: host, effective port, username, effective folder, `format.sender`, `format.recipient` and `format.signing_domain`. This preserves its saved cursor and queued retries. Changing a source field can make the app scan that source as new. Converting the file, changing its path or moving an entry within the array is not a source change.
3. In one settings change, remove `ROGERS_ALERTS_ENABLED` and `ROGERS_ALERTS_CONFIG_FILE` and set the `EMAIL_ALERTS_*` pair. Restart and verify the existing source in **Email alerts** before adding others. The panel name changed; the existing database tables, API identifiers, alert history and ledger are unchanged. No database reset or re-import is needed.

To return to the single-intake setup, remove both `EMAIL_ALERTS_*` settings, restore the legacy pair with the original private file and restart. Additional intakes stop polling. Keep the database; do not drop tables or clear cursors.

## Withdraw consent or stop polling

Remove the private file seen by the service, or replace it atomically without the current `consent_version`. In-flight polls recheck the file before new network operations and before staging evidence. To keep polling off after restart, disable the active family's enable setting or restore `STRICT_LOCAL_MODE=true`. If retiring the integration, remove the forwarding rule and revoke the mailbox credential. Existing alerts and reviews remain; changing a review in a closed month still requires reopening it.
