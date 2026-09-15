DROP VIEW IF EXISTS v_expense_classified;
DROP VIEW IF EXISTS v_leisure_vs_bigticket;
DROP VIEW IF EXISTS v_category_monthly_trend;
DROP VIEW IF EXISTS v_top_merchants;
DROP VIEW IF EXISTS v_recurring_candidates;
DROP VIEW IF EXISTS v_cashflow_monthly_trend;
DROP VIEW IF EXISTS v_cashflow_runway;
DROP VIEW IF EXISTS v_month_spine;

CREATE VIEW v_month_spine AS
WITH RECURSIVE bounds AS (
  SELECT
    COALESCE(MIN(month), strftime('%Y-%m', 'now')) AS min_month,
    COALESCE(MAX(month), strftime('%Y-%m', 'now')) AS max_month
  FROM v_cashflow_monthly
),
spine(month) AS (
  SELECT min_month FROM bounds
  UNION ALL
  SELECT strftime('%Y-%m', date(month || '-01', '+1 month'))
  FROM spine, bounds
  WHERE month < bounds.max_month
)
SELECT month FROM spine;

CREATE VIEW v_expense_classified AS
WITH expense_totals AS (
  SELECT
    transaction_id,
    SUM(ABS(split_amount_cents)) AS transaction_expense_cents
  FROM v_split_detail
  WHERE category_kind = 'expense'
  GROUP BY transaction_id
)
SELECT
  sd.transaction_id,
  sd.posted_on,
  sd.month,
  sd.account_name,
  sd.description,
  sd.counterparty,
  sd.category_id,
  sd.category_name,
  sd.brand_owner,
  sd.color,
  c.is_leisure,
  ABS(sd.split_amount_cents) AS magnitude_cents,
  et.transaction_expense_cents,
  CASE
    WHEN et.transaction_expense_cents >= CAST(
      (SELECT value FROM app_settings WHERE key = 'big_ticket_threshold_cents') AS INTEGER
    )
    THEN 1
    ELSE 0
  END AS is_big_ticket
FROM v_split_detail sd
JOIN categories c ON c.id = sd.category_id
JOIN expense_totals et ON et.transaction_id = sd.transaction_id
WHERE sd.category_kind = 'expense';

CREATE VIEW v_leisure_vs_bigticket AS
WITH months AS (
  SELECT month FROM v_month_spine
),
classified AS (
  SELECT
    month,
    SUM(CASE WHEN is_leisure = 1 AND is_big_ticket = 0 THEN magnitude_cents ELSE 0 END) AS leisure_cents,
    SUM(CASE WHEN is_big_ticket = 1 THEN magnitude_cents ELSE 0 END) AS bigticket_cents,
    SUM(CASE WHEN is_leisure = 0 AND is_big_ticket = 0 THEN magnitude_cents ELSE 0 END) AS everyday_cents
  FROM v_expense_classified
  GROUP BY month
)
SELECT
  m.month,
  COALESCE(classified.leisure_cents, 0) AS leisure_cents,
  COALESCE(classified.bigticket_cents, 0) AS bigticket_cents,
  COALESCE(classified.everyday_cents, 0) AS everyday_cents
FROM months m
LEFT JOIN classified
  ON classified.month = m.month
ORDER BY m.month;

CREATE VIEW v_category_monthly_trend AS
SELECT
  curr.month,
  curr.category_id,
  curr.category_name,
  curr.category_kind,
  curr.brand_owner,
  curr.color,
  curr.amount_cents,
  curr.magnitude_cents,
  COALESCE(prev.amount_cents, 0) AS prev_amount_cents,
  curr.amount_cents - COALESCE(prev.amount_cents, 0) AS amount_delta_cents,
  COALESCE(prev.magnitude_cents, 0) AS prev_magnitude_cents,
  curr.magnitude_cents - COALESCE(prev.magnitude_cents, 0) AS magnitude_delta_cents,
  CASE
    WHEN prev.magnitude_cents IS NULL OR prev.magnitude_cents = 0 THEN NULL
    ELSE ROUND(100.0 * (curr.magnitude_cents - prev.magnitude_cents) / prev.magnitude_cents, 1)
  END AS magnitude_pct_change
FROM v_category_monthly curr
LEFT JOIN v_category_monthly prev
  ON prev.category_id = curr.category_id
 AND prev.month = strftime('%Y-%m', date(curr.month || '-01', '-1 month'))
