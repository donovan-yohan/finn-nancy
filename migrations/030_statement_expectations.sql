-- FN-141A: immutable account statement policies and account-period evidence truth.
--
-- Policy configuration, per-period requirement, and evidence lifecycle are
-- deliberately separate axes.  Policy history is append-only.  Expectation
-- state may change only after a matching, unique audit operation is appended.
-- Document links are retained as active/detached history rather than deleted.

CREATE TABLE account_statement_policies (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  effective_from_month TEXT NOT NULL,
  configuration_state TEXT NOT NULL
    CHECK (configuration_state IN ('configured', 'unconfigured')),
  requirement_mode TEXT
    CHECK (requirement_mode IS NULL OR requirement_mode IN ('required', 'no_statement')),
  cadence TEXT
    CHECK (cadence IS NULL OR cadence IN ('monthly', 'quarterly', 'annual', 'none')),
  anchor_month INTEGER
    CHECK (anchor_month IS NULL OR anchor_month BETWEEN 1 AND 12),
  active_from TEXT,
  active_to TEXT,
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    length(effective_from_month) = 7
    AND effective_from_month GLOB
      '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
    AND CAST(substr(effective_from_month, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
    AND CAST(substr(effective_from_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
  ),
  CHECK (
    (
      active_from IS NULL
      OR (
        length(active_from) = 10
        AND active_from GLOB
          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND CAST(substr(active_from, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
        AND date(active_from, '+0 days') = active_from
      )
    ) IS TRUE
  ),
  CHECK (
    (
      active_to IS NULL
      OR (
        length(active_to) = 10
        AND active_to GLOB
          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND CAST(substr(active_to, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
        AND date(active_to, '+0 days') = active_to
      )
    ) IS TRUE
  ),
  CHECK (active_from IS NULL OR active_to IS NULL OR active_from <= active_to),
  CHECK (
    (
      (
        configuration_state = 'unconfigured'
        AND requirement_mode IS NULL
        AND cadence IS NULL
        AND anchor_month IS NULL
      )
      OR (
        configuration_state = 'configured'
        AND requirement_mode IS NOT NULL
        AND requirement_mode = 'required'
        AND cadence IS NOT NULL
        AND cadence = 'monthly'
        AND anchor_month IS NULL
      )
      OR (
        configuration_state = 'configured'
        AND requirement_mode IS NOT NULL
        AND requirement_mode = 'required'
        AND cadence IS NOT NULL
        AND cadence IN ('quarterly', 'annual')
        AND anchor_month IS NOT NULL
        AND anchor_month BETWEEN 1 AND 12
      )
      OR (
        configuration_state = 'configured'
        AND requirement_mode IS NOT NULL
        AND requirement_mode = 'no_statement'
        AND cadence IS NOT NULL
        AND cadence = 'none'
        AND anchor_month IS NULL
      )
    ) IS TRUE
  )
);

CREATE INDEX idx_account_statement_policies_effective
  ON account_statement_policies(
    account_id, effective_from_month DESC, id DESC
  );

CREATE TABLE account_statement_expectations (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  period_month TEXT NOT NULL,
  policy_id INTEGER NOT NULL
    REFERENCES account_statement_policies(id) ON DELETE RESTRICT,
  origin TEXT NOT NULL CHECK (origin IN ('policy', 'legacy_document')),
  requirement_state TEXT NOT NULL
    CHECK (
      requirement_state IN (
        'required', 'waived', 'not_due', 'exempt', 'unconfigured'
      )
    ),
  lifecycle_state TEXT
    CHECK (
      lifecycle_state IS NULL
      OR lifecycle_state IN ('expected', 'received', 'reviewed', 'reconciled')
    ),
  waived_at TEXT,
  waived_by TEXT,
  waiver_reason TEXT,
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  last_transition_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (account_id, period_month),
  CHECK (
    length(period_month) = 7
    AND period_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
    AND CAST(substr(period_month, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
    AND CAST(substr(period_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
  ),
  CHECK (
    (
      (
        requirement_state = 'required'
        AND lifecycle_state IS NOT NULL
        AND lifecycle_state IN ('expected', 'received', 'reviewed', 'reconciled')
      )
      OR (
        requirement_state IN ('waived', 'not_due', 'exempt', 'unconfigured')
        AND lifecycle_state IS NULL
      )
    ) IS TRUE
  ),
  CHECK (
    (
      (
        requirement_state = 'waived'
        AND waived_at IS NOT NULL
        AND waived_by IS NOT NULL
        AND length(trim(waived_by)) > 0
        AND waiver_reason IS NOT NULL
        AND length(trim(waiver_reason)) > 0
      )
      OR (
        requirement_state <> 'waived'
        AND waived_at IS NULL
        AND waived_by IS NULL
        AND waiver_reason IS NULL
      )
    ) IS TRUE
  )
);

CREATE INDEX idx_account_statement_expectations_period
  ON account_statement_expectations(
    period_month, requirement_state, lifecycle_state, account_id
  );

CREATE TABLE statement_expectation_documents (
  id INTEGER PRIMARY KEY,
  expectation_id INTEGER NOT NULL
    REFERENCES account_statement_expectations(id) ON DELETE RESTRICT,
  source_document_id INTEGER
    REFERENCES source_documents(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'detached')),
  attached_by TEXT NOT NULL CHECK (length(trim(attached_by)) > 0),
  attach_reason TEXT NOT NULL CHECK (length(trim(attach_reason)) > 0),
  attached_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  detached_at TEXT,
  detached_by TEXT,
  detach_reason TEXT,
  CHECK (
    (
      status = 'active'
      AND source_document_id IS NOT NULL
      AND detached_at IS NULL
      AND detached_by IS NULL
      AND detach_reason IS NULL
    )
    OR (
      status = 'detached'
      AND detached_at IS NOT NULL
      AND length(trim(detached_by)) > 0
      AND length(trim(detach_reason)) > 0
    )
  )
);

CREATE UNIQUE INDEX uq_statement_expectation_documents_active_source
  ON statement_expectation_documents(source_document_id)
  WHERE status = 'active';
CREATE INDEX idx_statement_expectation_documents_expectation
  ON statement_expectation_documents(expectation_id, status, id);

CREATE TABLE statement_expectation_audit (
  id INTEGER PRIMARY KEY,
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) > 0),
  event_kind TEXT NOT NULL
    CHECK (
      event_kind IN (
        'policy_recorded',
        'expectation_prepared',
        'expectation_refreshed',
        'requirement_waived',
        'waiver_restored',
        'document_attached',
        'document_detached',
        'lifecycle_transition'
      )
    ),
  policy_id INTEGER
    REFERENCES account_statement_policies(id) ON DELETE RESTRICT,
  expectation_id INTEGER
    REFERENCES account_statement_expectations(id) ON DELETE RESTRICT,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  period_month TEXT NOT NULL,
  source_document_id INTEGER,
  old_requirement_state TEXT
    CHECK (
      old_requirement_state IS NULL
      OR old_requirement_state IN (
        'required', 'waived', 'not_due', 'exempt', 'unconfigured'
      )
    ),
  new_requirement_state TEXT
    CHECK (
      new_requirement_state IS NULL
      OR new_requirement_state IN (
        'required', 'waived', 'not_due', 'exempt', 'unconfigured'
      )
    ),
  old_lifecycle_state TEXT
    CHECK (
      old_lifecycle_state IS NULL
      OR old_lifecycle_state IN (
        'expected', 'received', 'reviewed', 'reconciled'
      )
    ),
  new_lifecycle_state TEXT
    CHECK (
      new_lifecycle_state IS NULL
      OR new_lifecycle_state IN (
        'expected', 'received', 'reviewed', 'reconciled'
      )
    ),
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    length(period_month) = 7
    AND period_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
    AND CAST(substr(period_month, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
    AND CAST(substr(period_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
  )
);

CREATE INDEX idx_statement_expectation_audit_expectation
  ON statement_expectation_audit(expectation_id, id);
CREATE INDEX idx_statement_expectation_audit_account_period
  ON statement_expectation_audit(account_id, period_month, id);

-- Policy versions and audit history are append-only.
CREATE TRIGGER account_statement_policy_no_update
BEFORE UPDATE ON account_statement_policies
BEGIN
  SELECT RAISE(ABORT, 'statement policies are immutable; append a new version');
END;

CREATE TRIGGER account_statement_policy_no_delete
BEFORE DELETE ON account_statement_policies
BEGIN
  SELECT RAISE(ABORT, 'statement policies are append-only');
END;

CREATE TRIGGER statement_expectation_audit_no_update
BEFORE UPDATE ON statement_expectation_audit
BEGIN
  SELECT RAISE(ABORT, 'statement expectation audit is append-only');
END;

CREATE TRIGGER statement_expectation_audit_no_delete
BEFORE DELETE ON statement_expectation_audit
BEGIN
  SELECT RAISE(ABORT, 'statement expectation audit is append-only');
END;

CREATE TRIGGER account_statement_expectation_no_delete
BEFORE DELETE ON account_statement_expectations
BEGIN
  SELECT RAISE(ABORT, 'statement expectations cannot be deleted');
END;

CREATE TRIGGER statement_expectation_document_no_delete
BEFORE DELETE ON statement_expectation_documents
BEGIN
  SELECT RAISE(ABORT, 'statement expectation links cannot be deleted; detach them');
END;

-- Every inserted policy, expectation, and link receives an audit row without
-- relying on a particular application writer.
CREATE TRIGGER account_statement_policy_audit_insert
AFTER INSERT ON account_statement_policies
BEGIN
  INSERT INTO statement_expectation_audit(
    operation_key, event_kind, policy_id, expectation_id, account_id,
    period_month, source_document_id,
    old_requirement_state, new_requirement_state,
    old_lifecycle_state, new_lifecycle_state, actor, reason
  )
  VALUES (
    'policy:recorded:' || NEW.id,
    'policy_recorded',
    NEW.id,
    NULL,
    NEW.account_id,
    NEW.effective_from_month,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NEW.created_by,
    NEW.reason
  );
END;

-- Every future account gets a conservative baseline through the database
-- boundary, including accounts created outside the Manage route.  Cash is the
-- only kind that proves no institution statement; every other kind remains
-- explicitly unconfigured.  The sentinel month makes a later explicit policy
-- effective even when it starts before today's month; it does not claim an
-- account activation date because active_from/active_to remain NULL.
CREATE TRIGGER account_statement_policy_after_account_insert
AFTER INSERT ON accounts
BEGIN
  INSERT INTO account_statement_policies(
    account_id, effective_from_month, configuration_state,
    requirement_mode, cadence, anchor_month, active_from, active_to,
    created_by, reason
  )
  VALUES (
    NEW.id,
    '0001-01',
    CASE WHEN NEW.kind = 'cash' THEN 'configured' ELSE 'unconfigured' END,
    CASE WHEN NEW.kind = 'cash' THEN 'no_statement' ELSE NULL END,
    CASE WHEN NEW.kind = 'cash' THEN 'none' ELSE NULL END,
    NULL,
    NULL,
    NULL,
    'system:account-create',
    CASE
      WHEN NEW.kind = 'cash'
        THEN 'new cash account defaults to no statement'
      ELSE 'new noncash account requires statement policy configuration'
    END
  );
END;

CREATE TRIGGER account_statement_expectation_policy_insert_guard
BEFORE INSERT ON account_statement_expectations
WHEN NOT EXISTS (
  SELECT 1
  FROM account_statement_policies policy
  WHERE policy.id = NEW.policy_id
    AND policy.account_id = NEW.account_id
    AND policy.effective_from_month <= NEW.period_month
)
BEGIN
  SELECT RAISE(
    ABORT,
    'expectation policy must belong to the account and be effective for the period'
  );
END;

CREATE TRIGGER account_statement_expectation_audit_insert
AFTER INSERT ON account_statement_expectations
BEGIN
  INSERT INTO statement_expectation_audit(
    operation_key, event_kind, policy_id, expectation_id, account_id,
    period_month, source_document_id,
    old_requirement_state, new_requirement_state,
    old_lifecycle_state, new_lifecycle_state, actor, reason
  )
  VALUES (
    'expectation:prepared:' || NEW.id,
    'expectation_prepared',
    NEW.policy_id,
    NEW.id,
    NEW.account_id,
    NEW.period_month,
    NULL,
    NULL,
    NEW.requirement_state,
    NULL,
    NEW.lifecycle_state,
    NEW.created_by,
    NEW.reason
  );
END;

-- An expectation mutation is accepted only when the caller first appended a
-- unique audit operation that exactly describes the old and new states.
CREATE TRIGGER account_statement_expectation_update_guard
BEFORE UPDATE ON account_statement_expectations
BEGIN
  SELECT CASE WHEN
    NEW.id <> OLD.id
    OR NEW.account_id <> OLD.account_id
    OR NEW.period_month <> OLD.period_month
    OR NEW.created_by <> OLD.created_by
    OR NEW.reason <> OLD.reason
    OR NEW.created_at <> OLD.created_at
  THEN RAISE(ABORT, 'statement expectation identity is immutable') END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM account_statement_policies policy
    WHERE policy.id = NEW.policy_id
      AND policy.account_id = NEW.account_id
      AND policy.effective_from_month <= NEW.period_month
  )
  THEN RAISE(
    ABORT,
    'expectation policy must belong to the account and be effective for the period'
  ) END;

  SELECT CASE WHEN
    NEW.last_transition_key IS NULL
    OR NEW.last_transition_key IS OLD.last_transition_key
  THEN RAISE(
    ABORT,
    'statement expectation update requires a new audit operation key'
  ) END;

  SELECT CASE WHEN
    NEW.policy_id IS OLD.policy_id
    AND NEW.origin IS OLD.origin
    AND NEW.requirement_state IS OLD.requirement_state
    AND NEW.lifecycle_state IS OLD.lifecycle_state
    AND NEW.waived_at IS OLD.waived_at
    AND NEW.waived_by IS OLD.waived_by
    AND NEW.waiver_reason IS OLD.waiver_reason
  THEN RAISE(ABORT, 'statement expectation update must change truth state') END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM statement_expectation_audit audit
    WHERE audit.operation_key = NEW.last_transition_key
      AND audit.expectation_id = OLD.id
      AND audit.account_id = OLD.account_id
      AND audit.period_month = OLD.period_month
      AND audit.policy_id IS NEW.policy_id
      AND audit.source_document_id IS NULL
      AND audit.old_requirement_state IS OLD.requirement_state
      AND audit.new_requirement_state IS NEW.requirement_state
      AND audit.old_lifecycle_state IS OLD.lifecycle_state
      AND audit.new_lifecycle_state IS NEW.lifecycle_state
      AND (
        (
          audit.event_kind = 'expectation_refreshed'
          AND OLD.requirement_state <> 'waived'
          AND (
            OLD.lifecycle_state IS NULL
            OR OLD.lifecycle_state = 'expected'
          )
          AND NEW.requirement_state <> 'waived'
          AND NEW.waived_at IS NULL
          AND NEW.waived_by IS NULL
          AND NEW.waiver_reason IS NULL
          AND NOT EXISTS (
            SELECT 1
            FROM statement_expectation_documents link
            WHERE link.expectation_id = OLD.id
              AND link.status = 'active'
          )
        )
        OR (
          audit.event_kind = 'requirement_waived'
          AND OLD.requirement_state = 'required'
          AND OLD.lifecycle_state = 'expected'
          AND NEW.requirement_state = 'waived'
          AND NEW.lifecycle_state IS NULL
          AND NEW.policy_id = OLD.policy_id
          AND NEW.origin = OLD.origin
          AND NEW.waived_at IS NOT NULL
          AND NEW.waived_by = audit.actor
          AND NEW.waiver_reason = audit.reason
          AND NOT EXISTS (
            SELECT 1
            FROM statement_expectation_documents link
            WHERE link.expectation_id = OLD.id
              AND link.status = 'active'
          )
        )
        OR (
          audit.event_kind = 'waiver_restored'
          AND OLD.requirement_state = 'waived'
          AND OLD.lifecycle_state IS NULL
          AND NEW.requirement_state = 'required'
          AND NEW.lifecycle_state = 'expected'
          AND NEW.policy_id = OLD.policy_id
          AND NEW.origin = OLD.origin
          AND NEW.waived_at IS NULL
          AND NEW.waived_by IS NULL
          AND NEW.waiver_reason IS NULL
        )
        OR (
          audit.event_kind = 'lifecycle_transition'
          AND OLD.requirement_state = 'required'
          AND NEW.requirement_state = 'required'
          AND NEW.policy_id = OLD.policy_id
          AND NEW.origin = OLD.origin
          AND NEW.waived_at IS NULL
          AND NEW.waived_by IS NULL
          AND NEW.waiver_reason IS NULL
          AND (
            (
              OLD.lifecycle_state = 'expected'
              AND NEW.lifecycle_state = 'received'
              AND EXISTS (
                SELECT 1
                FROM statement_expectation_documents link
                WHERE link.expectation_id = OLD.id
                  AND link.status = 'active'
              )
            )
            OR (
              OLD.lifecycle_state = 'received'
              AND NEW.lifecycle_state = 'reviewed'
              AND EXISTS (
                SELECT 1
                FROM statement_expectation_documents link
                WHERE link.expectation_id = OLD.id
                  AND link.status = 'active'
              )
            )
            OR (
              OLD.lifecycle_state = 'reviewed'
              AND NEW.lifecycle_state = 'reconciled'
              AND EXISTS (
                SELECT 1
                FROM statement_expectation_documents link
                JOIN statement_lines line
                  ON line.source_document_id = link.source_document_id
                WHERE link.expectation_id = OLD.id
                  AND link.status = 'active'
              )
              AND NOT EXISTS (
                SELECT 1
                FROM statement_expectation_documents link
                JOIN statement_lines line
                  ON line.source_document_id = link.source_document_id
                WHERE link.expectation_id = OLD.id
                  AND link.status = 'active'
                  AND (
                    line.is_pending = 1
                    OR line.match_status NOT IN (
                      'matched', 'promoted', 'ignored'
                    )
                  )
              )
            )
            OR (
              OLD.lifecycle_state = 'reconciled'
              AND NEW.lifecycle_state = 'reviewed'
            )
            OR (
              OLD.lifecycle_state IN ('reviewed', 'reconciled')
              AND NEW.lifecycle_state = 'received'
              AND EXISTS (
                SELECT 1
                FROM statement_expectation_documents link
                WHERE link.expectation_id = OLD.id
                  AND link.status = 'active'
              )
            )
            OR (
              OLD.lifecycle_state IN ('received', 'reviewed', 'reconciled')
              AND NEW.lifecycle_state = 'expected'
              AND NOT EXISTS (
                SELECT 1
                FROM statement_expectation_documents link
                WHERE link.expectation_id = OLD.id
                  AND link.status = 'active'
              )
            )
          )
        )
      )
  )
  THEN RAISE(
    ABORT,
    'statement expectation update does not match an allowed audited transition'
  ) END;
END;

CREATE TRIGGER statement_expectation_document_insert_guard
BEFORE INSERT ON statement_expectation_documents
BEGIN
  SELECT CASE WHEN NEW.status <> 'active'
  THEN RAISE(ABORT, 'statement expectation links must be created active') END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM source_documents document
    WHERE document.id = NEW.source_document_id
      AND document.kind = 'statement'
  )
  THEN RAISE(
    ABORT,
    'only a statement document/import can attach to an expectation'
  ) END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM account_statement_expectations expectation
    WHERE expectation.id = NEW.expectation_id
      AND expectation.requirement_state = 'required'
  )
  THEN RAISE(
    ABORT,
    'statement documents can attach only to required expectations'
  ) END;
END;

-- The only ordinary link mutation is active -> detached with complete audit
-- metadata.  A detached link may later have source_document_id nulled only by
-- the source document's ON DELETE SET NULL action.
CREATE TRIGGER statement_expectation_document_update_guard
BEFORE UPDATE ON statement_expectation_documents
WHEN NOT (
  (
    OLD.status = 'active'
    AND NEW.status = 'detached'
    AND NEW.id = OLD.id
    AND NEW.expectation_id = OLD.expectation_id
    AND NEW.source_document_id IS OLD.source_document_id
    AND NEW.attached_by = OLD.attached_by
    AND NEW.attach_reason = OLD.attach_reason
    AND NEW.attached_at = OLD.attached_at
    AND NEW.detached_at IS NOT NULL
    AND length(trim(NEW.detached_by)) > 0
    AND length(trim(NEW.detach_reason)) > 0
  )
  OR (
    OLD.status = 'detached'
    AND NEW.status = 'detached'
    AND OLD.source_document_id IS NOT NULL
    AND NEW.source_document_id IS NULL
    AND NEW.id = OLD.id
    AND NEW.expectation_id = OLD.expectation_id
    AND NEW.attached_by = OLD.attached_by
    AND NEW.attach_reason = OLD.attach_reason
    AND NEW.attached_at = OLD.attached_at
    AND NEW.detached_at = OLD.detached_at
    AND NEW.detached_by = OLD.detached_by
    AND NEW.detach_reason = OLD.detach_reason
  )
)
BEGIN
  SELECT RAISE(
    ABORT,
    'statement expectation links can only transition active to detached'
  );
END;

CREATE TRIGGER statement_expectation_document_audit_insert
AFTER INSERT ON statement_expectation_documents
BEGIN
  INSERT INTO statement_expectation_audit(
    operation_key, event_kind, policy_id, expectation_id, account_id,
    period_month, source_document_id,
    old_requirement_state, new_requirement_state,
    old_lifecycle_state, new_lifecycle_state, actor, reason
  )
  SELECT
    'document:attached:' || NEW.id,
    'document_attached',
    expectation.policy_id,
    expectation.id,
    expectation.account_id,
    expectation.period_month,
    NEW.source_document_id,
    expectation.requirement_state,
    expectation.requirement_state,
    expectation.lifecycle_state,
    expectation.lifecycle_state,
    NEW.attached_by,
    NEW.attach_reason
  FROM account_statement_expectations expectation
  WHERE expectation.id = NEW.expectation_id;
END;

CREATE TRIGGER statement_expectation_document_audit_detach
AFTER UPDATE OF status ON statement_expectation_documents
WHEN OLD.status = 'active' AND NEW.status = 'detached'
BEGIN
  INSERT INTO statement_expectation_audit(
    operation_key, event_kind, policy_id, expectation_id, account_id,
    period_month, source_document_id,
    old_requirement_state, new_requirement_state,
    old_lifecycle_state, new_lifecycle_state, actor, reason
  )
  SELECT
    'document:detached:' || NEW.id,
    'document_detached',
    expectation.policy_id,
    expectation.id,
    expectation.account_id,
    expectation.period_month,
    NEW.source_document_id,
    expectation.requirement_state,
    expectation.requirement_state,
    expectation.lifecycle_state,
    expectation.lifecycle_state,
    NEW.detached_by,
    NEW.detach_reason
  FROM account_statement_expectations expectation
  WHERE expectation.id = NEW.expectation_id;
END;

-- Active expectation evidence cannot disappear or stop being a statement.
CREATE TRIGGER source_document_active_expectation_no_delete
BEFORE DELETE ON source_documents
WHEN EXISTS (
  SELECT 1
  FROM statement_expectation_documents link
  WHERE link.source_document_id = OLD.id
    AND link.status = 'active'
)
BEGIN
  SELECT RAISE(
    ABORT,
    'detach the active statement expectation link before deleting evidence'
  );
END;

CREATE TRIGGER source_document_active_expectation_statement_kind
BEFORE UPDATE OF kind ON source_documents
WHEN NEW.kind <> 'statement'
  AND EXISTS (
    SELECT 1
    FROM statement_expectation_documents link
    WHERE link.source_document_id = OLD.id
      AND link.status = 'active'
  )
BEGIN
  SELECT RAISE(
    ABORT,
    'an actively linked statement document cannot be reclassified'
  );
END;

-- Once evidence has been reviewed, identity changes must first pass through a
-- later workflow that downgrades and audits the expectation.
CREATE TRIGGER source_document_reviewed_identity_guard
BEFORE UPDATE OF kind, storage_ref, sha256 ON source_documents
WHEN (
    NEW.kind IS NOT OLD.kind
    OR NEW.storage_ref IS NOT OLD.storage_ref
    OR NEW.sha256 IS NOT OLD.sha256
  )
  AND EXISTS (
    SELECT 1
    FROM statement_expectation_documents link
    JOIN account_statement_expectations expectation
      ON expectation.id = link.expectation_id
    WHERE link.source_document_id = OLD.id
      AND link.status = 'active'
      AND expectation.lifecycle_state IN ('reviewed', 'reconciled')
  )
BEGIN
  SELECT RAISE(
    ABORT,
    'reviewed statement evidence identity cannot change without downgrade'
  );
END;

-- Conservative legacy policy backfill.  A cash account deterministically has
-- no institution statement.  Every other account remains explicitly
-- unconfigured.  The sentinel effective month does not infer an activation
-- date; active_from/active_to intentionally remain NULL.
INSERT INTO account_statement_policies(
  account_id, effective_from_month, configuration_state,
  requirement_mode, cadence, anchor_month, active_from, active_to,
  created_by, reason
)
SELECT
  account.id,
  '0001-01',
  CASE WHEN account.kind = 'cash' THEN 'configured' ELSE 'unconfigured' END,
  CASE WHEN account.kind = 'cash' THEN 'no_statement' ELSE NULL END,
  CASE WHEN account.kind = 'cash' THEN 'none' ELSE NULL END,
  NULL,
  NULL,
  NULL,
  'migration:030',
  CASE
    WHEN account.kind = 'cash'
      THEN 'conservative cash no-statement backfill; activity dates not inferred'
    ELSE 'legacy statement policy is unconfigured; cadence and activity dates not inferred'
  END
FROM accounts account
ORDER BY account.id;

-- A legacy statement document is auto-linked only when all of its staged rows
-- agree on exactly one non-NULL account and one valid closing month.  No
-- transaction date, account guess, or recurring cadence is inferred.
WITH eligible_documents AS (
  SELECT
    document.id AS source_document_id,
    MIN(line.account_id) AS account_id,
    MIN(line.statement_period) AS period_month
  FROM source_documents document
  JOIN statement_lines line
    ON line.source_document_id = document.id
  WHERE document.kind = 'statement'
  GROUP BY document.id
  HAVING COUNT(line.id) > 0
    AND SUM(CASE WHEN line.account_id IS NULL THEN 1 ELSE 0 END) = 0
    AND MIN(line.account_id) = MAX(line.account_id)
    AND SUM(
      CASE
        WHEN line.statement_period IS NULL
          OR length(line.statement_period) <> 7
          OR line.statement_period NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
          OR CAST(substr(line.statement_period, 1, 4) AS INTEGER)
            NOT BETWEEN 1 AND 9999
          OR CAST(substr(line.statement_period, 6, 2) AS INTEGER)
            NOT BETWEEN 1 AND 12
        THEN 1
        ELSE 0
      END
    ) = 0
    AND MIN(line.statement_period) = MAX(line.statement_period)
),
eligible_periods AS (
  SELECT DISTINCT account_id, period_month
  FROM eligible_documents
)
INSERT INTO account_statement_expectations(
  account_id, period_month, policy_id, origin,
  requirement_state, lifecycle_state, created_by, reason
)
SELECT
  eligible.account_id,
  eligible.period_month,
  policy.id,
  'legacy_document',
  'required',
  'expected',
  'migration:030',
  'exact legacy statement account and closing-period evidence'
FROM eligible_periods eligible
JOIN account_statement_policies policy
  ON policy.id = (
    SELECT candidate.id
    FROM account_statement_policies candidate
    WHERE candidate.account_id = eligible.account_id
      AND candidate.effective_from_month <= eligible.period_month
    ORDER BY candidate.effective_from_month DESC, candidate.id DESC
    LIMIT 1
  )
ORDER BY eligible.account_id, eligible.period_month;

WITH eligible_documents AS (
  SELECT
    document.id AS source_document_id,
    MIN(line.account_id) AS account_id,
    MIN(line.statement_period) AS period_month
  FROM source_documents document
  JOIN statement_lines line
    ON line.source_document_id = document.id
  WHERE document.kind = 'statement'
  GROUP BY document.id
  HAVING COUNT(line.id) > 0
    AND SUM(CASE WHEN line.account_id IS NULL THEN 1 ELSE 0 END) = 0
    AND MIN(line.account_id) = MAX(line.account_id)
    AND SUM(
      CASE
        WHEN line.statement_period IS NULL
          OR length(line.statement_period) <> 7
          OR line.statement_period NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
          OR CAST(substr(line.statement_period, 1, 4) AS INTEGER)
            NOT BETWEEN 1 AND 9999
          OR CAST(substr(line.statement_period, 6, 2) AS INTEGER)
            NOT BETWEEN 1 AND 12
        THEN 1
        ELSE 0
      END
    ) = 0
    AND MIN(line.statement_period) = MAX(line.statement_period)
)
INSERT INTO statement_expectation_documents(
  expectation_id, source_document_id, status, attached_by, attach_reason
)
SELECT
  expectation.id,
  eligible.source_document_id,
  'active',
  'migration:030',
  'exact legacy statement account and closing-period evidence'
FROM eligible_documents eligible
JOIN account_statement_expectations expectation
  ON expectation.account_id = eligible.account_id
 AND expectation.period_month = eligible.period_month
ORDER BY expectation.id, eligible.source_document_id;

-- Preserve the truthful lifecycle chain.  The expectation is prepared as
-- expected, each exact document link is audited, then one deterministic
-- audited transition advances the account-period to received.
INSERT INTO statement_expectation_audit(
  operation_key, event_kind, policy_id, expectation_id, account_id,
  period_month, source_document_id,
  old_requirement_state, new_requirement_state,
  old_lifecycle_state, new_lifecycle_state, actor, reason
)
SELECT
  'migration:030:received:' || expectation.id,
  'lifecycle_transition',
  expectation.policy_id,
  expectation.id,
  expectation.account_id,
  expectation.period_month,
  NULL,
  'required',
  'required',
  'expected',
  'received',
  'migration:030',
  'exact legacy statement evidence was attached'
FROM account_statement_expectations expectation
WHERE expectation.origin = 'legacy_document'
  AND expectation.requirement_state = 'required'
  AND expectation.lifecycle_state = 'expected'
  AND EXISTS (
    SELECT 1
    FROM statement_expectation_documents link
    WHERE link.expectation_id = expectation.id
      AND link.status = 'active'
  )
ORDER BY expectation.id;

UPDATE account_statement_expectations
SET lifecycle_state = 'received',
    last_transition_key = 'migration:030:received:' || id,
    updated_at = CURRENT_TIMESTAMP
WHERE origin = 'legacy_document'
  AND requirement_state = 'required'
  AND lifecycle_state = 'expected'
  AND EXISTS (
    SELECT 1
    FROM statement_expectation_documents link
    WHERE link.expectation_id = account_statement_expectations.id
      AND link.status = 'active'
  );
