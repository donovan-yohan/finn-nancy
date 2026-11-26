# Rogers Bank email alerts (legacy setup)

[Forwarded email alerts](EMAIL_ALERTS.md) is the current setup guide. Existing Rogers configurations still work unchanged and now appear under **Bank feed → Email alerts**. They remain review-only card authorizations, not ledger transactions or statement evidence.

## Choose a forwarding destination

Use the [dedicated mailbox and forwarding instructions](EMAIL_ALERTS.md#prepare-a-dedicated-mailbox). Enable the desired email purchase alerts for each card in [Rogers Bank Manage Alerts](https://www.rogersbank.com/en/how_to_set_up_alerts/). Rogers offers alerts for selected purchases and declines; forward only qualifying purchase alerts and do not assume every purchase generates an email.

## Configure Finn Nancy

The legacy settings accept only the original single-mailbox, single-template JSON file. Its top-level `consent_version` is `rogers-forwarded-alerts-v1`, alongside `host`, `port` (default `993`), `username`, `password`, `folder` (default `INBOX`), `format` and `card_accounts`. There is no `intakes` array and there are no `transport` or `kind` fields. Use the same [private file, mailbox and format requirements](EMAIL_ALERTS.md#write-email-alertsjson).

After accepting the [polling disclosure](EMAIL_ALERTS.md#enable-and-verify-polling), keep the legacy pair and leave both `EMAIL_ALERTS_*` settings unset:

```sh
ROGERS_ALERTS_ENABLED=true
ROGERS_ALERTS_CONFIG_FILE=/private/mounted/rogers-alerts.json
STRICT_LOCAL_MODE=false
```

Do not configure both settings families: polling fails closed. To use the preferred versioned file, follow the [migration steps](EMAIL_ALERTS.md#migrate-an-existing-rogers-setup) to keep the existing cursor and reviews.

## Verify and stop

Follow the [verification](EMAIL_ALERTS.md#enable-and-verify-polling) and [consent withdrawal](EMAIL_ALERTS.md#withdraw-consent-or-stop-polling) instructions. For a lasting stop on the legacy setup, disable `ROGERS_ALERTS_ENABLED` or restore `STRICT_LOCAL_MODE=true` and restart. Keep the database and its review history.
