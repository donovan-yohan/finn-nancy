-- FN-147: immutable close-cycle truth and universal period-write policy.
--
-- `closed_periods` remains a compatibility projection for older reads. The
-- append-only tables below are the canonical close history. Currentness is
-- derived from events; reopening never edits or deletes a prior snapshot.

CREATE TABLE period_close_cycles (
  id INTEGER PRIMARY KEY,
  cycle_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(cycle_key)) BETWEEN 1 AND 200),
  month TEXT NOT NULL
    CHECK (
      month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
      AND CAST(substr(month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
    ),
  cycle_number INTEGER NOT NULL CHECK (cycle_number > 0),
  prior_cycle_id INTEGER
    REFERENCES period_close_cycles(id) ON DELETE RESTRICT,
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (month, cycle_number),
  CHECK (prior_cycle_id IS NULL OR prior_cycle_id <> id)
);

CREATE INDEX idx_period_close_cycles_month
  ON period_close_cycles(month, cycle_number DESC, id DESC);

CREATE TABLE period_close_snapshots (
  id INTEGER PRIMARY KEY,
  snapshot_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(snapshot_key)) BETWEEN 1 AND 200),
  cycle_id INTEGER NOT NULL UNIQUE
    REFERENCES period_close_cycles(id) ON DELETE RESTRICT,
  snapshot_number INTEGER NOT NULL CHECK (snapshot_number > 0),
  close_state TEXT NOT NULL
    CHECK (close_state IN ('clean_closed', 'closed_with_exceptions')),
  exception_count INTEGER NOT NULL CHECK (exception_count >= 0),
  request_exception_digest TEXT NOT NULL
    CHECK (
      length(request_exception_digest)=64
      AND request_exception_digest NOT GLOB '*[^0-9a-f]*'
    ),
  exception_digest TEXT NOT NULL
    CHECK (
      length(exception_digest)=64
      AND exception_digest NOT GLOB '*[^0-9a-f]*'
    ),
  request_snapshot_digest TEXT NOT NULL
    CHECK (
      length(request_snapshot_digest)=64
      AND request_snapshot_digest NOT GLOB '*[^0-9a-f]*'
    ),
  integrity_status TEXT NOT NULL DEFAULT 'verified_sha256'
    CHECK (integrity_status IN ('verified_sha256', 'unverified_legacy')),
  snapshot_json TEXT NOT NULL
    CHECK (json_valid(snapshot_json) AND json_type(snapshot_json)='object'),
  snapshot_digest TEXT NOT NULL
    CHECK (
      length(snapshot_digest)=64
      AND snapshot_digest NOT GLOB '*[^0-9a-f]*'
    ),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (close_state='clean_closed' AND exception_count=0)
    OR (close_state='closed_with_exceptions' AND exception_count>0)
  )
);

CREATE TABLE period_close_events (
  id INTEGER PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(event_key)) BETWEEN 1 AND 200),
  cycle_id INTEGER NOT NULL
    REFERENCES period_close_cycles(id) ON DELETE RESTRICT,
  event_kind TEXT NOT NULL
    CHECK (
      event_kind IN (
        'legacy_imported', 'clean_closed', 'closed_with_exceptions',
        'reopened', 'override_reopened'
      )
    ),
  from_state TEXT NOT NULL
    CHECK (
      from_state IN (
        'open', 'clean_closed', 'closed_with_exceptions', 'reopened'
      )
    ),
  to_state TEXT NOT NULL
    CHECK (
      to_state IN (
        'open', 'clean_closed', 'closed_with_exceptions', 'reopened'
      )
    ),
  snapshot_id INTEGER
    REFERENCES period_close_snapshots(id) ON DELETE RESTRICT,
  actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  affected_ids_json TEXT NOT NULL DEFAULT '{}'
    CHECK (
      json_valid(affected_ids_json)
      AND json_type(affected_ids_json)='object'
    ),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
  evidence_digest TEXT NOT NULL
    CHECK (
      length(evidence_digest)=64
      AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
  integrity_status TEXT NOT NULL DEFAULT 'verified_sha256'
    CHECK (integrity_status IN ('verified_sha256', 'unverified_legacy')),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  reverses_event_id INTEGER
    REFERENCES period_close_events(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (reverses_event_id IS NULL OR reverses_event_id <> id),
  CHECK (
    (
      event_kind IN ('clean_closed', 'closed_with_exceptions')
      AND to_state=event_kind
      AND snapshot_id IS NOT NULL
      AND reverses_event_id IS NULL
    )
    OR (
      event_kind IN ('reopened', 'override_reopened')
      AND from_state IN ('clean_closed', 'closed_with_exceptions')
      AND to_state='reopened'
      AND snapshot_id IS NULL
    )
    OR (
      event_kind='legacy_imported'
      AND from_state='open'
      AND (
        (to_state IN ('open', 'reopened') AND snapshot_id IS NULL)
        OR (
          to_state='closed_with_exceptions'
          AND snapshot_id IS NOT NULL
        )
      )
    )
  )
);

CREATE INDEX idx_period_close_events_cycle
  ON period_close_events(cycle_id, id DESC);
CREATE UNIQUE INDEX uq_period_close_events_reversal
  ON period_close_events(reverses_event_id)
  WHERE reverses_event_id IS NOT NULL;

CREATE TABLE period_close_exceptions (
  id INTEGER PRIMARY KEY,
  exception_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(exception_key)) BETWEEN 1 AND 240),
  cycle_id INTEGER NOT NULL
    REFERENCES period_close_cycles(id) ON DELETE RESTRICT,
  lineage_key TEXT NOT NULL
    CHECK (length(trim(lineage_key)) BETWEEN 1 AND 200),
  prior_exception_id INTEGER
    REFERENCES period_close_exceptions(id) ON DELETE RESTRICT,
  exception_type TEXT NOT NULL
    CHECK (
      exception_type IN (
        'missing_statement', 'unresolved_line',
        'unclassified_positive_flow', 'unconfirmed_merchant_category',
        'unexplained_balance_delta', 'evidence_gap', 'manual_adjustment'
      )
    ),
  subject_kind TEXT NOT NULL
    CHECK (length(trim(subject_kind)) BETWEEN 1 AND 80),
  subject_id TEXT NOT NULL DEFAULT '' CHECK (length(subject_id) <= 160),
  affected_ids_json TEXT NOT NULL DEFAULT '{}'
    CHECK (
      json_valid(affected_ids_json)
      AND json_type(affected_ids_json)='object'
    ),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
  evidence_digest TEXT NOT NULL
    CHECK (
      length(evidence_digest)=64
      AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
  integrity_status TEXT NOT NULL DEFAULT 'verified_sha256'
    CHECK (integrity_status IN ('verified_sha256', 'unverified_legacy')),
  amount_cents INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  resolution_href TEXT NOT NULL DEFAULT '' CHECK (length(resolution_href) <= 500),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) BETWEEN 1 AND 160),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (cycle_id, lineage_key),
  CHECK (prior_exception_id IS NULL OR prior_exception_id <> id)
);

