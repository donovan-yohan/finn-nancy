# finn-nancy

`finn-nancy` is an experimental, local-first personal-finance application. Finn covers bookkeeping; Nancy covers planning. It imports receipts and statements, supports reconciliation and budgets, and can use optional assistant features.

It is not a hosted service, a bank integration, or a security boundary for a shared network. The web UI is intentionally unauthenticated: run it only on loopback, or behind an authenticated private overlay that you administer. Do not expose it to a LAN, public reverse proxy, or the Internet.

## Privacy and network use

Local storage and deterministic bookkeeping stay on the machine running the app. Some optional features can send data outside that machine:

- An OpenAI-compatible LLM endpoint receives prompts and document-derived content when configured. That endpoint may be self-hosted or remote.
- Merchant research is disabled by the default configuration. When explicitly enabled and consented to, it sends a sanitized merchant query to the configured search provider. The sanitizer rejects amounts, dates, account/card identifiers, and known private terms, but it is not a guarantee that a query is non-sensitive.
- Telegram capture is disabled by default. When explicitly enabled and consented to, submitted content is sent through Telegram.

Use `STRICT_LOCAL_MODE=true` to block the optional merchant-research and Telegram egress paths. Review the endpoint, provider policy, and retention posture before opting in.

## Quick start

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --group dev
cp .env.example .env
uv run fn seed-sample --db data/sample.sqlite
bash deploy/dev.sh
```

Open the loopback address printed by the development helper. The sample database contains only records marked as synthetic fixtures.

## Commands

```bash
uv run fn init-db --db data/local.sqlite
uv run fn migrate --db data/local.sqlite
uv run fn seed-sample --db data/sample.sqlite
uv run fn serve --db data/sample.sqlite --addr 127.0.0.1:8080 --read-only
scripts/public-release-audit
scripts/lint
scripts/test
```

`--read-only` opens SQLite with `PRAGMA query_only=ON` for a browse-only session.

## Deployment boundary

Keep the application bound to `127.0.0.1` or `::1`. If remote access is necessary, place it behind an authenticated private-overlay service with restrictive access controls; verify that the overlay does not provide a public-sharing or funnel feature. The included deployment examples bind locally and do not configure public exposure. No NGINX/LAN reverse-proxy sample is supplied because it would be unsafe for an unauthenticated app.

## Repository layout

- `app/` — FastAPI application, SQLite access, ingestion, reconciliation, and optional integrations.
- `migrations/` — ordered SQLite migrations.
- `fixtures/` — deterministic synthetic seed data.
- `tests/` — offline tests and synthetic evaluation data.
- `docs/` — architecture, fixture, and public-release guidance.
- `scripts/public-release-audit` — deterministic source-release gate.

## Status and limitations

This is an early-stage project. Imported data, generated classifications, reconciliation proposals, reports, and assistant output require review. The code has no app-level user authentication and has not been independently security audited.

See [docs/DESIGN.md](docs/DESIGN.md), [SECURITY.md](SECURITY.md), and [docs/PUBLIC_RELEASE.md](docs/PUBLIC_RELEASE.md).

## License

Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE), and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
