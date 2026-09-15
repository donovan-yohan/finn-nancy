-- FN-146: content-free capture provenance, lifecycle telemetry, and
-- versioned third-party transport consent.
--
-- This migration is provisionally numbered 032 because FN-142 owns 031.
-- It must be rebased and proven in 031 -> 032 order before publication.

CREATE TABLE capture_provenance (
  capture_id TEXT PRIMARY KEY
    REFERENCES capture_submissions(client_capture_id),
  source_document_id INTEGER NOT NULL,
  channel TEXT NOT NULL
    CHECK (channel IN ('web', 'share', 'inbox', 'telegram', 'api', 'mcp')),
  source TEXT NOT NULL
    CHECK (source IN (
      'camera', 'file', 'share', 'shortcut', 'web', 'inbox',
      'telegram', 'api', 'mcp', 'unspecified'
    )),
  transport_class TEXT NOT NULL
    CHECK (transport_class IN ('local_only', 'direct_network', 'third_party')),
  dedup_kind TEXT NOT NULL
    CHECK (dedup_kind IN ('new', 'content', 'replay')),
  proof_run_id TEXT NOT NULL DEFAULT ''
    CHECK (
      proof_run_id = ''
      OR (
        length(proof_run_id) = 36
        AND proof_run_id NOT GLOB '*[^a-f0-9-]*'
      )
    ),
  device_cohort_id TEXT NOT NULL DEFAULT ''
    CHECK (
      device_cohort_id = ''
      OR (
        length(device_cohort_id) = 36
        AND device_cohort_id NOT GLOB '*[^a-f0-9-]*'
      )
    ),
  accepted_at TEXT NOT NULL,
  durable_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_capture_provenance_document
  ON capture_provenance(source_document_id, accepted_at);
CREATE INDEX idx_capture_provenance_channel
  ON capture_provenance(channel, accepted_at);
CREATE INDEX idx_capture_provenance_proof_cohort
  ON capture_provenance(proof_run_id, device_cohort_id, accepted_at);

CREATE TABLE capture_events (
  id INTEGER PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE,
  capture_id TEXT NOT NULL
    REFERENCES capture_submissions(client_capture_id),
  source_document_id INTEGER NOT NULL,
  event_kind TEXT NOT NULL
    CHECK (event_kind IN (
      'accepted', 'durable', 'durable_ack', 'deduplicated',
      'offline_recovered', 'processing', 'processed', 'retry',
      'terminal_failure'
    )),
  stage_key TEXT NOT NULL
    CHECK (
      length(stage_key) BETWEEN 1 AND 64
      AND stage_key NOT GLOB '*[^a-z0-9_]*'
    ),
  occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  duration_ms INTEGER
    CHECK (duration_ms IS NULL OR duration_ms BETWEEN 0 AND 604800000),
  attempt_no INTEGER NOT NULL DEFAULT 0
    CHECK (attempt_no BETWEEN 0 AND 1000),
  reason_code TEXT NOT NULL DEFAULT ''
    CHECK (
      length(reason_code) <= 64
      AND reason_code NOT GLOB '*[^a-z0-9_]*'
    )
);

CREATE INDEX idx_capture_events_capture
  ON capture_events(capture_id, id);
CREATE INDEX idx_capture_events_document
  ON capture_events(source_document_id, id);
CREATE INDEX idx_capture_events_kind_time
  ON capture_events(event_kind, occurred_at);
CREATE INDEX idx_capture_events_stage
  ON capture_events(stage_key, event_kind, id);

CREATE TRIGGER capture_provenance_no_update
BEFORE UPDATE ON capture_provenance
BEGIN
  SELECT RAISE(ABORT, 'capture provenance is append-only');
END;

CREATE TRIGGER capture_provenance_no_delete
BEFORE DELETE ON capture_provenance
BEGIN
  SELECT RAISE(ABORT, 'capture provenance is append-only');
END;

CREATE TRIGGER capture_events_no_update
BEFORE UPDATE ON capture_events
BEGIN
  SELECT RAISE(ABORT, 'capture events are append-only');
END;

CREATE TRIGGER capture_events_no_delete
BEFORE DELETE ON capture_events
BEGIN
  SELECT RAISE(ABORT, 'capture events are append-only');
END;

CREATE TABLE capture_transport_consents (
  id INTEGER PRIMARY KEY,
  transport TEXT NOT NULL CHECK (transport IN ('telegram')),
  disclosure_version TEXT NOT NULL
    CHECK (
      length(disclosure_version) BETWEEN 1 AND 64
      AND disclosure_version NOT GLOB '*[^a-z0-9_.-]*'
    ),
  decision TEXT NOT NULL CHECK (decision IN ('consented', 'revoked')),
  occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_capture_transport_consents_latest
  ON capture_transport_consents(transport, id DESC);

CREATE TRIGGER capture_transport_consents_no_update
BEFORE UPDATE ON capture_transport_consents
BEGIN
  SELECT RAISE(ABORT, 'capture transport consent is append-only');
END;

CREATE TRIGGER capture_transport_consents_no_delete
BEFORE DELETE ON capture_transport_consents
BEGIN
  SELECT RAISE(ABORT, 'capture transport consent is append-only');
END;

-- Preserve existing FN-145 capture acknowledgements as local-only provenance.
-- Their original client acceptance time was not recorded, so the stored time
-- is used for both accepted and durable timestamps and the resulting zero
-- latency is excluded from client durable-ack metrics.
INSERT INTO capture_provenance(
  capture_id,
  source_document_id,
  channel,
  source,
  transport_class,
  dedup_kind,
  proof_run_id,
  device_cohort_id,
  accepted_at,
  durable_at
)
SELECT
  cs.client_capture_id,
  cs.source_document_id,
  CASE
    WHEN json_extract(cs.source_metadata_json, '$.source') = 'share'
      THEN 'share'
    ELSE 'web'
  END,
  CASE
    WHEN json_extract(cs.source_metadata_json, '$.source') IN (
      'camera', 'file', 'share', 'shortcut', 'web'
    )
      THEN json_extract(cs.source_metadata_json, '$.source')
    ELSE 'unspecified'
  END,
  'local_only',
  'new',
  '',
  '',
  cs.stored_at,
  cs.stored_at
FROM capture_submissions cs
WHERE cs.source_document_id IS NOT NULL;

INSERT INTO capture_events(
  event_key,
  capture_id,
  source_document_id,
  event_kind,
  stage_key,
  occurred_at,
  reason_code
)
SELECT
  cs.client_capture_id || ':accepted:0:legacy',
  cs.client_capture_id,
  cs.source_document_id,
  'accepted',
  'legacy_capture',
  cs.stored_at,
  'legacy'
FROM capture_submissions cs
WHERE cs.source_document_id IS NOT NULL;

INSERT INTO capture_events(
  event_key,
  capture_id,
  source_document_id,
  event_kind,
  stage_key,
  occurred_at,
  reason_code
)
SELECT
  cs.client_capture_id || ':durable:0:legacy',
  cs.client_capture_id,
  cs.source_document_id,
  'durable',
  'legacy_capture',
  cs.stored_at,
  'legacy'
FROM capture_submissions cs
WHERE cs.source_document_id IS NOT NULL;