CREATE INDEX idx_period_close_exceptions_cycle
  ON period_close_exceptions(cycle_id, exception_type, id);
CREATE INDEX idx_period_close_exceptions_lineage
  ON period_close_exceptions(lineage_key, id DESC);

CREATE TABLE period_close_preacknowledgements (
  id INTEGER PRIMARY KEY,
  acknowledgement_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(acknowledgement_key)) BETWEEN 1 AND 240),
  month TEXT NOT NULL
    CHECK (
      month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
      AND CAST(substr(month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
    ),
  exception_token TEXT NOT NULL
    CHECK (length(trim(exception_token)) BETWEEN 1 AND 100),
  event_kind TEXT NOT NULL
    CHECK (event_kind IN ('acknowledged', 'withdrawn')),
  exception_json TEXT NOT NULL
    CHECK (json_valid(exception_json) AND json_type(exception_json)='object'),
  exception_digest TEXT NOT NULL
    CHECK (
      length(exception_digest)=64
      AND exception_digest NOT GLOB '*[^0-9a-f]*'
    ),
  actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
  evidence_digest TEXT NOT NULL
    CHECK (
      length(evidence_digest)=64
      AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  reverses_acknowledgement_id INTEGER
    REFERENCES period_close_preacknowledgements(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (event_kind='acknowledged' AND reverses_acknowledgement_id IS NULL)
    OR (
      event_kind='withdrawn'
      AND reverses_acknowledgement_id IS NOT NULL
      AND reverses_acknowledgement_id <> id
    )
  )
);

CREATE INDEX idx_period_close_preacks_month_token
  ON period_close_preacknowledgements(month, exception_token, id DESC);
CREATE UNIQUE INDEX uq_period_close_preack_reversal
  ON period_close_preacknowledgements(reverses_acknowledgement_id)
  WHERE reverses_acknowledgement_id IS NOT NULL;

CREATE TABLE period_close_acknowledgements (
  id INTEGER PRIMARY KEY,
  acknowledgement_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(acknowledgement_key)) BETWEEN 1 AND 240),
  exception_id INTEGER NOT NULL
    REFERENCES period_close_exceptions(id) ON DELETE RESTRICT,
  exception_token TEXT NOT NULL
    CHECK (length(trim(exception_token)) BETWEEN 1 AND 100),
  event_kind TEXT NOT NULL
    CHECK (event_kind IN ('acknowledged', 'withdrawn')),
  actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
  evidence_digest TEXT NOT NULL
    CHECK (
      length(evidence_digest)=64
      AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  reverses_acknowledgement_id INTEGER
    REFERENCES period_close_acknowledgements(id) ON DELETE RESTRICT,
  source_preclose_acknowledgement_id INTEGER UNIQUE
    REFERENCES period_close_preacknowledgements(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (event_kind='acknowledged' AND reverses_acknowledgement_id IS NULL)
    OR (
      event_kind='withdrawn'
      AND reverses_acknowledgement_id IS NOT NULL
      AND reverses_acknowledgement_id <> id
    )
  )
);

CREATE UNIQUE INDEX uq_period_close_ack_reversal
  ON period_close_acknowledgements(reverses_acknowledgement_id)
  WHERE reverses_acknowledgement_id IS NOT NULL;

CREATE TABLE period_close_resolutions (
  id INTEGER PRIMARY KEY,
  resolution_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(resolution_key)) BETWEEN 1 AND 240),
  exception_id INTEGER NOT NULL
    REFERENCES period_close_exceptions(id) ON DELETE RESTRICT,
  event_kind TEXT NOT NULL CHECK (event_kind IN ('resolved', 'reversed')),
  actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
  evidence_digest TEXT NOT NULL
    CHECK (
      length(evidence_digest)=64
      AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  reverses_resolution_id INTEGER
    REFERENCES period_close_resolutions(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (event_kind='resolved' AND reverses_resolution_id IS NULL)
    OR (
      event_kind='reversed'
      AND reverses_resolution_id IS NOT NULL
      AND reverses_resolution_id <> id
    )
  )
);

