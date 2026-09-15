-- Per-account balance assertions (FN-106).
--
-- A statement's printed closing balance is a point-in-time truth: at asof_date the
-- account balance WAS asserted_cents. beancount's insight is that a failed balance
-- assertion IS the reconciliation exception, carrying an exact signed delta and date.
-- asserted_cents is stored verbatim as the extractor produced it (signed the same way
-- as the ledger: expenses negative, income/credits positive).
--
-- UNIQUE(account_id, asof_date): a statement re-approved or re-processed derives the same
-- asof_date, so capture upserts rather than duplicating the assertion.
CREATE TABLE IF NOT EXISTS account_balance_assertions (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  asof_date TEXT NOT NULL CHECK (asof_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  asserted_cents INTEGER NOT NULL,
  source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
  statement_period TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (account_id, asof_date)
);

CREATE INDEX IF NOT EXISTS idx_balance_assertions_account ON account_balance_assertions(account_id);
CREATE INDEX IF NOT EXISTS idx_balance_assertions_asof ON account_balance_assertions(asof_date);
