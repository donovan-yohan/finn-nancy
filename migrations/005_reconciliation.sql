-- Statement staging + reconciliation state.
-- Statements NEVER write transactions at ingest; rows stage here and only the
-- reconcile engine promotes unmatched lines (sole-promoter invariant).
CREATE TABLE statement_lines (
  id INTEGER PRIMARY KEY,
  source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
  account_id INTEGER REFERENCES accounts(id),
  posted_on TEXT NOT NULL CHECK (posted_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  raw_description TEXT NOT NULL,
  norm_merchant TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL,            -- SIGNED: expense negative, income positive
  currency TEXT NOT NULL DEFAULT 'CAD',
  balance_cents INTEGER,
  is_pending INTEGER NOT NULL DEFAULT 0 CHECK (is_pending IN (0,1)),
  statement_period TEXT NOT NULL DEFAULT '',
  row_hash TEXT NOT NULL,                    -- canonical; see app/ingest/normalize.py
  match_status TEXT NOT NULL DEFAULT 'unmatched'
     CHECK (match_status IN ('unmatched','matched','promoted','needs_review','ignored')),
  matched_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
  match_method TEXT NOT NULL DEFAULT '' CHECK (match_method IN ('','exact','heuristic','llm','manual')),
  match_score REAL NOT NULL DEFAULT 0,
  match_rationale TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (account_id, row_hash)             -- collapses re-exported/duplicate statement files
);
CREATE INDEX idx_stmtlines_block  ON statement_lines(amount_cents, posted_on);
CREATE INDEX idx_stmtlines_status ON statement_lines(match_status);
CREATE INDEX idx_stmtlines_doc    ON statement_lines(source_document_id);

-- Reconciliation overlay on the ledger row (receipt txns start uncleared).
ALTER TABLE transactions ADD COLUMN cleared_on TEXT NOT NULL DEFAULT '';   -- '' = uncleared
ALTER TABLE transactions ADD COLUMN recon_status TEXT NOT NULL DEFAULT 'uncleared'
     CHECK (recon_status IN ('uncleared','cleared','reversed'));
CREATE INDEX idx_transactions_recon ON transactions(recon_status);