CREATE UNIQUE INDEX uq_period_close_resolution_reversal
  ON period_close_resolutions(reverses_resolution_id)
  WHERE reverses_resolution_id IS NOT NULL;

CREATE TABLE period_close_reopens (
  id INTEGER PRIMARY KEY,
  reopen_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(reopen_key)) BETWEEN 1 AND 240),
  cycle_id INTEGER NOT NULL
    REFERENCES period_close_cycles(id) ON DELETE RESTRICT,
  event_id INTEGER NOT NULL UNIQUE
    REFERENCES period_close_events(id) ON DELETE RESTRICT,
  invalidated_snapshot_id INTEGER
    REFERENCES period_close_snapshots(id) ON DELETE RESTRICT,
  reopen_kind TEXT NOT NULL CHECK (reopen_kind IN ('explicit', 'override')),
  actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
  evidence_digest TEXT NOT NULL
    CHECK (
      length(evidence_digest)=64
      AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE period_write_overrides (
  id INTEGER PRIMARY KEY,
  override_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(override_key)) BETWEEN 1 AND 240),
  request_operation_key TEXT NOT NULL
    CHECK (length(trim(request_operation_key)) BETWEEN 1 AND 240),
  month TEXT NOT NULL
    CHECK (
      month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
      AND CAST(substr(month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
    ),
  cycle_id INTEGER NOT NULL
    REFERENCES period_close_cycles(id) ON DELETE RESTRICT,
  reopen_id INTEGER NOT NULL UNIQUE
    REFERENCES period_close_reopens(id) ON DELETE RESTRICT,
  actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  affected_ids_json TEXT NOT NULL
    CHECK (
      json_valid(affected_ids_json)
      AND json_type(affected_ids_json)='object'
    ),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json)='object'),
  evidence_digest TEXT NOT NULL
    CHECK (
      length(evidence_digest)=64
      AND evidence_digest NOT GLOB '*[^0-9a-f]*'
    ),
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (request_operation_key, month)
);

-- The latest event, not a mutable flag, defines current close state.
CREATE VIEW v_current_period_close_state AS
SELECT
  cycle.month,
  cycle.id AS cycle_id,
  cycle.cycle_number,
  event.id AS event_id,
  event.event_kind,
  event.to_state AS state,
  event.snapshot_id,
  event.actor,
  event.reason,
  event.created_at AS state_changed_at
FROM period_close_events event
JOIN period_close_cycles cycle ON cycle.id=event.cycle_id
WHERE NOT EXISTS (
  SELECT 1
  FROM period_close_events later_event
  JOIN period_close_cycles later_cycle
    ON later_cycle.id=later_event.cycle_id
  WHERE later_cycle.month=cycle.month
    AND (
      later_cycle.cycle_number > cycle.cycle_number
      OR (
        later_cycle.cycle_number = cycle.cycle_number
        AND later_event.id > event.id
      )
    )
);

CREATE VIEW v_current_period_close_snapshot AS
SELECT
  state.month,
  state.cycle_id,
  state.cycle_number,
  state.state,
  snapshot.id AS snapshot_id,
  snapshot.snapshot_number,
  snapshot.exception_count,
  snapshot.request_exception_digest,
  snapshot.exception_digest,
  snapshot.request_snapshot_digest,
  snapshot.snapshot_json,
  snapshot.snapshot_digest,
  snapshot.integrity_status,
  snapshot.created_by,
  snapshot.reason,
  snapshot.created_at
FROM v_current_period_close_state state
JOIN period_close_snapshots snapshot ON snapshot.id=state.snapshot_id
WHERE state.state IN ('clean_closed', 'closed_with_exceptions');

CREATE VIEW v_period_close_snapshot_history AS
SELECT
  cycle.month,
  cycle.cycle_number,
  snapshot.id AS snapshot_id,
  snapshot.snapshot_number,
  snapshot.close_state,
  snapshot.exception_count,
  snapshot.request_exception_digest,
  snapshot.exception_digest,
  snapshot.request_snapshot_digest,
  snapshot.snapshot_json,
  snapshot.snapshot_digest,
  snapshot.integrity_status,
  snapshot.created_by,
  snapshot.reason,
  snapshot.created_at,
  CASE WHEN current.snapshot_id=snapshot.id THEN 1 ELSE 0 END AS is_current
FROM period_close_snapshots snapshot
JOIN period_close_cycles cycle ON cycle.id=snapshot.cycle_id
LEFT JOIN v_current_period_close_snapshot current
  ON current.snapshot_id=snapshot.id;

CREATE VIEW v_period_policy_locked_months AS
SELECT month, cycle_id, cycle_number, state, snapshot_id
FROM v_current_period_close_state
WHERE state IN ('clean_closed', 'closed_with_exceptions');

