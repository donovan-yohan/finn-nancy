DROP VIEW IF EXISTS v_category_underspend_monthly;
DROP VIEW IF EXISTS v_budget_vs_actual;

CREATE TABLE household_members (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'seed', 'account', 'statement')),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO household_members(name, slug, source)
VALUES
  ('Sample Member A', 'sample_member_a', 'seed'),
  ('Sample Member B', 'sample_member_b', 'seed');

ALTER TABLE budgets ADD COLUMN owner_member_id INTEGER REFERENCES household_members(id) ON DELETE SET NULL;

UPDATE budgets
SET owner_member_id = (
  SELECT id FROM household_members WHERE slug = budgets.owner
)
WHERE owner IN ('sample_member_a', 'sample_member_b');

CREATE VIEW v_budget_vs_actual AS
WITH expense_categories AS (
  SELECT id AS category_id
  FROM categories
  WHERE kind = 'expense'
),
actuals AS (
  SELECT
    month,
    category_id,
    SUM(magnitude_cents) AS actual_cents
  FROM v_expense_classified
  GROUP BY month, category_id
),
grid AS (
  SELECT s.month, c.category_id
  FROM v_month_spine s
  CROSS JOIN expense_categories c
),
resolved AS (
  SELECT
    g.month,
    g.category_id,
    COALESCE(bm.amount_cents, bd.amount_cents, 0) AS budget_cents,
    COALESCE(bm.owner_member_id, bd.owner_member_id) AS owner_member_id,
    COALESCE(bm.owner, bd.owner, 'shared') AS legacy_budget_owner
  FROM grid g
  LEFT JOIN budgets bm
    ON bm.category_id = g.category_id
   AND bm.period_month = g.month
  LEFT JOIN budgets bd
    ON bd.category_id = g.category_id
   AND bd.period_month = ''
)
SELECT
  r.month,
  r.category_id,
  c.name AS category_name,
  c.brand_owner,
  c.color,
  c.is_leisure,
  r.budget_cents,
  r.owner_member_id,
  CASE
    WHEN hm.name IS NOT NULL THEN hm.name
    WHEN r.legacy_budget_owner = 'sample_member_a' THEN 'Sample Member A'
    WHEN r.legacy_budget_owner = 'sample_member_b' THEN 'Sample Member B'
    ELSE 'Shared'
  END AS budget_owner,
  COALESCE(a.actual_cents, 0) AS actual_cents,
  r.budget_cents - COALESCE(a.actual_cents, 0) AS remaining_cents,
  CASE
    WHEN r.budget_cents > 0 THEN ROUND(100.0 * COALESCE(a.actual_cents, 0) / r.budget_cents, 1)
    ELSE NULL
  END AS pct_used
FROM resolved r
JOIN categories c
  ON c.id = r.category_id
LEFT JOIN household_members hm
  ON hm.id = r.owner_member_id
LEFT JOIN actuals a
  ON a.category_id = r.category_id
 AND a.month = r.month
WHERE r.budget_cents > 0 OR COALESCE(a.actual_cents, 0) > 0;

CREATE VIEW v_category_underspend_monthly AS
SELECT
  month,
  category_id,
  category_name,
  brand_owner,
  owner_member_id,
  budget_owner,
  remaining_cents AS underspend_cents
FROM v_budget_vs_actual
WHERE remaining_cents > 0;
