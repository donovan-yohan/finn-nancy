CREATE VIEW v_statement_coverage_lines AS
SELECT
  sl.id AS line_id,
  sl.source_document_id,
  sd.original_name AS document_name,
  sd.status AS document_status,
  sl.account_id,
  COALESCE(a.name, 'Unassigned') AS account_name,
  sl.posted_on,
  strftime('%Y-%m', sl.posted_on) AS month,
  sl.raw_description,
  sl.norm_merchant,
  sl.amount_cents,
  CASE WHEN sl.amount_cents < 0 THEN ABS(sl.amount_cents) ELSE 0 END AS spend_cents,
  CASE WHEN sl.amount_cents > 0 THEN sl.amount_cents ELSE 0 END AS income_cents,
  sl.match_status,
  sl.matched_transaction_id,
  sl.match_method,
  sl.match_score,
  sl.match_rationale,
  CASE
    WHEN sl.amount_cents > 0 THEN 'income'
    WHEN sl.match_status = 'ignored' THEN 'ignored'
    WHEN sl.match_status IN ('matched', 'promoted') THEN 'covered'
    ELSE 'unmatched'
  END AS coverage_bucket,
  COALESCE(
    (
      SELECT GROUP_CONCAT(c.name, ', ')
      FROM transaction_splits ts
      JOIN categories c ON c.id = ts.category_id
      WHERE ts.transaction_id = sl.matched_transaction_id
    ),
    ''
  ) AS category_names,
  CASE
    WHEN sl.amount_cents >= 0 THEN ''
    WHEN sl.match_status = 'ignored' THEN 'ignored/internal transfer'
    WHEN sl.account_id IS NULL THEN 'needs account'
    WHEN sl.match_status IN ('matched', 'promoted')
      AND EXISTS (
        SELECT 1
        FROM transaction_splits ts
        JOIN categories c ON c.id = ts.category_id
        WHERE ts.transaction_id = sl.matched_transaction_id
          AND c.name = 'Uncategorized'
      )
      THEN 'needs category'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND EXISTS (
        SELECT 1
        FROM transactions t
        WHERE t.account_id IS sl.account_id
          AND t.amount_cents = sl.amount_cents
          AND t.recon_status = 'cleared'
          AND t.posted_on BETWEEN date(sl.posted_on, '-2 days') AND date(sl.posted_on, '+2 days')
      )
      THEN 'possible duplicate'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND EXISTS (
        SELECT 1
        FROM transactions t
        WHERE t.account_id IS sl.account_id
          AND t.amount_cents = sl.amount_cents
          AND t.recon_status = 'uncleared'
          AND t.posted_on BETWEEN date(sl.posted_on, '-7 days') AND date(sl.posted_on, '+1 days')
      )
      THEN 'ambiguous match'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND NOT EXISTS (
        SELECT 1
        FROM transactions t
        WHERE lower(t.description) LIKE '%' || lower(sl.norm_merchant) || '%'
           OR lower(t.counterparty) LIKE '%' || lower(sl.norm_merchant) || '%'
      )
      THEN 'new merchant'
    WHEN sl.match_status IN ('unmatched', 'needs_review') THEN 'missing receipt'
    ELSE ''
  END AS attention_reason
FROM statement_lines sl
JOIN source_documents sd ON sd.id = sl.source_document_id
LEFT JOIN accounts a ON a.id = sl.account_id;

CREATE VIEW v_statement_coverage_by_doc AS
WITH grouped AS (
  SELECT
    source_document_id,
    document_name,
    document_status,
    MIN(posted_on) AS first_posted_on,
    MAX(posted_on) AS last_posted_on,
    COUNT(*) AS line_count,
    SUM(spend_cents) AS statement_spend_cents,
    SUM(CASE WHEN coverage_bucket = 'covered' THEN spend_cents ELSE 0 END) AS covered_spend_cents,
    SUM(CASE WHEN coverage_bucket = 'unmatched' THEN spend_cents ELSE 0 END) AS unmatched_spend_cents,
    SUM(CASE WHEN coverage_bucket = 'ignored' THEN spend_cents ELSE 0 END) AS ignored_spend_cents,
    SUM(income_cents) AS income_cents,
    SUM(CASE WHEN attention_reason <> '' AND attention_reason <> 'ignored/internal transfer' THEN spend_cents ELSE 0 END) AS attention_spend_cents
  FROM v_statement_coverage_lines
  GROUP BY source_document_id
)
SELECT
  *,
  CASE
    WHEN covered_spend_cents + unmatched_spend_cents = 0 THEN 0
    ELSE ROUND(100.0 * covered_spend_cents / (covered_spend_cents + unmatched_spend_cents), 1)
  END AS coverage_pct
FROM grouped;
