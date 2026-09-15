# Public-release checklist

Run these offline checks from the release candidate:

```bash
scripts/public-release-audit
scripts/lint
scripts/test
scripts/agentic-harness
scripts/reconciliation-eval
scripts/descriptor-resolution-eval
scripts/build
scripts/smoke
```

The public-release audit rejects sensitive file extensions, non-placeholder email addresses, personal absolute paths, private IPs and hostnames, private-overlay domains, malformed synthetic-person placeholders, credential-shaped values, populated secret assignments, unreviewed structured finance fixtures, and unreviewed opaque binaries. In a Git checkout it scans cached and non-ignored untracked files. It is a guardrail, not proof that data is safe.

Before publishing, independently inspect the source tree and all intended Git history for private data, review generated artifacts and dependencies, and verify that deployment documentation does not expose the unauthenticated UI. Do not publish local databases, backups, receipts, statements, `.env` files, or service-specific operational records.
