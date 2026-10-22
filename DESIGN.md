---
version: alpha
name: Finn Nancy
description: A compact financial workspace. Blue records facts; orange marks interpretation.
colors:
  primary: "#52a8ff"
  secondary: "#ff8a3d"
  neutral: "#000000"
  text: "#f5f5f5"
  muted: "#a3a3a3"
  panel: "#0a0a0a"
  inset: "#141414"
  border: "#333333"
  light-canvas: "#ffffff"
  light-text: "#171717"
  light-muted: "#595959"
  light-panel: "#fafafa"
  light-inset: "#f5f5f5"
  light-border: "#d4d4d4"
  light-facts: "#0064d9"
  light-interpretation: "#bc4700"
typography:
  heading:
    fontFamily: system-ui
    fontSize: 1.5rem
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  body-md:
    fontFamily: system-ui
    fontSize: 0.875rem
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: system-ui
    fontSize: 0.75rem
    fontWeight: 500
    lineHeight: 1.4
rounded:
  sm: 6px
  md: 8px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
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
---

## Overview

Finn Nancy is a financial workspace, not a dashboard demonstration. Put transactions and the next useful action ahead of commentary. One page heading is enough. Use a small vocabulary of surfaces, controls and spacing across bookkeeping and planning.

The current implementation is an isolated Plaid Sandbox candidate. Its imported rows are not ledger transactions. This design must not make them appear reconciled, categorized or booked.

## Colors

Use a true black page in dark mode and a white page in light mode. Panels and separators are neutral gray, never navy. There are no gradients, glass effects, glows or floating card shadows.

Blue identifies bookkeeping actions and recorded facts. Orange identifies categories, budgets, insights, estimates and interpretation. Orange does not mean expense. A positive sign alone does not mean income. A negative sign alone does not mean spending. Keep signed amounts neutral unless a validated accounting meaning supports further emphasis.

Use theme-specific foregrounds rather than one low-contrast accent everywhere. Dark blue `#52a8ff` and orange `#ff8a3d` contrast with black at 8.40:1 and 8.95:1. Light blue `#0064d9` and orange `#bc4700` contrast with white at 5.49:1 and 5.20:1. The brighter dark-mode orange may be used as a fill with black text, not as small orange text on white. Validate foregrounds on actual panel/tinted backgrounds too. Errors and destructive actions have their own explicit semantics.

The existing CSS variables `--finn` and `--nancy` remain the implementation names for these functional roles. The application stylesheet is the runtime source of truth; this file defines intent and the core token values, not a separate theme engine.

## Typography

Use the installed system sans-serif stack. No external font request is required. Body UI text is approximately 14px, secondary text 12px, and page headings 24px. Headings and merchant names use moderate weight. Avoid excessive uppercase, widely tracked micro-labels and oversized headline numbers. Monetary values use tabular numerals and retain sign, precision and currency.

## Layout

Use the 4/8/12/16/24px spacing scale. Reduce nesting and repeated labels before reducing text size. Desktop rows align description, account/connection owner, date, source status and amount. Mobile uses the same transaction DOM with a compact two-line summary and full-width disclosure. Ordinary interactive rows remain at least 44px high. Do not squeeze long or unknown values into clipped cells to meet a density target.

Keep search and the filter disclosure in one compact toolbar. Less common filters expand below it. The result count states the current range and exact total. Pagination preserves the selected filters. Do not build a saved-view or bulk-action feature merely because the reference application has one.

## Elevation & Depth

Use one-pixel separators and restrained surface differences. Depth is structural, not decorative. Focus rings remain obvious in both themes. Expanded evidence is visually attached to its row.

## Shapes

Controls use 6px corners and containers use at most 8px corners. Prefer a continuous transaction list over a stack of mobile cards. Status labels are compact text or subtle markers, not a different pill for every fact.

## Components

### Transaction rows

Activity uses native disclosure controls. Home uses links to account-scoped activity. Do not pretend the disclosure list is an ARIA data grid. Desktop visual column headings help scanning; each row retains a useful native accessible name.

Collapsed rows show merchant, signed amount/currency, date, account and enough connection ownership to distinguish identical account names. Source status such as pending or removed remains inspectable. Excluded accounts are not hidden by default. Expanded evidence preserves complete merchant text, unknown accounting meaning, not-booked state and nested raw provenance. Provider identifiers stay out of the default scan view.

The API payload, integer-cent arithmetic, currency treatment and row identity do not change with the layout. No category, budget, balance or insight is invented to fill an empty column.

### Appearance

Follow the operating system by default, with explicit Light and Dark choices. Any retained theme preference is non-financial and independent of transaction, session and capture data. Browser chrome follows the selected theme.

### Labels and copy

A visible label must identify a control or field, distinguish otherwise ambiguous information, communicate an actionable state, or prevent a consequential misunderstanding. If removing it changes none of those, remove it.

Remove repeated page/section headings, generic eyebrows, routine enabled-state narration and implementation commentary. Show loading/error text only while it is true and useful. Keep accessible control names, exact counts, dates/currencies, a single Sandbox marker, concise Not booked context and decision-critical consent or uncertain-operation warnings. Recovery guidance must not claim that an uncertain remote exchange or revocation succeeded.

## Do's and Don'ts

- Do keep the safety and accounting boundaries while removing redundant prose.
- Do test mobile, desktop, long names, extreme amounts, unknown currencies and keyboard disclosure in both themes.
- Do preserve complete signed amounts and distinguish accounts across connections.
- Do use orange for interpretation without fabricating interpretation.
- Do not add decoration, empty insights, duplicate data trees, fake sort buttons or explanatory card stacks.
- Do not shrink touch targets or hide errors to make a screen look clean.