ORDER BY curr.month, curr.category_id;

CREATE VIEW v_top_merchants AS
WITH merchant_monthly AS (
  SELECT
    sd.month,
    sd.counterparty AS merchant,
    SUM(ABS(sd.split_amount_cents)) AS amount_cents,
    COUNT(*) AS tx_count
  FROM v_split_detail sd
  WHERE sd.category_kind = 'expense'
    AND sd.counterparty != ''
  GROUP BY sd.month, sd.counterparty
),
ranks AS (
  SELECT
    month,
    merchant,
    amount_cents,
    tx_count,
    ROW_NUMBER() OVER (PARTITION BY month ORDER BY amount_cents DESC, merchant) AS merchant_rank
  FROM merchant_monthly
)
SELECT
  month,
  merchant,
  amount_cents,
  tx_count,
  merchant_rank
FROM ranks
WHERE merchant_rank <= 10
ORDER BY month, merchant_rank;

CREATE VIEW v_recurring_candidates AS
WITH expense_by_month AS (
  SELECT
    sd.month,
    sd.counterparty AS merchant,
    SUM(ABS(sd.split_amount_cents)) AS amount_cents
  FROM v_split_detail sd
  WHERE sd.category_kind = 'expense'
    AND sd.counterparty != ''
  GROUP BY sd.month, sd.counterparty
),
stats AS (
  SELECT
    merchant,
    COUNT(*) AS months_seen,
    MIN(month) AS first_month,
    MAX(month) AS last_month,
    ROUND(AVG(amount_cents), 2) AS avg_monthly_cents
  FROM expense_by_month
  GROUP BY merchant
)
SELECT
  merchant,
  months_seen,
  first_month,
  last_month,
  avg_monthly_cents
FROM stats
WHERE months_seen >= 2
ORDER BY months_seen DESC, avg_monthly_cents DESC, merchant;

CREATE VIEW v_cashflow_monthly_trend AS
SELECT
  curr.month,
  curr.income_cents,
  curr.expense_cents,
  curr.net_cents,
  COALESCE(prev.income_cents, 0) AS prev_income_cents,
  COALESCE(prev.expense_cents, 0) AS prev_expense_cents,
  COALESCE(prev.net_cents, 0) AS prev_net_cents,
  curr.income_cents - COALESCE(prev.income_cents, 0) AS income_delta_cents,
  curr.expense_cents - COALESCE(prev.expense_cents, 0) AS expense_delta_cents,
  curr.net_cents - COALESCE(prev.net_cents, 0) AS net_delta_cents,
  CASE
    WHEN prev.income_cents IS NULL OR prev.income_cents = 0 THEN NULL
    ELSE ROUND(100.0 * (curr.income_cents - prev.income_cents) / prev.income_cents, 1)
  END AS income_pct_change,
  CASE
    WHEN prev.expense_cents IS NULL OR prev.expense_cents = 0 THEN NULL
    ELSE ROUND(100.0 * (curr.expense_cents - prev.expense_cents) / prev.expense_cents, 1)
  END AS expense_pct_change,
  CASE
    WHEN prev.net_cents IS NULL OR prev.net_cents = 0 THEN NULL
    ELSE ROUND(100.0 * (curr.net_cents - prev.net_cents) / prev.net_cents, 1)
  END AS net_pct_change
FROM v_cashflow_monthly curr
LEFT JOIN v_cashflow_monthly prev
  ON prev.month = strftime('%Y-%m', date(curr.month || '-01', '-1 month'))
ORDER BY curr.month;

CREATE VIEW v_cashflow_runway AS
WITH base AS (
  SELECT
    month,
    income_cents,
    expense_cents,
    net_cents,
    SUM(net_cents) OVER (
      ORDER BY month
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS cumulative_net_cents
  FROM v_cashflow_monthly
),
avg_expenses AS (
  SELECT AVG(expense_cents) AS avg_expense_cents
  FROM v_cashflow_monthly
  WHERE expense_cents > 0
)
SELECT
  base.month,
  base.income_cents,
  base.expense_cents,
  base.net_cents,
  base.cumulative_net_cents,
  CASE
    WHEN avg_expense_cents IS NULL OR avg_expense_cents = 0 THEN NULL
    ELSE ROUND(base.cumulative_net_cents / avg_expense_cents, 2)
  END AS runway_months
FROM base CROSS JOIN avg_expenses
ORDER BY base.month;
