# Immutable local container candidate

This packages the existing server, not a deployment controller. No database,
operator configuration, credentials or receipt originals are image inputs.
Runtime never installs dependencies, initializes, seeds, migrates, repairs or
backs up state. The ordinary application lifespan still starts its worker and
LLM warm-up; `READ_ONLY=true` is **not** consumer-free restore inspection. Use
[HOUSEHOLD_RECOVERY.md](HOUSEHOLD_RECOVERY.md) for recovery boundaries.

## Build an exact committed source

After review and the normal repository gates, use a clean local checkout and a
full committed SHA (replace the example value):

```sh
scripts/build-image --revision FULL_40_CHARACTER_COMMIT_SHA --platform linux/arm64 --tag finn-nancy:reviewed
scripts/smoke-image --image finn-nancy:reviewed --artifacts /protected/new-generated-proof
```

The build wrapper rejects tracked or untracked uncommitted files. Ignored local
state is neither read nor sent: Git archives only the selected commit's explicit
app, migrations, package/lock, legal and launcher allowlist. Symlinks and
unexpected input types fail closed. `--repository` selects another local clean
repository; `--export-context NEW_TAR_PATH` exports exactly those inputs without
building. Never build with a checkout-wide `docker build .` context. The deny-
by-default `.dockerignore` is defense in depth, not the source/privacy authority.

Python and uv base images are digest-pinned. `uv sync --frozen --no-dev
--no-editable` runs only in the build stage. Runtime contains the root-owned app,
venv, migrations and legal notices, no Git metadata or runtime uv command. The
migrations stay at `/opt/finn-nancy/migrations`, where the existing runner expects
them. No dependency, migration or application behavior is changed here.

The default is `linux/arm64`; `--platform linux/amd64` builds the other supported
image. Record whether execution was native or emulated. Neither is VM deployment
proof. The script loads a local image and never pushes. Retain its JSON output,
build log, image ID and independent source manifest. OCI revision/tree labels and
`FINN_NANCY_BUILD_SHA` / `FINN_NANCY_BUILD_TREE_SHA` describe the selected commit;
they are claims, **not** signed attestations, and runtime environment overrides
can change `/version`. Independently bind the intended commit/tree to the exact
image ID/digest, not merely a mutable tag or environment value.

An uncommitted packaging candidate must not claim its parent HEAD includes the
packaging. A generated isolated Git snapshot may be used for local acceptance
only when explicitly identified as a synthetic candidate, with the parent SHA
and exact packaging-file hashes retained separately. Rebuild and rerun acceptance
from the real committed reviewed revision before any release.

## Persistent state and admission

Defaults: `DATA_DIR=/var/lib/finn-nancy`,
`DB_PATH=/var/lib/finn-nancy/ledger.sqlite`, `ADDR=0.0.0.0:8080`.
Mount the **whole** stable state root, not one SQLite file: WAL/SHM, immutable
`originals/`, ingestion evidence and selected checkpoint state belong together.
The state directory and database must already exist and be writable by fixed
UID/GID **10001:10001**. Provision ownership deliberately outside the container;
the launcher never creates directories, changes ownership or creates a database.
`DB_PATH` and `DATA_DIR` must be absolute, with a regular database inside the
state root; symlinks in either path (including ancestors) are refused. Do not
let other actors replace or rename the mounted state while the service runs.

With no runtime arguments, admission opens the existing DB using an escaped
SQLite `mode=ro` URI and `query_only`, verifies packaged migrations include the
nonempty immutable baselines, requires the complete ordered ledger to match,
and requires an existing valid opaque database UUID. The connection is closed
before exec of `fn serve --db ... --addr ...`. Any runtime argument is refused;
there is no maintenance subcommand. Errors are content-free and occur before
web imports/listener/worker startup. Main DB bytes are not changed on rejection;
SQLite read-only WAL access may create shared-memory sidecars. This is admission,
not a full integrity/data/backup audit, nor a canonical golden schema digest:
legitimate migrated legacy DDL can differ. Compare the actual `/version` schema
digest to the independently approved proof copy at rollout.

## Private ingress and configuration

**Do not expose this unauthenticated UI to a LAN or the Internet.** Container
`0.0.0.0` is an isolated bridge-internal bind only. Host publishing must explicitly
bind loopback, e.g. `--publish 127.0.0.1:8080:8080`; never use `-P`, host networking,
unqualified `8080:8080`, Funnel, or a public reverse proxy/tunnel. The existing
access-controlled private Tailnet Serve boundary remains required for remote
clients. The narrowly scoped bridge-internal bind exception is defined in the
current [delivery policy](DELIVERY.md#deployment-boundary); it does not authorize
deployment.

A future **authorized** operator invocation has this shape (not an instruction to
run a second instance or to deploy):

```sh
docker run --detach --name finn-nancy --pull=never \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --publish 127.0.0.1:8080:8080 \
  --mount type=bind,src=/operator/private/full-state,dst=/var/lib/finn-nancy \
  --env API_TOKEN finn-nancy:reviewed
```

Choose restricted bridge/egress policy separately for the real deployment; the
acceptance harness uses `--network=none` and publishes **no** ports. Its HTTP
checks execute within the container. Do not assume a bridge prevents outbound
traffic. `STRICT_LOCAL_MODE=true`, `PLAID_ENABLED=false` and
`EMBEDDINGS_ENABLED=false` are image defaults. Telegram, inbox and external chat
are unset unless explicitly supplied. Existing app settings govern opt-ins;
strict-local does not suppress the existing local-model warm-up. Acceptance
points it at unavailable container loopback with a one-second timeout and zero
retries, with network isolation preventing external egress.

Supply `API_TOKEN` via the approved external environment custody process; the
image neither bakes a value nor supports an invented `API_TOKEN_FILE`. Do not
print credentials, place them in build arguments, shell history or source. The
keyed API is disabled without an API token; the web UI is not thereby protected.
Optional existing private-file settings such as `PLAID_PRIVATE_DIR`,
`PLAID_CREDENTIALS_FILE` and Hermes chat token-file references require separately
owned, appropriately protected mounts and explicit review. Do not merge secrets
into the main state/image or export their values. Retain and validate private
reference state separately per the recovery guide.

## Generated acceptance, shutdown and rollback

`smoke-image` accepts an image and an exclusive new artifact directory, never a
caller database/state path. It creates only labeled, uniquely named disposable
Docker volumes/containers, disables network/publishing, generates/migrates a DB
through the existing code, and captures generated image bytes via the existing
capture path. Its queued job is held in the future, so normal workers start but
do not consume that fixture. It proves health/version revision+tree, migration
and identity evidence, HTML/static assets, keyed read and absent/wrong-key
refusal, nonroot operation, immutable code/venv and writable full state. It stops
with SIGTERM and recreates against the same volume, checking identity,
source-document references, original hashes and exact queued rows. It also
proves absent/blank/corrupt state, missing/unknown/reordered migrations and
missing/invalid identity refuse before listening with unchanged main DB bytes,
and runtime maintenance arguments refuse. Logs and a JSON receipt are retained;
the generated credential is never printed or stored. All owned containers and
volumes are removed in `finally` and absence is read back. No broad prune runs.
Failed helper output is diagnostic only and contains generated data.

Stop the single server gracefully before replacing its image. No second writer,
automatic migration or deployment is provided. Follow normal backup/copy-proof,
writer-fencing, version/listener ownership and authorization gates. Once new
writes exist, restarting an older database would lose history; preserve both
states and use an approved compatible rollback/forward repair. Packaging proof
does not authorize real data access, credentials, migration, VM setup, SSH,
cutover, registry publication or live deployment.