CREATE VIEW v_period_close_current_acknowledgements AS
SELECT acknowledgement.*
FROM period_close_acknowledgements acknowledgement
WHERE acknowledgement.event_kind='acknowledged'
  AND NOT EXISTS (
    SELECT 1
    FROM period_close_acknowledgements withdrawal
    WHERE withdrawal.reverses_acknowledgement_id=acknowledgement.id
      AND withdrawal.event_kind='withdrawn'
  );

CREATE VIEW v_period_close_current_preacknowledgements AS
SELECT acknowledgement.*
FROM period_close_preacknowledgements acknowledgement
WHERE acknowledgement.event_kind='acknowledged'
  AND NOT EXISTS (
    SELECT 1
    FROM period_close_preacknowledgements withdrawal
    WHERE withdrawal.reverses_acknowledgement_id=acknowledgement.id
      AND withdrawal.event_kind='withdrawn'
  )
  AND NOT EXISTS (
    SELECT 1
    FROM period_close_acknowledgements frozen
    WHERE frozen.source_preclose_acknowledgement_id=acknowledgement.id
  );

CREATE VIEW v_period_close_active_exceptions AS
SELECT exception.*
FROM period_close_exceptions exception
JOIN v_current_period_close_state state
  ON state.cycle_id=exception.cycle_id
WHERE state.state IN ('clean_closed', 'closed_with_exceptions')
  AND NOT EXISTS (
    SELECT 1
    FROM period_close_resolutions resolution
    WHERE resolution.exception_id=exception.id
      AND resolution.event_kind='resolved'
      AND NOT EXISTS (
        SELECT 1
        FROM period_close_resolutions reversal
        WHERE reversal.reverses_resolution_id=resolution.id
          AND reversal.event_kind='reversed'
      )
  );

-- Conservative legacy import: an old generic `closed` row cannot prove the
-- stricter FN-147 clean-close matrix, so it becomes closed_with_exceptions.
INSERT INTO period_close_cycles(
  cycle_key, month, cycle_number, created_by, reason, operation_key
)
SELECT
  'legacy-cycle:' || period.month,
  period.month,
  1,
  'migration:036',
  'preserve legacy close lifecycle',
  'migration:036:cycle:' || period.month
FROM closed_periods period;

INSERT INTO period_close_snapshots(
  snapshot_key, cycle_id, snapshot_number, close_state, exception_count,
  request_exception_digest, exception_digest, request_snapshot_digest,
  snapshot_json, snapshot_digest, integrity_status, created_by, reason,
  operation_key
)
SELECT
  'legacy-snapshot:' || period.month,
  cycle.id,
  1,
  'closed_with_exceptions',
  1,
  lower(printf('%064x', cycle.id + 35900000)),
  lower(printf('%064x', cycle.id + 35900000)),
  lower(printf('%064x', cycle.id + 35800000)),
  CASE
    WHEN json_valid(period.summary_json)
      AND json_type(period.summary_json)='object'
    THEN period.summary_json
    ELSE '{}'
  END,
  lower(printf('%064x', cycle.id + 36000000)),
  'unverified_legacy',
  'migration:036',
  'legacy close cannot prove clean FN-147 criteria',
  'migration:036:snapshot:' || period.month
FROM closed_periods period
JOIN period_close_cycles cycle ON cycle.month=period.month
WHERE period.status='closed';

INSERT INTO period_close_exceptions(
  exception_key, cycle_id, lineage_key, exception_type, subject_kind, subject_id,
  affected_ids_json, evidence_json, evidence_digest, reason,
  integrity_status, resolution_href, created_by, operation_key
)
SELECT
  'legacy-exception:' || period.month,
  cycle.id,
  'legacy:' || period.month,
  'evidence_gap',
  'legacy_close',
  period.month,
  json_object('period_month', period.month, 'legacy_period_id', period.id),
  json_object('legacy_status', period.status),
  lower(printf('%064x', cycle.id + 36100000)),
  'legacy close lacks the FN-147 evidence and authority receipt',
  'unverified_legacy',
  '/close?month=' || period.month,
  'migration:036',
  'migration:036:exception:' || period.month
FROM closed_periods period
JOIN period_close_cycles cycle ON cycle.month=period.month
WHERE period.status='closed';

INSERT INTO period_close_events(
  event_key, cycle_id, event_kind, from_state, to_state, snapshot_id,
  actor, reason, affected_ids_json, evidence_json, evidence_digest,
  integrity_status, operation_key
)
SELECT
  'legacy-event:' || period.month,
  cycle.id,
  'legacy_imported',
  'open',
  CASE period.status
    WHEN 'closed' THEN 'closed_with_exceptions'
    WHEN 'reopened' THEN 'reopened'
    ELSE 'open'
  END,
  snapshot.id,
  'migration:036',
  'preserve legacy close state conservatively',
  json_object('period_month', period.month, 'legacy_period_id', period.id),
  json_object('legacy_status', period.status),
  lower(printf('%064x', cycle.id + 36200000)),
  'unverified_legacy',
  'migration:036:event:' || period.month
FROM closed_periods period
JOIN period_close_cycles cycle ON cycle.month=period.month
LEFT JOIN period_close_snapshots snapshot ON snapshot.cycle_id=cycle.id;

