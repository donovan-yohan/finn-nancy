# Household-first Plaid onboarding

## Outcome and authority

Implement the user's requested Connections cleanup using Hotate's Finn Nancy component direction. A new installation starts with no invented people. Users add themselves and other household members locally, then optionally connect banks for a selected named person. Each connection and account remains recognizably associated with that member. Shared economic ownership and household inclusion remain explicit account decisions, not inferred from who connected Plaid.

Authorized: isolated local application implementation, additive migration, generated tests/browser verification, reusable design foundation adoption scoped to Connections. Not authorized: commits, pushes, deployment, real financial/provider/credential access, enabling production egress, rewriting the frontend, individual logins/invites or destructive cleanup of referenced identities.

## Existing contract and chosen approach

The canonical household_members table already exists. Reuse it and its IDs across budgets, connection consent, bank_connections and personal account ownership. Do not add another people database or make membership depend on Plaid. Use the existing /connections destination with member-first sections, an empty household action, Add member, and a per-member Add bank action. Do not create a separate onboarding wizard/router or store a duplicate onboarding step.

Alternative considered: separate Household settings route with a multi-step Plaid wizard. Rejected for this slice because it adds navigation/state ownership while all necessary member/connection operations already exist. The implementation should derive readiness from authoritative settings/member/connection state.

## Source and ownership

Base 35125e98d0fa647274ee6c49a03391aa1f85a81f in isolated feat/household-plaid-onboarding. Keep primary main clean. Existing design direction is released; a new Hotate onboarding reference is being authored separately and does not own application files. Ebi integrates and verifies the production candidate; independent review precedes full gates.

## Required shape

- Member view model: stable id, actual user-entered name, source and explicit needs-name state if a legacy placeholder with references must be retained.
- Readiness view model: configured/disabled/strict-local/read-only/ready with a concrete next step and no credential/path values. No secret file read for overview. Provider permission remains enforced by existing service guards.
- Household-first screen: consistent page/section edges, persistent Name label, concise add-self/family guidance, optional Plaid connections under each member, empty/member/no-account/loading/error/recovery states.
- Member name correction: validated local write using existing member ID; no financial history, consent or account reassignment. Duplicate/blank/control-character names give actionable errors. Referenced old seed identities must not be deleted or reassigned automatically.
- Clean database: additive migration removes/deactivates only exact unused shipped seed identities. Historical migrations are not edited. Seed names are confined to explicit generated test/demo fixtures, not real first use. Referenced placeholders require a visible naming action without showing fake people as real users or silently assigning their ownership.
- Account editor: compact saved summary and explicit edit; ownership and household inclusion visible; dirty-only Save changes/Cancel. Unmapped accounts require an explicit personal/shared choice. A joint account links to an existing ledger account intentionally; retain primary/secondary policy with understandable labels and brief contextual help.
- Consent: current version, per-member, explicit before external Plaid; local member creation never launches Link. Preserve exact server-verified Hosted Link/session protocol, CSRF/origin, strict-local/read-only and lifecycle/uncertainty guards.
- Async UI: real input/button disabling through current operation and authoritative refresh; preserve unrelated drafts; no stale re-enabled mutations after failed refresh; errors at affected operation; no false empty or success.

## Design foundation

Reuse local Bootstrap 5.3.8, canonical tokens and concrete layout/copy contracts from the released Hotate package. Scope adoption to the Connections surface, avoid global Bootstrap reset breaking old pages, preserve the existing shell navigation unless deliberately integrated. No CDN/fonts/logos. Blue facts/actions, orange interpretation, black/white neutral themes. No arbitrary centering, explanatory card stacks or repeated safety narration.

## Verification

TDD vertically through real repo/service/router interfaces. Fresh DB, legacy DB before migration and copied generated DB migration/replay tests; used seed preservation; named people/consent/connection IDs remain stable; no external I/O. Browser via real create_app and generated MockTransport: no-members → two named people → independent consent/Hosted Link → account mapping personal/shared → existing sync/recovery, plus disabled/unconfigured/read-only and validation. Cover 320/390/768/1440 both themes and enlarged text, pointer/keyboard, labels/focus, targets, no overflow, request counts and persisted readback. Never test against operator state.

Run focused tests while iterating, then one independent broad review, a consolidated repair if needed, focused re-review, and canonical public-release-audit, agentic-harness, lint, full tests, build, smoke and relevant browser gates on frozen final source. Maximum three repair cycles for a still-failing acceptance predicate, preserving earlier failures. Deliver working code and real execution results; no deployment claim.
