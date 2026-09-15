# Delivery and release policy

This repository contains product code and environment-neutral examples. Keep operator-specific deployment state, hostnames, paths, identities, databases, statements, receipts, credentials, and rollback records outside Git.

## Change gates

Run the canonical checks against the exact revision being proposed:

```bash
scripts/public-release-audit
scripts/agentic-harness
scripts/lint
scripts/test
scripts/build
scripts/smoke
```

Run the reconciliation evaluation commands when their scoring, corpus, policy, or knowledge contracts change. Use generated temporary databases for tests and smokes. Never point a test or acceptance command at an operator database.

## Review evidence

A change is ready only when its focused tests and risk-appropriate acceptance evidence pass at the reviewed head. Record the revision, commands, exit status, pass/skip counts, artifacts, migration impact, rollback, and unresolved caveats. Security, persistence, protocol, and runtime changes require current-head evidence; an older green result is not sufficient.

## Deployment boundary

The application UI has no app-level authentication. Supported deployments bind to loopback. Remote access requires an authenticated, access-controlled private overlay that does not provide public sharing. Do not expose the app directly to a LAN, public reverse proxy, tunnel, or the Internet.

Before changing a real deployment, create and verify a WAL-safe backup manifest, prove migrations and smoke behavior on a copy, and verify `/version` against the intended revision. `scripts/release-audit` is read-only and validates listener ownership and release evidence; it does not authorize a rollout.

## Public-source release

Follow `docs/PUBLIC_RELEASE.md`. Publish only from a reviewed, history-free candidate when prior history contains private operational or financial context. Apache-2.0 licensing applies to the project; preserve third-party notices and run an independent secret/privacy scan in addition to the repository gate.
