-- Identity correction is local only. Never delete/reassign audited ownership.
ALTER TABLE household_members ADD COLUMN name_required INTEGER NOT NULL DEFAULT 0
  CHECK (name_required IN (0,1));

-- Migration-owned recovery metadata preserves the shipped label and active state.
INSERT INTO app_settings(key, value)
SELECT '__household_seed_retirement_042', json_group_array(json_object(
  'id', id, 'name', name, 'slug', slug, 'is_active', is_active,
  'referenced', EXISTS (SELECT 1 FROM budgets WHERE owner_member_id=m.id)
    OR EXISTS (SELECT 1 FROM bank_consents WHERE member_id=m.id)
    OR EXISTS (SELECT 1 FROM bank_connections WHERE member_id=m.id)
    OR EXISTS (SELECT 1 FROM bank_link_sessions WHERE member_id=m.id)
    OR EXISTS (SELECT 1 FROM bank_bindings WHERE owner_member_id=m.id)
))
FROM household_members m
WHERE source='seed' AND (
  (name='Sample Member A' AND slug='sample_member_a') OR
  (name='Sample Member B' AND slug='sample_member_b')
);

UPDATE household_members AS member
SET name_required=(SELECT json_extract(value,'$.referenced')
    FROM json_each((SELECT value FROM app_settings WHERE key='__household_seed_retirement_042'))
    WHERE json_extract(value,'$.id')=member.id),
  is_active=CASE WHEN (SELECT json_extract(value,'$.referenced')
    FROM json_each((SELECT value FROM app_settings WHERE key='__household_seed_retirement_042'))
    WHERE json_extract(value,'$.id')=member.id) THEN is_active ELSE 0 END,
  -- Avoid clobbering a real person who happens to have the neutral recovery label.
  name=(WITH RECURSIVE candidates(n, label) AS (
    SELECT 0, 'Name required (legacy ' || member.id || ')'
    UNION ALL SELECT n+1, 'Name required (legacy ' || member.id || ') #' || (n+1)
    FROM candidates WHERE EXISTS (SELECT 1 FROM household_members WHERE name=label)
  ) SELECT label FROM candidates
    WHERE NOT EXISTS (SELECT 1 FROM household_members WHERE name=label)
    ORDER BY n LIMIT 1)
WHERE id IN (SELECT json_extract(value,'$.id')
  FROM json_each((SELECT value FROM app_settings WHERE key='__household_seed_retirement_042')));

-- Change labels only. Preserve the two established budget calculations and consumers.
DROP VIEW v_budget_vs_actual;
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
    WHEN r.legacy_budget_owner IN ('sample_member_a','sample_member_b') THEN 'Name required'
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

DROP VIEW v_report_budget_vs_actual;
CREATE VIEW v_report_budget_vs_actual AS
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
  FROM v_resolved_expense_category_monthly
  GROUP BY month, category_id
),
grid AS (
  SELECT spine.month, category.category_id
  FROM v_month_spine spine
  CROSS JOIN expense_categories category
),
resolved AS (
  SELECT
    grid.month,
    grid.category_id,
    COALESCE(monthly.amount_cents, default_budget.amount_cents, 0)
      AS budget_cents,
    COALESCE(
      monthly.owner_member_id,
      default_budget.owner_member_id
    ) AS owner_member_id,
    COALESCE(monthly.owner, default_budget.owner, 'shared')
      AS legacy_budget_owner
  FROM grid
  LEFT JOIN budgets monthly
    ON monthly.category_id = grid.category_id
   AND monthly.period_month = grid.month
  LEFT JOIN budgets default_budget
    ON default_budget.category_id = grid.category_id
   AND default_budget.period_month = ''
)
SELECT
  resolved.month,
  resolved.category_id,
  category.name AS category_name,
  category.brand_owner,
  category.color,
  category.is_leisure,
  resolved.budget_cents,
  resolved.owner_member_id,
  CASE
    WHEN member.name IS NOT NULL THEN member.name
    WHEN resolved.legacy_budget_owner IN ('sample_member_a','sample_member_b') THEN 'Name required'
    ELSE 'Shared'
  END AS budget_owner,
  COALESCE(actuals.actual_cents, 0) AS actual_cents,
  resolved.budget_cents - COALESCE(actuals.actual_cents, 0)
    AS remaining_cents,
  CASE
    WHEN resolved.budget_cents > 0 THEN ROUND(
      100.0 * COALESCE(actuals.actual_cents, 0)
      / resolved.budget_cents,
      1
    )
    ELSE NULL
  END AS pct_used
FROM resolved
JOIN categories category
  ON category.id = resolved.category_id
LEFT JOIN household_members member
  ON member.id = resolved.owner_member_id
LEFT JOIN actuals
  ON actuals.category_id = resolved.category_id
 AND actuals.month = resolved.month
WHERE resolved.budget_cents > 0
   OR COALESCE(actuals.actual_cents, 0) > 0;
