-- Stable device-generated capture ids make multipart retries idempotent.
--
-- Keep the submission row when a document is deleted so an old device retry
-- cannot silently resurrect evidence that the user intentionally removed.
CREATE TABLE capture_submissions (
  client_capture_id TEXT PRIMARY KEY,
  source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
  sha256 TEXT NOT NULL,
  channel TEXT NOT NULL,
  source_metadata_json TEXT NOT NULL DEFAULT '{}',
  stored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_capture_submissions_document
  ON capture_submissions(source_document_id);
