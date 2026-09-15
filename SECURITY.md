# Security policy

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](/security/advisories/new) for this repository. Do not open a public issue with exploit details, financial data, credentials, or a proof against a live deployment.

Include the affected revision, a minimal synthetic reproduction, impact, and any mitigation you have validated. Maintainers will acknowledge reports through the private advisory workflow and coordinate disclosure there.

## Security model

This application is designed for a single operator or a tightly controlled private environment.

- The browser UI is **unauthenticated**. Anyone who can reach it can view or alter its data within the application's capabilities.
- The supported network posture is loopback-only. Remote use requires an authenticated, access-controlled private overlay. Public exposure, LAN exposure, public tunnels, and unauthenticated reverse proxies are unsupported.
- SQLite databases and uploaded originals may contain highly sensitive financial information. Keep their paths outside version control, use host-level permissions and encryption appropriate to your threat model, and back them up securely.
- Ingestion, merchant resolution, and assistant output are untrusted inputs or suggestions. Review proposed writes and treat document text as potentially malicious.
- Optional LLM, merchant-research, and Telegram integrations can create egress. They require configuration or consent; `STRICT_LOCAL_MODE=true` blocks merchant research and Telegram, but does not disable a configured LLM endpoint.
- API tokens protect only the programmatic API; they do not add authentication to the web UI.

## Scope and limits

The project has not received an independent security audit. It does not promise protection against a compromised host, browser, private-overlay account, configured external provider, or a user with network access to an exposed service.
