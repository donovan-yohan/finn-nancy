DROP VIEW IF EXISTS v_recurring_payment_deltas;
DROP VIEW IF EXISTS v_recurring_payment_series_monthly;

CREATE VIEW v_recurring_payment_series_monthly AS
WITH monthly AS (
  SELECT
    TRIM(COALESCE(NULLIF(t.counterparty, ''), t.description)) AS merchant,
    t.account_id,
    a.name AS account_name,
    strftime('%Y-%m', t.posted_on) AS month,
    MIN(t.posted_on) AS first_posted_on,
    MAX(t.posted_on) AS last_posted_on,
    COUNT(DISTINCT t.id) AS tx_count,
    SUM(ABS(ts.amount_cents)) AS amount_cents,
    GROUP_CONCAT(DISTINCT t.id) AS transaction_ids,
    GROUP_CONCAT(DISTINCT sl.id) AS statement_line_ids,
    GROUP_CONCAT(DISTINCT c.name) AS category_names
  FROM transactions t
  JOIN accounts a ON a.id = t.account_id
  JOIN transaction_splits ts ON ts.transaction_id = t.id
  JOIN categories c ON c.id = ts.category_id
  LEFT JOIN statement_lines sl ON sl.matched_transaction_id = t.id
  WHERE t.recon_status = 'cleared'
    AND t.amount_cents < 0
    AND c.kind = 'expense'
    AND TRIM(COALESCE(NULLIF(t.counterparty, ''), t.description)) <> ''
  GROUP BY merchant, t.account_id, month
)
SELECT
  monthly.*,
  COUNT(*) OVER (PARTITION BY merchant, account_id) AS months_seen,
  MIN(month) OVER (PARTITION BY merchant, account_id) AS first_month,
  MAX(month) OVER (PARTITION BY merchant, account_id) AS last_month,
  ROUND(AVG(amount_cents) OVER (PARTITION BY merchant, account_id), 2) AS avg_monthly_cents
FROM monthly;

CREATE VIEW v_recurring_payment_deltas AS
SELECT
  curr.merchant,
  curr.account_id,
  curr.account_name,
  curr.month,
  prev.month AS previous_month,
  curr.first_month,
  curr.last_month,
  curr.months_seen,
  curr.current_amount_cents,
  curr.previous_amount_cents,
  curr.amount_delta_cents,
  curr.pct_change,
  CASE
    WHEN curr.amount_delta_cents > 0 THEN 'increase'
    WHEN curr.amount_delta_cents < 0 THEN 'decrease'
    ELSE 'stable'
  END AS direction,
  CASE
    WHEN ABS(curr.amount_delta_cents) >= 500
     AND ABS(COALESCE(curr.pct_change, 0)) >= 5.0
    THEN 1
    ELSE 0
  END AS is_meaningful_delta,
  curr.tx_count AS current_tx_count,
  prev.tx_count AS previous_tx_count,
  curr.transaction_ids AS current_transaction_ids,
  prev.transaction_ids AS previous_transaction_ids,
  curr.statement_line_ids AS current_statement_line_ids,
  prev.statement_line_ids AS previous_statement_line_ids,
  curr.category_names
FROM (
  SELECT
    series.*,
    prev.month AS previous_month,
    series.amount_cents AS current_amount_cents,
    prev.amount_cents AS previous_amount_cents,
    series.amount_cents - prev.amount_cents AS amount_delta_cents,
    CASE
      WHEN prev.amount_cents IS NULL OR prev.amount_cents = 0 THEN NULL
      ELSE ROUND(100.0 * (series.amount_cents - prev.amount_cents) / prev.amount_cents, 1)
    END AS pct_change
  FROM v_recurring_payment_series_monthly series
  JOIN v_recurring_payment_series_monthly prev
    ON prev.merchant = series.merchant
   AND prev.account_id = series.account_id
   AND prev.month = strftime('%Y-%m', date(series.month || '-01', '-1 month'))
  WHERE series.months_seen >= 2
) curr
JOIN v_recurring_payment_series_monthly prev
  ON prev.merchant = curr.merchant
 AND prev.account_id = curr.account_id
 AND prev.month = curr.previous_month
ORDER BY curr.month DESC, ABS(curr.amount_delta_cents) DESC, curr.merchant;
