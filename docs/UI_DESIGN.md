# Application UI design contract

## Foundation and ownership

All 41 current Jinja templates belong to one application-wide foundation, including
secondary destinations and the separate Sandbox app. This is structural adoption,
not a claim that every financial state, device or browser has been accepted.
[DESIGN.md](../DESIGN.md) describes the system and portable exports.

Runtime load order in `foundation_base.html` is local Bootstrap **5.3.8**, generated
`foundation/tokens.css`, shared `foundation/components.css`, then page composition.
The canonical values are `app/web/static/foundation/tokens.json`, grouped into
`global`, `light` and `dark`. `SOURCE.json` pins the imported token/CSS/vendor
snapshot. `base.html` only extends the shared foundation. The retired `app.css`
must not return as a dependency. No CDN, external font or Bootstrap JS is loaded.
Native HTML, HTMX and existing feature scripts retain interaction ownership.

The root `tokens.json` and `tailwind.theme.json` are portable exports from DESIGN.md,
not runtime inputs or a second palette. Regeneration and the upstream runtime
adapter boundary are documented there. Do not hand-edit exporter output or invent
an in-repository runtime generator. Keep the canonical JSON, generated CSS,
portable exports and provenance hashes consistent when changing tokens.

| Composition owner | Full-page templates | Shared/returned fragments |
| --- | --- | --- |
| Shell, `components.css` | `foundation_base`, compatibility `base` | `foundation_macros`, `_tabbar` |
| Activity, `activity.css` | `dashboard`, `activity`, `txn_form` | `transactions_partial` |
| Capture, `capture.css` | `upload`, `processing`, `structured_import`, `import_run` | `_upload_results`, `_doc_status`, `_import_run_status` |
| Evidence, `evidence.css` | `review`, `recon_review`, `statement_review`, `statement_source` | `_coverage_breakdown` |
| Close/report, `close-report.css` | `close`, `period_statement` | None |
| Nancy, `nancy.css` | `insights`, `categories`, `goals`, `goals_close`, `chat` | None |
| Management, `manage.css` | `manage`, `backlog`, `approvals` | `_backlog_job` |
| Banking, `banking.css` | `banking_feed`, `banking_review`, `connections_notice`, `sandbox_shell`, `connections`, `sandbox_activity`, `sandbox_accounts`, `sandbox_overview`, `sandbox_unavailable` | Sandbox children inherit `sandbox_shell` |
| Household onboarding, `household-onboarding.css` | `banking_connections` | Existing foundation macros |

Names in this table omit `.html`. `/connections` in the normal app renders
`banking_connections`; Sandbox's `/connections` renders `connections`. They share
presentation, not provider authority or data. `connections_notice` is retained
but is not a currently routed destination. A returned fragment belongs to its
host's composition sheet, never to an independent shell or palette.

`tests/test_design_adoption.py` enforces the exact template inventory, inheritance,
fragment hosts, route-rendered template references, shared asset order, real HTTP
route-family shells, no competing canonical variables and retirement of the legacy
asset. New templates require an explicit owner, not merely a matching accent.

## Roles, components and layout

Finn is bookkeeping, recorded facts, capture, reconciliation and close. Use blue.
Nancy is categorization, budgets, goals, analysis and interpretation. Use orange.
Neither accent identifies money direction. Amount signs, warnings and errors retain
their actual financial/interaction meaning; unknown flow never becomes income
because its amount is positive. Keep signed money neutral unless its validated
meaning supports further emphasis.

Use black/white canvases and neutral gray surfaces. No navy, gradients, glow,
glass blur or floating card shadows. Text/semantic foregrounds must reach **4.5:1**
on canvas, surface and hover fills in both themes. Control boundaries and visible
focus must reach **3:1** against adjacent surfaces. Focus is an opaque 3px
`--focus` outline with 3px offset. Do not substitute separator gray for a control
boundary or remove focus to simplify a screen.

Use Bootstrap buttons, fields, native selects, checks and tables through the
shared adapter, not copied page-local primitive systems. Primary action is blue,
alternatives outline, quiet navigation a real link. Keep native disabled/required
semantics, visible labels, contextual validation and confirmations. Reuse `fn-*`
page headers, action rows, toolbars, disclosures, state messages and table wrappers.
Feature CSS owns financial composition and generated feature markup, not global
resets or alternate token definitions.

Body/metadata/section/title sizes are 15/13/18/28px equivalents in rem. Body line
height is 1.5; money uses tabular numerals. The spacing scale is 4/8/12/16/24/32/48px,
canonical corner 6px and target floor 48px. Targets and rows grow with content.
The shell is at most 1200px, with 32px desktop and 16px phone minimum gutters.
Heading spacing is 32px top/24px bottom on desktop, 24px top on phone. Navigation
stays in normal flow. Editors are at most 640px; prose at most 65ch.

