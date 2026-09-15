-- Ingestion staging + merchant memory + account resolution.
-- NOTE: the 'Uncategorized' fallback category is ensured at RUNTIME (get-or-create),
-- not seeded here, because this migration runs before fixtures load in seed-sample and
-- an autoincrement insert would collide with the fixtures' explicit category ids (1..10).

-- Structured extraction staged before (or instead of) ledger commit, so a low-confidence
-- receipt can be re-promoted after human review WITHOUT re-running the (cold-start) vision call.
CREATE TABLE ingest_extractions (
  id INTEGER PRIMARY KEY,
  source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
  doc_kind TEXT NOT NULL,                   -- 'receipt' | 'statement'
  extracted_json TEXT NOT NULL,             -- pydantic .model_dump_json()
  confidence REAL NOT NULL DEFAULT 0,
  external_id TEXT NOT NULL DEFAULT '',     -- id used for the receipt transaction
  proposed_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  proposed_category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  review_status TEXT NOT NULL DEFAULT 'pending'
         CHECK (review_status IN ('pending','approved','rejected','auto')),
  transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_ingest_extractions_review ON ingest_extractions(review_status);
CREATE INDEX idx_ingest_extractions_document ON ingest_extractions(source_document_id);

-- The one merchant-memory table (classification + reconciliation alias, superset).
CREATE TABLE merchant_aliases (
  id INTEGER PRIMARY KEY,
  raw_pattern TEXT NOT NULL UNIQUE,         -- normalized token, e.g. "LOBLAWS"
  canonical   TEXT NOT NULL DEFAULT '',     -- display form, e.g. "Loblaws"
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  hits INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Account resolution key for statements/receipts (e.g. "Synthetic Bank:chequing:9003").
ALTER TABLE accounts ADD COLUMN external_ref TEXT NOT NULL DEFAULT '';
