CREATE VIEW v_split_detail AS
SELECT
  t.id AS transaction_id,
  t.posted_on,
  strftime('%Y-%m', t.posted_on) AS month,
  a.name AS account_name,
  a.kind AS account_kind,
  t.description,
  t.counterparty,
  t.amount_cents AS transaction_amount_cents,
  s.amount_cents AS split_amount_cents,
  c.id AS category_id,
  c.name AS category_name,
  c.kind AS category_kind,
  c.brand_owner,
  c.color,
  s.memo
FROM transaction_splits s
JOIN transactions t ON t.id = s.transaction_id
JOIN accounts a ON a.id = t.account_id
JOIN categories c ON c.id = s.category_id;

CREATE VIEW v_cashflow_monthly AS
SELECT
  month,
  COALESCE(SUM(CASE WHEN category_kind = 'income' THEN split_amount_cents ELSE 0 END), 0) AS income_cents,
  ABS(COALESCE(SUM(CASE WHEN category_kind = 'expense' THEN split_amount_cents ELSE 0 END), 0)) AS expense_cents,
  COALESCE(SUM(CASE WHEN category_kind IN ('income', 'expense') THEN split_amount_cents ELSE 0 END), 0) AS net_cents
FROM v_split_detail
GROUP BY month;

CREATE VIEW v_category_monthly AS
SELECT
  month,
  category_id,
  category_name,
  category_kind,
  brand_owner,
  color,
  SUM(split_amount_cents) AS amount_cents,
  ABS(SUM(split_amount_cents)) AS magnitude_cents
FROM v_split_detail
WHERE category_kind IN ('income', 'expense')
GROUP BY month, category_id;

CREATE VIEW v_category_totals AS
WITH totals AS (
  SELECT
    category_id,
    category_name,
    category_kind,
    brand_owner,
    color,
    SUM(split_amount_cents) AS total_cents,
    ABS(SUM(split_amount_cents)) AS magnitude_cents
  FROM v_split_detail
  WHERE category_kind IN ('income', 'expense')
  GROUP BY category_id
), grand AS (
  SELECT SUM(magnitude_cents) AS total_magnitude_cents FROM totals
)
SELECT
  totals.*,
  CASE
    WHEN grand.total_magnitude_cents = 0 THEN 0
    ELSE ROUND(100.0 * totals.magnitude_cents / grand.total_magnitude_cents, 1)
  END AS pct_of_total
FROM totals, grand;

CREATE VIEW v_transactions_recent AS
SELECT
  t.id,
  t.posted_on,
  a.name AS account_name,
  t.description,
  t.counterparty,
  t.amount_cents,
  GROUP_CONCAT(c.name, ', ') AS categories
FROM transactions t
JOIN accounts a ON a.id = t.account_id
LEFT JOIN transaction_splits s ON s.transaction_id = t.id
LEFT JOIN categories c ON c.id = s.category_id
GROUP BY t.id
ORDER BY t.posted_on DESC, t.id DESC;
