---
version: alpha
name: Finn Nancy
description: A local-first financial workspace. Blue records facts; orange marks interpretation.
colors:
  primary: "#52a8ff"
  secondary: "#ff8a3d"
  neutral: "#000000"
  text: "#f5f5f5"
  muted: "#a3a3a3"
  panel: "#0a0a0a"
  inset: "#1a1a1a"
  border: "#333333"
  control-line: "#797979"
  danger: "#ff6b6b"
  on-finn: "#000000"
  focus: "#52a8ff"
  light-canvas: "#ffffff"
  light-text: "#171717"
  light-muted: "#595959"
  light-panel: "#fafafa"
  light-inset: "#f2f2f2"
  light-border: "#d4d4d4"
  light-control-line: "#7c7c7c"
  light-facts: "#0064d9"
  light-interpretation: "#bc4700"
  light-danger: "#b42323"
  light-on-finn: "#ffffff"
  light-focus: "#0064d9"
typography:
  heading:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: 1.75rem
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.035em"
  section:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: 1.125rem
    fontWeight: 650
    lineHeight: 1.4
  body-md:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: 0.9375rem
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: 0.8125rem
    fontWeight: 500
    lineHeight: 1.5
rounded:
  sm: 6px
  md: 6px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  xxl: 32px
  xxxl: 48px
components:
  bookkeeping-label-dark:
    backgroundColor: "{colors.inset}"
    textColor: "{colors.primary}"
    typography: "{typography.body-md}"
  interpretation-label-dark:
    backgroundColor: "{colors.inset}"
    textColor: "{colors.secondary}"
    typography: "{typography.body-md}"
  bookkeeping-label-light:
    backgroundColor: "{colors.light-inset}"
    textColor: "{colors.light-facts}"
    typography: "{typography.body-md}"
  interpretation-label-light:
    backgroundColor: "{colors.light-inset}"
    textColor: "{colors.light-interpretation}"
    typography: "{typography.body-md}"
  body-dark:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.text}"
    typography: "{typography.body-md}"
  body-light:
    backgroundColor: "{colors.light-canvas}"
    textColor: "{colors.light-text}"
    typography: "{typography.body-md}"
  muted-dark:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.muted}"
  muted-light:
    backgroundColor: "{colors.light-panel}"
    textColor: "{colors.light-muted}"
  button-primary-dark:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-finn}"
    rounded: "{rounded.sm}"
    height: 48px
  button-primary-light:
    backgroundColor: "{colors.light-facts}"
    textColor: "{colors.light-on-finn}"
    rounded: "{rounded.sm}"
    height: 48px
  error-dark:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.danger}"
  error-light:
    backgroundColor: "{colors.light-panel}"
    textColor: "{colors.light-danger}"
---

## Overview

Finn Nancy is a local-first household financial workspace. Home, Activity, capture,
evidence/reconciliation, close/reports, banking, management and Nancy's planning
pages share the same application foundation. Sandbox remains a separate transport
and explicitly identified data context, not the scope of this design system.
Imported observations are not booked transactions.

The runtime authority is `app/web/static/foundation/tokens.json`, with explicit
`global`, `light` and `dark` groups. Its generated `tokens.css` and unchanged
Bootstrap 5.3.8 CSS load before the shared `components.css` adapter in
`foundation_base.html`. Feature sheets compose those primitives; they must not
create competing palettes. `base.html` is only an inheritance compatibility entry.
There is no runtime `app.css`, CDN, external font or Bootstrap JavaScript dependency.
Native HTML and existing HTMX/feature scripts own interaction.

This file is a portable description of those runtime tokens, not another theme
engine. The root `tokens.json` (DTCG) and `tailwind.theme.json` (Tailwind v3) are
exports of this file, not runtime inputs. CLI 0.4.0 exports typography sizes,
weights and font families but omits `lineHeight`; the source spec and runtime
adapter retain that contract. Their historical color names map to
foundation roles: primary/facts = finn, secondary/interpretation = nancy,
neutral/canvas = canvas, text = ink, panel = surface, inset = hover, border = line.
`tests/test_design_tokens.py` enforces that mapping in both themes, including type,
spacing, control size, focus and control boundaries.

Regenerate the portable exports with the existing `@google/design.md` 0.4.0 CLI:

```sh
npx --offline @google/design.md lint DESIGN.md
npx --offline @google/design.md export --format dtcg DESIGN.md > tokens.json
npx --offline @google/design.md export --format json-tailwind DESIGN.md > tailwind.theme.json
```

The foundation snapshot and Bootstrap hashes are pinned in
`app/web/static/foundation/SOURCE.json`. The snapshot's upstream
`build_reference.py` JSON-to-CSS adapter is not installed in this repository.
Do not claim a local regeneration command or edit generated runtime CSS by hand.
A token change must update the canonical JSON, regenerate with the upstream
adapter, review the pinned hashes and reconcile this specification/exports.

## Colors

