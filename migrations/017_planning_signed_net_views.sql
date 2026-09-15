DROP VIEW IF EXISTS v_planning_category_net_trend;
DROP VIEW IF EXISTS v_planning_category_monthly_net;

CREATE VIEW v_planning_category_monthly_net AS
SELECT
  sd.month,
  sd.category_id,
  sd.category_name,
  sd.category_kind,
  sd.brand_owner,
  sd.color,
  SUM(CASE WHEN sd.split_amount_cents < 0 THEN -sd.split_amount_cents ELSE 0 END) AS charge_cents,
  SUM(CASE WHEN sd.split_amount_cents > 0 THEN sd.split_amount_cents ELSE 0 END) AS refund_cents,
  -SUM(sd.split_amount_cents) AS net_expense_cents
FROM v_split_detail sd
WHERE sd.category_kind = 'expense'
GROUP BY sd.month, sd.category_id;

CREATE VIEW v_planning_category_net_trend AS
SELECT
  curr.month,
  curr.category_id,
  curr.category_name,
  curr.category_kind,
  curr.brand_owner,
  curr.color,
  curr.charge_cents,
  curr.refund_cents,
  curr.net_expense_cents,
  COALESCE(prev.net_expense_cents, 0) AS prev_net_expense_cents,
  curr.net_expense_cents - COALESCE(prev.net_expense_cents, 0) AS net_delta_cents,
  CASE
    WHEN prev.net_expense_cents IS NULL OR prev.net_expense_cents <= 0 THEN NULL
    ELSE ROUND(100.0 * (curr.net_expense_cents - prev.net_expense_cents) / prev.net_expense_cents, 1)
  END AS net_pct_change
FROM v_planning_category_monthly_net curr
LEFT JOIN v_planning_category_monthly_net prev
  ON prev.category_id = curr.category_id
 AND prev.month = strftime('%Y-%m', date(curr.month || '-01', '-1 month'))
ORDER BY curr.month, curr.category_id;
