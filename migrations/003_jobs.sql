-- Durable background-job queue. The web tier only enqueues; a single worker
-- consumer drains it. This is the ONLY queue in the app (no Celery/Redis/broker).
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,                       -- 'ingest_document' | 'reconcile_document' | 'monthly_insight'
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending'
         CHECK (status IN ('pending','running','done','error','dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  last_error TEXT NOT NULL DEFAULT '',
  source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
  available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- backoff gate
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX idx_jobs_claim ON jobs(status, available_at);
