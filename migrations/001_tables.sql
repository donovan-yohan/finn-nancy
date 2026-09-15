CREATE TABLE accounts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  institution TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK (kind IN ('chequing', 'savings', 'credit', 'cash', 'investment')),
  currency TEXT NOT NULL DEFAULT 'CAD',
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE categories (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'transfer')),
  brand_owner TEXT NOT NULL CHECK (brand_owner IN ('finn', 'nancy', 'shared')) DEFAULT 'finn',
  color TEXT NOT NULL DEFAULT '#4EA1FF'
);

CREATE TABLE transactions (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  posted_on TEXT NOT NULL CHECK (posted_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  description TEXT NOT NULL,
  counterparty TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL,
  source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
  source TEXT NOT NULL DEFAULT 'sample',
  source_confidence REAL NOT NULL DEFAULT 1.0,
  statement_period TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (source, external_id)
);

CREATE INDEX idx_transactions_posted_on ON transactions(posted_on);
CREATE INDEX idx_transactions_account ON transactions(account_id);
CREATE INDEX idx_transactions_source_document ON transactions(source_document_id);

CREATE TABLE source_documents (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('statement', 'receipt', 'invoice', 'upload', 'other')),
  original_name TEXT NOT NULL DEFAULT '',
  storage_ref TEXT NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '',
  mime_type TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'staged' CHECK (status IN ('staged', 'processed', 'matched', 'needs_review', 'archived')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (storage_ref)
);
CREATE INDEX idx_source_documents_kind ON source_documents(kind);
CREATE INDEX idx_source_documents_status ON source_documents(status);

CREATE TABLE import_batches (
  id INTEGER PRIMARY KEY,
  batch_key TEXT NOT NULL UNIQUE,
  source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
  ingestor TEXT NOT NULL DEFAULT 'hermes',
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  total_files INTEGER NOT NULL DEFAULT 0,
  matched_files INTEGER NOT NULL DEFAULT 0,
  statement_rows INTEGER NOT NULL DEFAULT 0,
  inserted_rows INTEGER NOT NULL DEFAULT 0,
  skipped_duplicates INTEGER NOT NULL DEFAULT 0,
  skipped_warnings INTEGER NOT NULL DEFAULT 0,
  warnings TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE import_audit (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
  source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
  document_name TEXT NOT NULL DEFAULT '',
  statement_period TEXT NOT NULL DEFAULT '',
  source_ref TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('inserted', 'matched_existing', 'skipped_duplicate', 'needs_review', 'error')),
  transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
  warning TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_import_audit_batch ON import_audit(batch_id);
CREATE INDEX idx_import_audit_document ON import_audit(source_document_id);

CREATE TABLE transaction_splits (
  id INTEGER PRIMARY KEY,
  transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  amount_cents INTEGER NOT NULL,
  memo TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_splits_transaction ON transaction_splits(transaction_id);
CREATE INDEX idx_splits_category ON transaction_splits(category_id);
