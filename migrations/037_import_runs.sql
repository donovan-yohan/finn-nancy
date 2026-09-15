-- A batch upload needs an object with a durable URL.
--
-- Uploads previously returned a transient list of per-file results that
-- vanished on refresh, so "drop five statements and come back later" had no
-- destination to come back to. A run groups the documents captured together
-- and lets the UI poll one rollup instead of one request per document.

CREATE TABLE import_runs (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL DEFAULT 'web'
    CHECK (length(trim(channel)) BETWEEN 1 AND 40),
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE source_documents ADD COLUMN import_run_id INTEGER
  REFERENCES import_runs(id);

CREATE INDEX idx_source_documents_import_run
  ON source_documents(import_run_id);
