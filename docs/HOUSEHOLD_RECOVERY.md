# Household recovery and isolated migration rehearsal

A successful SQLite backup is necessary but not sufficient to recover a household workspace. The ledger references immutable originals and private bank-token files. Preserve those together; do not promote the isolated Plaid sandbox database into canonical authority.

## Recovery scope

Keep these under private operator custody, outside Git and build artifacts:

1. The canonical SQLite database, captured through its online backup API with a verified backup manifest. Do not copy a live `.sqlite` file while ignoring WAL.
2. The canonical `DATA_DIR`, including content-addressed `originals/`, durable ingestion evidence and any selected ancillary checkpoint state. Preserve relative paths referenced by the database, original bytes and metadata.
3. The matching private Plaid token directory. Opaque `bank_connections` and pending Link references in SQLite are not the tokens themselves. Preserve directory/file ownership and private modes. A recovered ledger with missing token references is not a recovered working connection.
4. Operator configuration and credentials through the approved custody process, separately from the source release. Do not put `.env`, API keys, provider credentials, private token files or model/chat secrets into archives intended for source distribution.
5. The exact application release, dependency lock, additive migrations and release/backup manifests. A clean merged revision is required for a live release; an uncommitted local candidate is not an approved production release.

Retain the old instance and original backups. Source retention on the same physical host is not independent disaster recovery. Backup location, encryption, retention and acceptable recovery loss must be decided for the financial workspace; another application's exception is not authorization here.

## Consistency boundary

For a final cutover, obtain explicit authorization to fence every old writer and consumer before capturing the final database/data/token set. This includes web requests, API/CLI clients, workers, inbox watchers, scheduled ingestion, provider sync and optional chat/transports. Verify the actual service/listener owners; do not stop a guessed service or run a second writer against the same ledger.

The online SQLite backup is internally consistent while writes continue, but copying its referenced files and tokens at another instant does not automatically make a coherent whole-workspace backup. Use an approved quiet boundary or an independently proved snapshot procedure for the complete set. Pending one-shot Link/exchange state deserves particular care: never replay an uncertain provider operation to make a restore look healthy.

## Database manifest

The existing commands create an exclusive new backup and verify its bytes, identity, schema/migration sequence and migration proof copy:

```sh
scripts/backup-manifest create --source-db /operator/canonical/ledger.sqlite --backup /operator/protected/backup/ledger.sqlite --manifest /operator/protected/backup/manifest.json
scripts/backup-manifest verify --manifest /operator/protected/backup/manifest.json
```

The parent backup directory must already exist with appropriate private custody. Paths are examples, not deployment targets. Running this against real data still requires explicit access approval. This manifest covers the database, **not** the entire document/token/configuration recovery scope above. Track the other components privately with integrity manifests and a shared capture boundary; never publish filenames or digests that expose private financial evidence.

## Isolated restore acceptance

1. Restore into a new private destination, not over the source or a running application's state. Preserve the original backup unchanged. Supply the intended database/data/private-directory paths explicitly.
2. Run migrations on the proof destination only. Check database identity, ordered migration digest, schema digest, `integrity_check` and `foreign_key_check`. Compare pre/post financial rows and splits independently; schema success is not a data comparison.
3. Verify every restored document's referenced original exists and matches its recorded SHA-256. Preserve storage references or perform a separately reviewed, audited relocation; do not silently drop missing evidence.
4. Verify each required opaque token reference resolves in the matching restored private store without printing values. Use the application's store validator for ownership, modes and path restrictions; never put the private store beneath a Git checkout.
5. Keep provider traffic and consumers off during rehearsal: `STRICT_LOCAL_MODE=true`, `PLAID_ENABLED=false`, `READ_ONLY=true`, no inbox/Telegram/chat endpoint and no worker lifespan startup. Do not run the ordinary long-lived server just to inspect a copy if it would start consumers.
6. Construct the actual application and exercise Home, Activity, Connections, Bank feed, `/healthz`, `/version` and a keyed finance detail/report. Verify configured anonymous API refusal, restored identity and expected money. Liveness alone is not permission to enable sync.
7. Retain a private receipt naming the source release/backup manifests, component counts/integrity, comparison results, disabled effects, destination and remaining limitations. Do not put real rows or credential values in the receipt.

A rehearsal on generated fixtures proves the procedure's code paths, not that a real household backup exists or can be restored. A real proof copy remains a separate approved operation.

## Cutover and rollback

Only after approved restore/network/functional checks: choose the exact clean release, final quiet capture, old-writer fencing, destination activation and endpoint change. Verify one service owns the audited loopback listener and check `/version` through `scripts/release-audit` before and after the documented rollout. Keep Tailnet Serve private; never use Funnel or a LAN/public bind for this UI.

Before destination writes, rollback may retain the untouched original under explicit operator control. After destination writes, restarting an older source would discard new history and can create two writers. Stop, retain both states and review reverse synchronization or a compatible forward repair. Do not remove additive evidence migrations or erase audit rows to make old code start.

## Generated proof

`tests/test_household_restore.py` exercises an actual WAL-safe database manifest plus generated original bytes and matching private reference files, copied to an isolated destination. It verifies identity/integrity/foreign keys, exact ledger/split/source rows, immutable original hashes, token resolution without output, six composed routes and refused provider sync in read-only mode. The source and backup remain intact. This starts no workers or real provider operations and is not a VM, real backup, credential enrollment or cutover receipt.