Headings, filters, lists and forms share one start edge. Start-align names, labels,
prose and empty/error states; end-align exact numeric columns. Center only the
outer shell, control contents, vertically aligned row groups or an actual dialog's
outer box. Do not center ordinary headings/forms because the page is sparse.
Compactness comes from aligned rows, fewer wrappers and disclosure, not smaller
text, clipped money or concealed warnings. Long labels and actions wrap. Extreme
amounts retain every cent/currency with deliberate break opportunities.

Home has one Home title, period-linked Money in, Money out and Net totals, and a
visible warning/review link when incomplete meaning or provenance excludes rows.
Activity uses native details/summary, not a pretend ARIA grid. Filters, keyset
pagination, expanded evidence and source identities survive HTMX updates. Reports
keep row-linked totals, frozen identity, unsupported-currency and blocked states.

Evidence candidate tables scroll locally inside named keyboard-focusable regions;
the page itself must reflow. The PDF source lens preserves normalized bounding-box
coordinates, row-to-region links and source-image aspect. Keep accessible frame
names, page links, revision/fingerprint/operation fields, approvals and captured
Undo authority. A successful bank read does not prove statement completeness.

## Navigation and capture

The primary destinations are Home, Activity, Upload, Insights and More. Chat,
Goals, Connections, Processing, Statements, Approvals, Monthly close, Reconcile,
Review inbox, Uncategorized, Budget and Manage remain reachable through More.
Sandbox has its own Home, Activity, Accounts and Connections destinations.

Processing has its own `/processing` page. Never mount the device queue over other
pages. A selected upload produces one dismissible notification linking to
Processing. Normal notifications expire after eight seconds with focus/hover
protection; storage errors remain until dismissed. The processing page remains
available offline through the existing service worker.

Presentation does not own capture durability. Device originals remain Pending
until committed server acknowledgement permits Saved. Stable capture IDs, retries,
offline reload recovery and server processing continue when Processing is not open.
A toast is not a durable queue. Suppress an acknowledged local display duplicate
only when its server record is visible; never suppress a device-owned original.
Server history is paginated with unfinished documents first. Retry/review remain
available. Saved does not mean parsed, reconciled, categorized or closed.

Appearance follows the system by default with explicit Light/Dark choices. Its
non-financial cookie is independent of capture, financial and session storage.
Browser chrome follows the selected theme. Reduced motion eliminates transition
and animation duration and automatic smooth scrolling.

## Copy and state

Professional sentence case is the product voice. One heading per destination.
Buttons name an action, labels identify fields, helpers change entry decisions.
Keep labels above fields and errors/recovery beside the affected control. Remove
synonymous eyebrows, duplicate headings, routine enabled-state narration and
implementation commentary. Do not replace them with decorative badges or panels.

| Term | Meaning |
| --- | --- |
| Activity / Transaction | Recorded canonical financial events with validated flow and splits |
| Bank feed | Imported observations, not a statement, booking decision or completeness guarantee |
| Statement / Receipt | Account-period / purchase evidence; receipt plus statement may support one expense |
| Category | Approved split classification; provider output remains a suggestion |
| Review / Approval | A needed human decision / explicit acceptance, not ingestion success |
| Connection / Account | Institution access / financial account; connected-by, ownership and inclusion differ |
| Period | Explicit reporting/close scope, not an inferred complete bank-sync window |
| Pending / Saved | Device-owned capture / committed server acknowledgement; parsing is a separate phase |
| Not booked / Uncategorized | Observation not recorded / no approved category; neither establishes flow meaning |
| Currency unknown | Currency is unestablished; never substitute household currency silently |

Distinguish empty, no results, loading, read error, uncertain write, partial success
and completed phase. A failed read is not “No activity.” An uncertain removal is
not “Disconnected” or permission for automatic replay. Counts use exact singular
or plural, not “transaction(s)”; unavailable totals are not zero. Keep exact dates,
amounts/currencies, unknowns, Sandbox/Not booked, consent, partial totals, destructive
consequences and uncertain recovery visible. Raw identifiers/provenance can use
Source details, but essential uncertainty must precede disclosure.

## Safety and verification

This system does not remove reconciliation, evidence, retry, approvals or close
checks. Reports still read approved splits. Do not change integer-cent arithmetic,
flow classification, immutable originals, consent or closed-period guards for a
visual shortcut. Suggestions stay suggestions. Never invent balances/categories,
a new dashboard, fake sorting/bulk actions or unsupported assistant capability.

Use generated fixtures and temporary databases only. Run the targeted
`test_design_*`, theme, processing, web smoke, reconciliation, statement-review and
source-lens contracts while iterating. Companion Chromium tests exercise rendered
Home/Activity, normalized source geometry, local scrolling, keyboard decisions and
focus rather than treating CSS text as pixel evidence. Real route assertions do
not certify every adverse state. Full delivery requires the canonical gates and
risk-appropriate `scripts/smoke-capture-browser`, composed browser checks and visual
review in both themes. Record which checks actually ran; inheritance is not
full-app acceptance, deployment approval or physical-device evidence.