Use a black dark canvas, white light canvas and neutral gray surfaces. No navy,
gradients, glass blur, glows or floating card shadows. Blue identifies bookkeeping
actions and recorded facts. Orange identifies categories, budgets, planning and
interpretation. Orange is not an expense sign or a generic warning. Signed money
is neutral unless validated financial meaning justifies emphasis. A positive sign
alone never means income.

Use theme-specific accents on the actual canvas, surface and hover backgrounds.
Normal text, including muted/semantic text, must meet 4.5:1. Control boundaries and
visible focus must meet 3:1 against adjacent surfaces. Separator lines are not
control boundaries. Danger has its own explicit label and role; color alone does
not communicate error or approval. The primary button uses `on-finn`, not a
hard-coded white foreground in dark mode.

## Typography

Use the installed system stack. Runtime sizes are `.9375rem` body, `.8125rem`
metadata, `1.125rem` section heading and `1.75rem` destination heading, equivalent
to 15/13/18/28px at the default 16px root. Body line height is 1.5. Headings use
600–650 weight, ordinary labels 500. Do not compensate for enlarged text by
shrinking fonts. Amounts use tabular lining numerals; preserve sign, currency and
all cents, allowing grouping-aware line breaks for extreme values rather than
ellipsis or rounding. Unknown currency stays unknown.

## Layout

The shared shell is at most 1200px wide, with 32px minimum side gutters at 768px
and above, 16px below. The header is in normal flow, at least 80px on desktop;
phone navigation wraps onto a second row. Page headings, forms, toolbars, lists
and section separators share one start edge. Heading spacing is 32px top/24px
bottom on desktop, 24px top on phone. Use the 4/8/12/16/24/32/48px spacing scale.
Editors are at most 640px; ordinary prose is at most 65ch.

Labels, prose, empty/error messages and forms start-align. Amount columns end-align.
Center only the outer shell, labels within their control targets, vertically aligned
row groups, or the outer box of an actual blocking dialog. Never center ordinary
page copy because space is available. Controls and summaries have a 48px minimum
target, not a fixed height. Rows grow with content. Reflow long labels and actions;
never hide money or consequential warnings to satisfy a density target.

## Elevation & Depth

Use one-pixel separators and restrained surface changes. Evidence is attached to
its row. No decorative elevation. The shared visible focus is an opaque 3px
`--focus` outline with 3px offset. Do not override it with translucent outlines.
Normal-flow navigation must not obscure evidence anchors. Motion is limited to
100ms feedback; reduced motion sets durations to zero and disables smooth scrolling.

## Shapes

Use the canonical 6px corner for controls and containers. Prefer continuous rows
over stacked decorative cards. Status needs text, not a different pill for every
fact. Checkboxes have a 20px visible box inside a labelled 48px target.

## Components

Use Bootstrap `.btn` variants, `.form-control`, `.form-select`, `.form-label`,
`.form-check-input` and `.table`, adapted by `components.css`. Reuse `fn-*` shell,
page-header, toolbar, action, disclosure, state and table recipes and existing
Jinja macros. Feature sheets own domain composition, not global primitive resets.
Persistent labels belong above fields, helper/error text below the affected field.
Use native disabled/required/invalid semantics and keep draft values on failed saves.

Activity uses native disclosure, not an ARIA grid. Home has one real Home heading,
period-scoped Money in, Money out and Net links, and a visible warning/link for
transactions excluded because meaning or provenance is incomplete. Do not invent
categories, insights or balances to fill a layout. Reports link to contributing
rows and keep blocked/frozen/unsupported-currency states distinguishable.

Evidence tables scroll locally in named keyboard-focusable regions, not at the
page level. The PDF lens preserves normalized bounding-box coordinates and a
readable source/row relationship. Preserve source links, revision/fingerprint
fields, confirmations, approval gates and undo authority through presentation
changes. Statement completeness is not inferred from a successful bank sync.

Appearance follows the system by default with explicit Light/Dark choices.
Its non-financial preference is independent of capture/transaction/session storage.
Processing owns durable device/server progress. Notifications are transient
feedback, never authority for Saved. Pending originals stay device-owned until
committed server acknowledgement; Saved does not mean parsed, approved or closed.

Product copy is professional sentence case. One heading per destination. Buttons
name an action; labels identify a field. Keep exact counts, dates/currencies,
unknowns, Sandbox/Not booked context, consent, incomplete totals and uncertain
outcomes. Put technical provenance behind deliberate disclosure, not essential
uncertainty. A failed read is not an empty account, and an uncertain write is not
a definite success or failure. See [docs/UI_DESIGN.md](docs/UI_DESIGN.md) for
vocabulary, route ownership and verification boundaries.

## Do's and Don'ts

- Preserve signed integer cents, validated flow meaning, approved splits and evidence.
- Keep review, reconciliation, approvals, retry, close guards and recovery reachable.
- Exercise real routes, forms, HTMX fragments and browser states, not only token strings.
- Test both themes, keyboard focus, local scrolling, long names and enlarged text.
- Do not add fabricated data, fake controls, another component framework or palette.
- Treat inheritance coverage as structural adoption, not proof that every state was accepted.
