CREATE TABLE IF NOT EXISTS subscription_watchlist_decisions (
  id INTEGER PRIMARY KEY,
  merchant TEXT NOT NULL,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  decision TEXT NOT NULL CHECK (
    decision IN ('subscription', 'not_subscription', 'already_known', 'watch_next_month')
  ),
  decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (merchant, account_id)
);

CREATE INDEX IF NOT EXISTS idx_subscription_watchlist_decisions_account
  ON subscription_watchlist_decisions(account_id);

DROP VIEW IF EXISTS v_subscription_watchlist_candidates;

CREATE VIEW v_subscription_watchlist_candidates AS
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
),
series AS (
  SELECT
    monthly.*,
    COUNT(*) OVER (PARTITION BY merchant, account_id) AS months_seen,
    MIN(month) OVER (PARTITION BY merchant, account_id) AS first_month,
    MAX(month) OVER (PARTITION BY merchant, account_id) AS last_month
  FROM monthly
)
SELECT
  curr.merchant,
  curr.account_id,
  curr.account_name,
  'new_subscription_likely' AS candidate_type,
  prev.month AS first_month,
  curr.month AS last_month,
  prev.first_posted_on AS first_seen_on,
  curr.last_posted_on AS last_seen_on,
  date(curr.last_posted_on, '+1 month') AS expected_next_charge_on,
  CAST(ROUND((curr.amount_cents + prev.amount_cents) / 2.0) AS INTEGER) AS estimated_amount_cents,
  curr.months_seen,
  curr.amount_cents AS current_amount_cents,
  prev.amount_cents AS previous_amount_cents,
  curr.tx_count AS current_tx_count,
  prev.tx_count AS previous_tx_count,
  prev.transaction_ids || ',' || curr.transaction_ids AS transaction_ids,
  TRIM(COALESCE(prev.statement_line_ids, '') || ',' || COALESCE(curr.statement_line_ids, ''), ',') AS statement_line_ids,
  COALESCE(curr.category_names, prev.category_names) AS category_names
FROM series curr
JOIN series prev
  ON prev.merchant = curr.merchant
 AND prev.account_id = curr.account_id
 AND prev.month = strftime('%Y-%m', date(curr.month || '-01', '-1 month'))
WHERE curr.months_seen = 2
  AND curr.first_month = prev.month
  AND curr.last_month = curr.month
ORDER BY curr.month DESC, estimated_amount_cents DESC, curr.merchant;
