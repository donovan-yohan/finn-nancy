# Self-host getting started

Start with an empty ledger on your own machine. Bank connections and assistant services are optional.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/).
- A local checkout of this repository at a reviewed revision, including `uv.lock` and `migrations/`.
- A private, writable state directory outside the checkout. The commands below use a POSIX shell on macOS or Linux.

The UI has no app-level authentication. Keep the server on loopback. For a phone or another computer, use an authenticated private overlay with restrictive access controls and HTTPS; do not enable public sharing, a public tunnel, or LAN access.

## Install and initialize

In the repository root:

```sh
uv sync --frozen --no-dev
export FN_SOURCE="$PWD"
export FN="$FN_SOURCE/.venv/bin/fn"
```

Keep the checkout: this is an editable source installation, and migrations are read from it. Dependency installation may need network access; `--frozen` uses the committed lock without updating it.

In the same shell, choose your state directory and explicit local-only settings:

```sh
umask 077
export DATA_DIR="$HOME/.local/share/finn-nancy"
mkdir -p "$DATA_DIR"
export DB_PATH="$DATA_DIR/ledger.sqlite"
export STRICT_LOCAL_MODE=true
export PLAID_ENABLED=false
export MERCHANT_SEARCH_ENABLED=false
export EMBEDDINGS_ENABLED=false
export LOCAL_LLM_BASE=http://127.0.0.1:9/v1
export LLM_TIMEOUT_S=1
export LLM_MAX_RETRIES=0
cd "$DATA_DIR"
"$FN" init-db
"$FN" migrate
```

Use a new state directory for first use, not an existing household installation. `init-db` applies schema migrations; it does not load sample transactions. The second command should report `none (up to date)`. No active household members are invented for you. Do **not** run `seed-sample` against a real ledger: it rebuilds a synthetic demonstration database.

These commands use environment variables rather than copying a sample configuration. Settings also load a `.env` from the **current working directory**; use a fresh directory without one and a shell without inherited integration settings for this first run. Keep credentials and operator configuration outside Git. Retain these exports privately for subsequent sessions; do not commit them with secrets.

## Run and check

```sh
"$FN" serve --addr 127.0.0.1:8080
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080). From another terminal:

```sh
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/version
```

`/healthz` returns `ok` for basic database liveness. `/version` reports the loaded revision, database identity, schema and migration evidence; check `migration_status` is `ok` and the sequence matches. Neither endpoint proves that your financial records are complete.

Stop the foreground server with Ctrl-C. It starts its ingestion worker in-process; do not add a second worker or server against the same ledger. `--read-only` makes database access query-only, but still starts the ordinary server lifespan; it is not a consumer-free restore-inspection command.

## First useful session

1. Open Connections and add yourself and any other household members. This stays local; adding a person does not connect a bank.
2. In Manage, add the accounts you intend to track and configure their statement expectations and opening balances where applicable.
3. Add a manual transaction, or use structured statement import for supported CSV/OFX input. Check the preview and account before confirming.
4. For receipts and PDF extraction, configure a working model first. Upload originals, check Processing, then review and reconcile staged statement rows.
5. Review a period statement before attempting a close. Missing statements, ambiguous flow, or unresolved evidence need a decision, not a guessed balance.

See [common-flow walkthroughs](DEMO_WALKTHROUGHS.md) and [statement expectations](STATEMENT_EXPECTATIONS.md).

## Configuration

| Setting | Purpose |
| --- | --- |
| `DB_PATH`, `DATA_DIR` | Absolute ledger path and persistent state root, including originals and backups |
| `HOME_CURRENCY`, `HOME_LOCALE` | Household defaults; the shipped currency default is CAD. Set these deliberately before importing |
| `LOCAL_LLM_BASE`, `CHAT_MODEL` | Your OpenAI-compatible model endpoint and served model name |
| `LOCAL_LLM_KEY` | Optional model credential; supply through private operator configuration |
| `EMBEDDINGS_ENABLED`, `EMBED_MODEL` | Optional semantic retrieval and its embedding model |
| `STRICT_LOCAL_MODE` | Fail-closed gate for optional Plaid, merchant-search, and Telegram traffic |
| `PLAID_ENABLED` | Separate bank-connection opt-in; see [Plaid setup](PLAID_SETUP.md) |
| `API_TOKEN` | Shared bearer key for programmatic clients, not a login for the web UI |

The initial `127.0.0.1:9` model address is deliberately unavailable: it permits startup without contacting an external model, but cannot extract documents or answer financial questions. The server tries a small model warm-up on startup. Before model-dependent work, set `LOCAL_LLM_BASE` to your actual local inference endpoint and `CHAT_MODEL` to its served model, then restart. A remote endpoint receives prompts and document-derived content even in strict-local mode.

Leave `INBOX_DIR`, Telegram credentials, and external chat settings unset unless you intend to use them. Merchant research requires separate provider configuration and consent; sanitized searches are still external disclosure. Enabling Plaid requires turning strict-local mode off, so explicitly keep unrelated integrations disabled. See [Hermes chat](HERMES_CHAT.md) and [banking contract](BANKING.md) for integration details.

## Back up and update

`"$FN" backup` creates a WAL-safe SQLite snapshot in `DATA_DIR/backups/`. It is **not** a complete workspace backup: preserve originals, any checkpoint state, matching private bank-token storage, and operator configuration as well. Keep an independent protected copy, not only a backup on the same disk. Follow [household recovery](HOUSEHOLD_RECOVERY.md) for a coherent capture boundary and verified database manifests; do not copy a live SQLite file while ignoring WAL.

Before updating: stop all writers and consumers, retain the old release and complete recovery set, and rehearse migrations on an isolated copy. In the reviewed new source checkout, rerun `uv sync --frozen --no-dev` and refresh `FN_SOURCE`/`FN`. From the private state directory with the intended configuration, run `"$FN" migrate`, start the single server, and check `/healthz` and `/version`. Never seed during an update. Once new writes exist, restoring an older database would discard them; do not treat source rollback as database rollback. See [delivery policy](DELIVERY.md).

## Prefer a container?

Use the [immutable container guide](CONTAINER.md), not a checkout-wide `docker build .`. Its entrypoint refuses missing or unmigrated state and accepts no maintenance arguments: it does **not** initialize or migrate for you. Prepare state with the matching source revision, mount the whole state root with the documented ownership, and publish only to `127.0.0.1`. Container packaging does not replace the backup, private-ingress, or single-writer requirements above.