-- New close events cannot claim a cleaner state than their frozen rows, and
-- every exception close must already have one current durable acknowledgement
-- per exception in the same transaction. Legacy imports above remain explicit
-- unverified history and are not relabeled as newly verified closes.
CREATE TRIGGER period_close_events_close_invariant
BEFORE INSERT ON period_close_events
WHEN NEW.event_kind IN ('clean_closed', 'closed_with_exceptions')
  AND (
    NOT EXISTS (
      SELECT 1
      FROM period_close_snapshots snapshot
      WHERE snapshot.id=NEW.snapshot_id
        AND snapshot.cycle_id=NEW.cycle_id
        AND snapshot.close_state=NEW.event_kind
        AND snapshot.exception_count=(
          SELECT COUNT(*)
          FROM period_close_exceptions exception
          WHERE exception.cycle_id=NEW.cycle_id
        )
    )
    OR (
      NEW.event_kind='closed_with_exceptions'
      AND EXISTS (
        SELECT 1
        FROM period_close_exceptions exception
        WHERE exception.cycle_id=NEW.cycle_id
          AND NOT EXISTS (
            SELECT 1
            FROM v_period_close_current_acknowledgements acknowledgement
            WHERE acknowledgement.exception_id=exception.id
          )
      )
    )
  )
BEGIN
  SELECT RAISE(
    ABORT,
    'period close requires exact state and durable exception acknowledgements'
  );
END;

-- New lifecycle evidence is append-only. Corrections are compensating rows.
CREATE TRIGGER period_close_cycles_immutable_update
BEFORE UPDATE ON period_close_cycles
BEGIN
  SELECT RAISE(ABORT, 'period close cycles are append-only');
END;
CREATE TRIGGER period_close_cycles_immutable_delete
BEFORE DELETE ON period_close_cycles
BEGIN
  SELECT RAISE(ABORT, 'period close cycles are append-only');
END;

CREATE TRIGGER period_close_snapshots_immutable_update
BEFORE UPDATE ON period_close_snapshots
BEGIN
  SELECT RAISE(ABORT, 'period close snapshots are append-only');
END;
CREATE TRIGGER period_close_snapshots_immutable_delete
BEFORE DELETE ON period_close_snapshots
BEGIN
  SELECT RAISE(ABORT, 'period close snapshots are append-only');
END;

CREATE TRIGGER period_close_events_immutable_update
BEFORE UPDATE ON period_close_events
BEGIN
  SELECT RAISE(ABORT, 'period close events are append-only');
END;
CREATE TRIGGER period_close_events_immutable_delete
BEFORE DELETE ON period_close_events
BEGIN
  SELECT RAISE(ABORT, 'period close events are append-only');
END;

CREATE TRIGGER period_close_exceptions_immutable_update
BEFORE UPDATE ON period_close_exceptions
BEGIN
  SELECT RAISE(ABORT, 'period close exceptions are append-only');
END;
CREATE TRIGGER period_close_exceptions_immutable_delete
BEFORE DELETE ON period_close_exceptions
BEGIN
  SELECT RAISE(ABORT, 'period close exceptions are append-only');
END;

CREATE TRIGGER period_close_preacknowledgements_immutable_update
BEFORE UPDATE ON period_close_preacknowledgements
BEGIN
  SELECT RAISE(ABORT, 'period close preacknowledgements are append-only');
END;
CREATE TRIGGER period_close_preacknowledgements_immutable_delete
BEFORE DELETE ON period_close_preacknowledgements
BEGIN
  SELECT RAISE(ABORT, 'period close preacknowledgements are append-only');
END;

CREATE TRIGGER period_close_acknowledgements_immutable_update
BEFORE UPDATE ON period_close_acknowledgements
BEGIN
  SELECT RAISE(ABORT, 'period close acknowledgements are append-only');
END;
CREATE TRIGGER period_close_acknowledgements_immutable_delete
BEFORE DELETE ON period_close_acknowledgements
BEGIN
  SELECT RAISE(ABORT, 'period close acknowledgements are append-only');
END;

CREATE TRIGGER period_close_resolutions_immutable_update
BEFORE UPDATE ON period_close_resolutions
BEGIN
  SELECT RAISE(ABORT, 'period close resolutions are append-only');
END;
CREATE TRIGGER period_close_resolutions_immutable_delete
BEFORE DELETE ON period_close_resolutions
BEGIN
  SELECT RAISE(ABORT, 'period close resolutions are append-only');
END;

CREATE TRIGGER period_close_reopens_immutable_update
BEFORE UPDATE ON period_close_reopens
BEGIN
  SELECT RAISE(ABORT, 'period close reopens are append-only');
END;
CREATE TRIGGER period_close_reopens_immutable_delete
BEFORE DELETE ON period_close_reopens
BEGIN
  SELECT RAISE(ABORT, 'period close reopens are append-only');
END;

CREATE TRIGGER period_write_overrides_immutable_update
BEFORE UPDATE ON period_write_overrides
BEGIN
  SELECT RAISE(ABORT, 'period write overrides are append-only');
END;
CREATE TRIGGER period_write_overrides_immutable_delete
BEFORE DELETE ON period_write_overrides
BEGIN
  SELECT RAISE(ABORT, 'period write overrides are append-only');
END;

