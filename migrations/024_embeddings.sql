-- Embedding cache for approved transaction RAG chunks.
--
-- Storage decision: keep float32 vectors in a normal SQLite BLOB instead of a
-- loadable vector extension. The corpus is personal-ledger scale, extension
-- loading is not guaranteed in all Python builds, and app-side cosine keeps
-- ingestion/page renders functional even when embeddings are disabled.
CREATE TABLE embeddings (
  id INTEGER PRIMARY KEY,
  ref_kind TEXT NOT NULL CHECK (ref_kind = 'transaction'),
  ref_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  dims INTEGER NOT NULL CHECK (dims > 0),
  norm REAL NOT NULL DEFAULT 0,
  content_sha256 TEXT NOT NULL,
  content TEXT NOT NULL,
  vector BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (ref_kind, ref_id, model)
);

CREATE INDEX idx_embeddings_ref ON embeddings(ref_kind, ref_id);
CREATE INDEX idx_embeddings_model ON embeddings(model, ref_kind);
CREATE INDEX idx_embeddings_hash ON embeddings(model, content_sha256);

CREATE TRIGGER embeddings_transactions_ad AFTER DELETE ON transactions BEGIN
  DELETE FROM embeddings WHERE ref_kind = 'transaction' AND ref_id = old.id;
END;
