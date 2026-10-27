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

## Manual GHCR distribution (no deployment)

Merge/review the release workflow through a PR first; do not dispatch a branch
version. The `release image` Actions workflow is **workflow_dispatch only**.
Select `main` and supply `source_sha` as its full lowercase 40-character SHA.
A branch dispatch or different workflow revision is skipped before helper execution;
no skipped run qualifies a release. A mismatched source on a trusted main dispatch
reaches the read-only helper and fails with a refusal receipt before build or login.
Both jobs require clean `HEAD == origin/main == current GitHub
main`, a merged main PR whose final head/tree matches the dispatch attestation,
and the latest exact-source `ci.yml` **push/main** run completed with `success`.
PR checks, generic combined status and an older successful attempt do not qualify.
The CI query relies on GitHub's newest-first workflow-run listing, filtered by
exact SHA, main and push; the newest run/attempt in that first page is re-read.
Do not rerun only a failed publication job after main advances: dispatch again
for the newly reviewed, CI-qualified current source.

Dispatch also requires `review_receipt_sha256` (64 lowercase hexadecimal digits),
`reviewed_pr_head` and `reviewed_pr_tree` (40 lowercase hexadecimal digits each).
The authorized dispatcher must first retain a **real independent PASS review
receipt**, verify its final PR head and Git tree, resolve outstanding findings
under source review/merge policy, and compute the SHA-256 of those exact receipt
bytes in protected evidence custody. Supply that digest and the verified head/tree;
never invent a receipt or a GitHub approval. The helper checks the referenced PR
is merged into the selected main commit, its final head equals the attested head,
and that head's API Git tree equals both the attested tree and selected main tree.
A squash commit can have a different SHA from its reviewed PR head; its tree must
still match. After source gates pass, both phases record receipt digest, PR/head/tree,
actor, run and attempt; publication requires the same attestation as qualification.
An early source-gate refusal records its phase/error but may omit that attestation.

**Trust boundary:** the digest references an operator-verified independent PASS;
it does not independently establish reviewer authenticity, receipt contents or
review quality. Hosted publication trusts the authorized dispatcher's verification.
It does not fetch private receipts or infer approval from a GitHub review list.
`CHANGES_REQUESTED`, dismissals and other review findings remain source review and
merge-policy obligations, not an invented approver requirement. Neither CI nor
this attestation waives them.

Two hosted `ubuntu-24.04` native amd64 jobs keep `packages: write` out of the
build/smoke job. A single job would be shorter but its job token would already
have package-write authority during source execution. Qualification calls the
existing allowlisted `build-image` and `smoke-image` by actual image ID, with no
package credential in their environments; it never reads operator state.
A one-day same-run artifact transfers the Docker archive, source/tree/image ID,
archive hash and successful smoke/cleanup receipt. The publisher rechecks the
source/review/CI gates and archive/receipt hashes before loading. It does not
execute the image. Only that job has package-write permission, using the built-in
GitHub token, an isolated temporary Docker config and password stdin. No PAT,
new repository secret, local `gh` package scope or registry login action is used.

Publication creates a unique `sha-SOURCE-amd64-RUN-ATTEMPT` tag in
`ghcr.io/OWNER/REPOSITORY`; it never uses or updates `latest`. The push digest is
checked against the registry tag, its digest-addressed manifest config must
equal the qualified image ID, and the pulled digest must preserve the source/tree
labels, architecture and image ID. Consume **`ghcr.io/OWNER/REPOSITORY@sha256:...`**
from `publication.json`, not the tag. The helper logs out even after a failed
publication and removes its temporary auth directory. A push followed by failed
readback can leave a registry tag; `success: false` is not release acceptance and
no automatic deletion, rollback or retry is attempted.

Every helper invocation with a writable artifact destination leaves a terminal
`qualify-phase.json` or `publish-phase.json`, including source-gate, build,
artifact-validation and pre-login refusals. Publication also writes
`publication.json` on failure. `qualified.json` remains success-only. Receipts
identify a stable stage/error code and, for child failures, safe command basename
and exit status; arbitrary exception messages, command arguments, credentials and
login input are not echoed. `build.log` retains at most 16 KiB of sanitized
stdout/stderr on success **or failure**, with known workflow credentials and common
credential syntax redacted before truncation. This is generated build diagnostic
evidence, not a general-purpose secret detector; private inputs remain forbidden.
An unwritable destination or external runner termination can prevent receipts;
a missing receipt never implies success.

`push_state` distinguishes `push_not_attempted`, `attempted_uncertain`, `completed`
and `completed_readback_verified`. A nonzero or timed-out push may already have
written to the registry: it remains uncertain and is **never automatically
replayed**. Separate `push_completed`, `readback_verified`, `logout_verified` and
`auth_directory_removed` predicates must not be conflated with overall success.
Cleanup errors are retained alongside an earlier primary failure. Digest parsing
uses only a dedicated bounded push stdout/stderr result; neither stream is broadly
logged. Generated tests cover both streams, not installed native Docker behavior.

The helper caps qualification/publication work at 30/14 minutes within the 35/20
minute jobs, with smaller per-child limits (build 15 minutes, smoke 12, push 5,
load/save/pull 3, ordinary commands 90 seconds, login 60 and logout 30). A timed-out
owned process group is killed before credential cleanup; logout has a separate
bounded allowance. Checkout/artifact transfer/upload consume the remaining job
budget; platform cancellation/termination still cannot guarantee cleanup or a
receipt. A timed-out smoke is not cleanup acceptance for its generated resources.

Actions retains generated smoke/qualification and registry readback receipts for
14 days (archive transfer: one day). Download accepted receipts into the operator's
protected evidence custody before expiry. These are integrity/provenance receipts,
not signed attestations, reproducible-build claims, package-visibility changes or
deployment proof. GHCR package ACL/organization policy may still deny the first
push; handle that as an operator publication blocker, not permission to add a
long-lived token. Existing repository review/branch protection must govern changes
to this workflow and helper; self-checks are not a sandbox against a writer who
can replace an authorized workflow. Nothing here provisions a VM, accesses private
state, migrates a real database, changes ingress or starts a live service.