-- Fail-closed database boundary.  Application repositories provide typed
-- errors and audited reopen capabilities; these triggers are the last line of
-- defense for queued jobs, admin paths, cascades, and future writers.
CREATE TRIGGER period_policy_guard_transactions_insert
BEFORE INSERT ON transactions
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month=substr(NEW.posted_on, 1, 7)
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transactions_update
BEFORE UPDATE ON transactions
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(OLD.posted_on, 1, 7), substr(NEW.posted_on, 1, 7)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transactions_delete
BEFORE DELETE ON transactions
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month=substr(OLD.posted_on, 1, 7)
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transaction_splits_insert
BEFORE INSERT ON transaction_splits
WHEN EXISTS (
  SELECT 1
  FROM transactions transaction_row
  JOIN v_period_policy_locked_months locked
    ON locked.month=substr(transaction_row.posted_on, 1, 7)
  WHERE transaction_row.id=NEW.transaction_id
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transaction_splits_update
BEFORE UPDATE ON transaction_splits
WHEN EXISTS (
  SELECT 1
  FROM transactions transaction_row
  JOIN v_period_policy_locked_months locked
    ON locked.month=substr(transaction_row.posted_on, 1, 7)
  WHERE transaction_row.id IN (OLD.transaction_id, NEW.transaction_id)
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transaction_splits_delete
BEFORE DELETE ON transaction_splits
WHEN EXISTS (
  SELECT 1
  FROM transactions transaction_row
  JOIN v_period_policy_locked_months locked
    ON locked.month=substr(transaction_row.posted_on, 1, 7)
  WHERE transaction_row.id=OLD.transaction_id
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transaction_relationships_insert
BEFORE INSERT ON transaction_relationships
WHEN EXISTS (
  SELECT 1
  FROM transactions transaction_row
  JOIN v_period_policy_locked_months locked
    ON locked.month=substr(transaction_row.posted_on, 1, 7)
  WHERE transaction_row.id IN (
    NEW.source_transaction_id, NEW.target_transaction_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transaction_relationships_update
BEFORE UPDATE ON transaction_relationships
WHEN EXISTS (
  SELECT 1
  FROM transactions transaction_row
  JOIN v_period_policy_locked_months locked
    ON locked.month=substr(transaction_row.posted_on, 1, 7)
  WHERE transaction_row.id IN (
    OLD.source_transaction_id, OLD.target_transaction_id,
    NEW.source_transaction_id, NEW.target_transaction_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transaction_flow_reviews_update
BEFORE UPDATE ON transaction_flow_reviews
WHEN EXISTS (
  SELECT 1
  FROM transactions transaction_row
  JOIN v_period_policy_locked_months locked
    ON locked.month=substr(transaction_row.posted_on, 1, 7)
  WHERE transaction_row.id IN (OLD.transaction_id, NEW.transaction_id)
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_transaction_flow_audit_insert
BEFORE INSERT ON transaction_flow_audit
WHEN EXISTS (
  SELECT 1
  FROM transactions transaction_row
  JOIN v_period_policy_locked_months locked
    ON locked.month=substr(transaction_row.posted_on, 1, 7)
  WHERE transaction_row.id=NEW.transaction_id
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_lines_insert
BEFORE INSERT ON statement_lines
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(NEW.posted_on, 1, 7),
    CASE
      WHEN length(NEW.statement_period) >= 7
      THEN substr(NEW.statement_period, 1, 7)
    END
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_lines_update
BEFORE UPDATE ON statement_lines
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(OLD.posted_on, 1, 7), substr(NEW.posted_on, 1, 7),
    CASE
      WHEN length(OLD.statement_period) >= 7
      THEN substr(OLD.statement_period, 1, 7)
    END,
    CASE
      WHEN length(NEW.statement_period) >= 7
      THEN substr(NEW.statement_period, 1, 7)
    END
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_lines_delete
BEFORE DELETE ON statement_lines
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(OLD.posted_on, 1, 7),
    CASE
      WHEN length(OLD.statement_period) >= 7
      THEN substr(OLD.statement_period, 1, 7)
    END
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_reviews_insert
BEFORE INSERT ON statement_reviews
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    NEW.period_month,
    substr(NEW.period_start_on, 1, 7),
    substr(NEW.period_end_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM statement_lines line
    WHERE line.source_document_id=NEW.source_document_id
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_reviews_update
BEFORE UPDATE ON statement_reviews
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    OLD.period_month, NEW.period_month,
    substr(OLD.period_start_on, 1, 7),
    substr(OLD.period_end_on, 1, 7),
    substr(NEW.period_start_on, 1, 7),
    substr(NEW.period_end_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM statement_lines line
    WHERE line.source_document_id IN (
      OLD.source_document_id, NEW.source_document_id
    )
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_review_pages_insert
BEFORE INSERT ON statement_review_pages
WHEN EXISTS (
  SELECT 1
  FROM statement_reviews review
  JOIN v_period_policy_locked_months locked
    ON locked.month IN (
      review.period_month,
      substr(review.period_start_on, 1, 7),
      substr(review.period_end_on, 1, 7)
    )
  WHERE review.id=NEW.statement_review_id
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_source_anchors_insert
BEFORE INSERT ON statement_source_anchors
WHEN EXISTS (
  SELECT 1
  FROM statement_reviews review
  JOIN v_period_policy_locked_months locked
    ON locked.month IN (
      review.period_month,
      substr(review.period_start_on, 1, 7),
      substr(review.period_end_on, 1, 7)
    )
  WHERE review.id=NEW.statement_review_id
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_field_evidence_insert
BEFORE INSERT ON statement_field_evidence
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE EXISTS (
    SELECT 1
    FROM statement_reviews review
    WHERE review.id=NEW.statement_review_id
      AND locked.month IN (
        review.period_month,
        substr(review.period_start_on, 1, 7),
        substr(review.period_end_on, 1, 7)
      )
  )
  OR EXISTS (
    SELECT 1
    FROM statement_lines line
    WHERE line.id=NEW.statement_line_id
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_review_audit_insert
BEFORE INSERT ON statement_review_audit
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE EXISTS (
    SELECT 1
    FROM statement_reviews review
    WHERE review.id=NEW.statement_review_id
      AND locked.month IN (
        review.period_month,
        substr(review.period_start_on, 1, 7),
        substr(review.period_end_on, 1, 7)
      )
  )
  OR EXISTS (
    SELECT 1
    FROM statement_lines line
    WHERE line.id=NEW.statement_line_id
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_account_statement_policies_insert
BEFORE INSERT ON account_statement_policies
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month >= NEW.effective_from_month
    AND (
      NEW.active_from IS NULL
      OR locked.month >= substr(NEW.active_from, 1, 7)
    )
    AND (
      NEW.active_to IS NULL
      OR locked.month <= substr(NEW.active_to, 1, 7)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_account_statement_expectations_insert
BEFORE INSERT ON account_statement_expectations
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month=NEW.period_month
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_account_statement_expectations_update
BEFORE UPDATE ON account_statement_expectations
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (OLD.period_month, NEW.period_month)
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_expectation_documents_insert
BEFORE INSERT ON statement_expectation_documents
WHEN EXISTS (
  SELECT 1
  FROM account_statement_expectations expectation
  JOIN v_period_policy_locked_months locked
    ON locked.month=expectation.period_month
  WHERE expectation.id=NEW.expectation_id
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_expectation_documents_update
BEFORE UPDATE ON statement_expectation_documents
WHEN EXISTS (
  SELECT 1
  FROM account_statement_expectations expectation
  JOIN v_period_policy_locked_months locked
    ON locked.month=expectation.period_month
  WHERE expectation.id IN (OLD.expectation_id, NEW.expectation_id)
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_statement_expectation_audit_insert
BEFORE INSERT ON statement_expectation_audit
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month=NEW.period_month
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_account_balance_assertions_insert
BEFORE INSERT ON account_balance_assertions
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(NEW.asof_date, 1, 7),
    substr(NEW.statement_period, 1, 7)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_account_balance_assertions_update
BEFORE UPDATE ON account_balance_assertions
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(OLD.asof_date, 1, 7), substr(NEW.asof_date, 1, 7),
    substr(OLD.statement_period, 1, 7),
    substr(NEW.statement_period, 1, 7)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_account_balance_assertions_delete
BEFORE DELETE ON account_balance_assertions
WHEN EXISTS (
  SELECT 1 FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(OLD.asof_date, 1, 7),
    substr(OLD.statement_period, 1, 7)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_positive_flow_decision_events_insert
BEFORE INSERT ON positive_flow_decision_events
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month=NEW.selected_statement_month
  OR EXISTS (
    SELECT 1
    FROM transactions transaction_row
    WHERE transaction_row.id IN (
      NEW.subject_transaction_id, NEW.candidate_transaction_id
    )
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1
    FROM statement_lines line
    WHERE line.id=NEW.statement_line_id
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_merchant_resolution_events_insert
BEFORE INSERT ON merchant_resolution_events
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE EXISTS (
    SELECT 1
    FROM transactions transaction_row
    WHERE transaction_row.id=NEW.transaction_id
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1
    FROM transaction_splits split
    JOIN transactions transaction_row ON transaction_row.id=split.transaction_id
    WHERE split.id=NEW.transaction_split_id
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1
    FROM statement_lines line
    WHERE line.id=NEW.statement_line_id
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
  OR EXISTS (
    SELECT 1
    FROM proposed_actions proposal
    LEFT JOIN transactions transaction_row
      ON transaction_row.id=json_extract(proposal.payload_json, '$.transaction_id')
    LEFT JOIN statement_lines line
      ON line.id=json_extract(proposal.payload_json, '$.statement_line_id')
    WHERE proposal.id=NEW.proposed_action_id
      AND locked.month IN (
        substr(transaction_row.posted_on, 1, 7),
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7),
        json_extract(proposal.payload_json, '$.month'),
        json_extract(proposal.payload_json, '$.period_month')
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_proposed_actions_insert
BEFORE INSERT ON proposed_actions
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    json_extract(NEW.payload_json, '$.month'),
    json_extract(NEW.payload_json, '$.period_month'),
    substr(json_extract(NEW.payload_json, '$.posted_on'), 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM transactions transaction_row
    WHERE transaction_row.id=json_extract(NEW.payload_json, '$.transaction_id')
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM statement_lines line
    WHERE line.id=json_extract(NEW.payload_json, '$.statement_line_id')
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
  OR EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.transaction_ids') item
    JOIN transactions transaction_row
      ON transaction_row.id=CAST(item.value AS INTEGER)
    WHERE locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.statement_line_ids') item
    JOIN statement_lines line ON line.id=CAST(item.value AS INTEGER)
    WHERE locked.month IN (
      substr(line.posted_on, 1, 7),
      substr(line.statement_period, 1, 7)
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_proposed_actions_update
BEFORE UPDATE ON proposed_actions
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    json_extract(OLD.payload_json, '$.month'),
    json_extract(OLD.payload_json, '$.period_month'),
    substr(json_extract(OLD.payload_json, '$.posted_on'), 1, 7),
    json_extract(NEW.payload_json, '$.month'),
    json_extract(NEW.payload_json, '$.period_month'),
    substr(json_extract(NEW.payload_json, '$.posted_on'), 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM transactions transaction_row
    WHERE transaction_row.id IN (
      json_extract(OLD.payload_json, '$.transaction_id'),
      json_extract(NEW.payload_json, '$.transaction_id')
    )
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM statement_lines line
    WHERE line.id IN (
      json_extract(OLD.payload_json, '$.statement_line_id'),
      json_extract(NEW.payload_json, '$.statement_line_id')
    )
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
  OR EXISTS (
    SELECT 1
    FROM (
      SELECT value FROM json_each(OLD.payload_json, '$.transaction_ids')
      UNION ALL
      SELECT value FROM json_each(NEW.payload_json, '$.transaction_ids')
    ) item
    JOIN transactions transaction_row
      ON transaction_row.id=CAST(item.value AS INTEGER)
    WHERE locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1
    FROM (
      SELECT value FROM json_each(OLD.payload_json, '$.statement_line_ids')
      UNION ALL
      SELECT value FROM json_each(NEW.payload_json, '$.statement_line_ids')
    ) item
    JOIN statement_lines line ON line.id=CAST(item.value AS INTEGER)
    WHERE locked.month IN (
      substr(line.posted_on, 1, 7),
      substr(line.statement_period, 1, 7)
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_proposed_action_audit_insert
BEFORE INSERT ON proposed_action_audit
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  JOIN proposed_actions proposal ON proposal.id=NEW.proposed_action_id
  WHERE locked.month IN (
    json_extract(proposal.payload_json, '$.month'),
    json_extract(proposal.payload_json, '$.period_month'),
    substr(json_extract(proposal.payload_json, '$.posted_on'), 1, 7),
    json_extract(NEW.payload_snapshot_json, '$.month'),
    json_extract(NEW.payload_snapshot_json, '$.period_month'),
    substr(json_extract(NEW.payload_snapshot_json, '$.posted_on'), 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM transactions transaction_row
    WHERE transaction_row.id IN (
      json_extract(proposal.payload_json, '$.transaction_id'),
      json_extract(NEW.payload_snapshot_json, '$.transaction_id')
    )
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1 FROM statement_lines line
    WHERE line.id IN (
      json_extract(proposal.payload_json, '$.statement_line_id'),
      json_extract(NEW.payload_snapshot_json, '$.statement_line_id')
    )
      AND locked.month IN (
        substr(line.posted_on, 1, 7),
        substr(line.statement_period, 1, 7)
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_goal_ledger_insert
BEFORE INSERT ON goal_ledger
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month=NEW.month
  OR EXISTS (
    SELECT 1 FROM transactions transaction_row
    WHERE transaction_row.id=NEW.transaction_id
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_goal_ledger_update
BEFORE UPDATE ON goal_ledger
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month IN (OLD.month, NEW.month)
  OR EXISTS (
    SELECT 1 FROM transactions transaction_row
    WHERE transaction_row.id IN (OLD.transaction_id, NEW.transaction_id)
      AND locked.month=substr(transaction_row.posted_on, 1, 7)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_structured_statement_imports_insert
BEFORE INSERT ON structured_statement_imports
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(NEW.period_start_on, 1, 7),
    substr(NEW.period_end_on, 1, 7)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_structured_statement_imports_update
BEFORE UPDATE ON structured_statement_imports
WHEN EXISTS (
  SELECT 1
  FROM v_period_policy_locked_months locked
  WHERE locked.month IN (
    substr(OLD.period_start_on, 1, 7),
    substr(OLD.period_end_on, 1, 7),
    substr(NEW.period_start_on, 1, 7),
    substr(NEW.period_end_on, 1, 7)
  )
  OR EXISTS (
    SELECT 1
    FROM statement_reviews review
    WHERE review.id IN (
      OLD.statement_review_id, NEW.statement_review_id
    )
      AND locked.month IN (
        review.period_month,
        substr(review.period_start_on, 1, 7),
        substr(review.period_end_on, 1, 7)
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;

CREATE TRIGGER period_policy_guard_structured_statement_import_audit_insert
BEFORE INSERT ON structured_statement_import_audit
WHEN EXISTS (
  SELECT 1
  FROM structured_statement_imports imported
  JOIN v_period_policy_locked_months locked
    ON locked.month IN (
      substr(imported.period_start_on, 1, 7),
      substr(imported.period_end_on, 1, 7)
    )
  WHERE imported.id=NEW.import_id
)
BEGIN
  SELECT RAISE(ABORT, 'period policy rejects write to closed period');
END;
