-- A budgeted month with no transactions was invisible.
--
-- v_month_spine derived its range from v_cashflow_monthly alone, so a month
-- that carried a budget but recorded no spending produced no grid row, no
-- v_budget_vs_actual row, and therefore no underspend. The effect is that the
-- clearest possible underspend -- budget set, nothing spent -- could never
-- fund a goal at month close.
--
-- With no transactions at all the range still falls back to the current month,
-- so an empty database behaves as before.

DROP VIEW IF EXISTS v_month_spine;

CREATE VIEW v_month_spine AS
WITH RECURSIVE known_months(month) AS (
  SELECT month FROM v_cashflow_monthly
  UNION
  SELECT period_month FROM budgets WHERE period_month <> ''
),
bounds AS (
  SELECT
    COALESCE(MIN(month), strftime('%Y-%m', 'now')) AS min_month,
    COALESCE(MAX(month), strftime('%Y-%m', 'now')) AS max_month
  FROM known_months
),
spine(month) AS (
  SELECT min_month FROM bounds
  UNION ALL
  SELECT strftime('%Y-%m', date(month || '-01', '+1 month'))
  FROM spine, bounds
  WHERE month < bounds.max_month
)
SELECT month FROM spine;
