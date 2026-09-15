-- FN-140: persist movement semantics independently from purpose categories.
--
-- Historical sign/category data is not sufficient evidence for semantic meaning.
-- Only sources whose existing writer contract is deterministic are backfilled:
-- negative home-currency receipts are purchases, opening rows are openings, and
-- reconciliation adjustments are adjustments. V1's persisted account default is
-- CAD; blank/foreign account currency is not enough evidence to classify history.
-- Everything else remains explicitly reviewable.

ALTER TABLE transactions ADD COLUMN flow_kind TEXT NOT NULL DEFAULT 'unknown'
  CHECK (flow_kind IN (
    'unknown',
    'purchase',
    'income',
    'refund',
    'reimbursement',
    'internal_transfer',
    'card_payment',
    'fee',
    'interest',
    'reversal',
    'adjustment',
    'opening'
  ));

ALTER TABLE statement_lines ADD COLUMN flow_kind TEXT NOT NULL DEFAULT 'unknown'
  CHECK (flow_kind IN (
    'unknown',
    'purchase',
    'income',
    'refund',
    'reimbursement',
    'internal_transfer',
    'card_payment',
    'fee',
    'interest',
    'reversal',
    'adjustment',
    'opening'
  ));

CREATE TABLE transaction_flow_audit (
  id INTEGER PRIMARY KEY,
  transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  old_flow_kind TEXT NOT NULL,
  new_flow_kind TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (old_flow_kind <> new_flow_kind)
);
CREATE INDEX idx_transaction_flow_audit_txn
  ON transaction_flow_audit(transaction_id, id);

INSERT INTO transaction_flow_audit(
  transaction_id, old_flow_kind, new_flow_kind, actor, reason
)
SELECT
  id,
  'unknown',
  CASE
    WHEN source = 'receipt' AND amount_cents < 0
      AND EXISTS (
        SELECT 1 FROM accounts account
        WHERE account.id = transactions.account_id
          AND UPPER(TRIM(account.currency)) = 'CAD'
      )
    THEN 'purchase'
    WHEN source = 'opening' THEN 'opening'
    WHEN source = 'adjustment' THEN 'adjustment'
  END,
  'migration:028',
  'deterministic source-contract backfill'
FROM transactions
WHERE flow_kind = 'unknown'
  AND (
    (source = 'receipt' AND amount_cents < 0 AND EXISTS (
      SELECT 1 FROM accounts account
      WHERE account.id = transactions.account_id
        AND UPPER(TRIM(account.currency)) = 'CAD'
    ))
    OR source = 'opening'
    OR source = 'adjustment'
  );

UPDATE transactions
SET flow_kind = CASE
  WHEN source = 'receipt' AND amount_cents < 0
    AND EXISTS (
      SELECT 1 FROM accounts account
      WHERE account.id = transactions.account_id
        AND UPPER(TRIM(account.currency)) = 'CAD'
    )
  THEN 'purchase'
  WHEN source = 'opening' THEN 'opening'
  WHEN source = 'adjustment' THEN 'adjustment'
  ELSE flow_kind
END
WHERE flow_kind = 'unknown'
  AND (
    (source = 'receipt' AND amount_cents < 0 AND EXISTS (
      SELECT 1 FROM accounts account
      WHERE account.id = transactions.account_id
        AND UPPER(TRIM(account.currency)) = 'CAD'
    ))
    OR source = 'opening'
    OR source = 'adjustment'
  );

CREATE TABLE transaction_flow_reviews (
  transaction_id INTEGER PRIMARY KEY REFERENCES transactions(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'resolved')),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT,
  resolved_by TEXT NOT NULL DEFAULT '',
  resolution_flow_kind TEXT NOT NULL DEFAULT '',
  CHECK (
    (status = 'pending'
      AND resolved_at IS NULL
      AND resolved_by = ''
      AND resolution_flow_kind = '')
    OR
    (status = 'resolved'
      AND resolved_at IS NOT NULL
      AND length(trim(resolved_by)) > 0
      AND resolution_flow_kind NOT IN ('', 'unknown'))
  )
);
CREATE INDEX idx_transaction_flow_reviews_status
  ON transaction_flow_reviews(status, transaction_id);

INSERT INTO transaction_flow_reviews(transaction_id, reason)
SELECT id, 'historical semantics are ambiguous'
FROM transactions
WHERE flow_kind = 'unknown';

CREATE TABLE transaction_relationships (
  id INTEGER PRIMARY KEY,
  relationship_kind TEXT NOT NULL CHECK (relationship_kind IN (
    'transfer_pair',
    'refund_of',
    'reimbursement_for',
    'payment_for',
    'reversal_of'
  )),
  source_transaction_id INTEGER NOT NULL
    REFERENCES transactions(id) ON DELETE RESTRICT,
  target_transaction_id INTEGER NOT NULL
    REFERENCES transactions(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'revoked')),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at TEXT,
  revoked_by TEXT NOT NULL DEFAULT '',
  revocation_reason TEXT NOT NULL DEFAULT '',
  CHECK (source_transaction_id <> target_transaction_id),
  CHECK (
    (status = 'active'
      AND revoked_at IS NULL
      AND revoked_by = ''
      AND revocation_reason = '')
    OR
    (status = 'revoked'
      AND revoked_at IS NOT NULL
      AND length(trim(revoked_by)) > 0
      AND length(trim(revocation_reason)) > 0)
  )
);
CREATE INDEX idx_transaction_relationships_source
  ON transaction_relationships(source_transaction_id, status, id);
CREATE INDEX idx_transaction_relationships_target
  ON transaction_relationships(target_transaction_id, status, id);
CREATE UNIQUE INDEX uq_transaction_relationships_active_directed
  ON transaction_relationships(
    relationship_kind, source_transaction_id, target_transaction_id
  )
  WHERE status = 'active';
CREATE UNIQUE INDEX uq_transaction_relationships_active_pair
  ON transaction_relationships(
    relationship_kind,
    min(source_transaction_id, target_transaction_id),
    max(source_transaction_id, target_transaction_id)
  )
  WHERE status = 'active' AND relationship_kind = 'transfer_pair';
CREATE UNIQUE INDEX uq_transaction_relationships_active_transfer_source
  ON transaction_relationships(source_transaction_id)
  WHERE status = 'active' AND relationship_kind = 'transfer_pair';
CREATE UNIQUE INDEX uq_transaction_relationships_active_transfer_target
  ON transaction_relationships(target_transaction_id)
  WHERE status = 'active' AND relationship_kind = 'transfer_pair';
CREATE UNIQUE INDEX uq_transaction_relationships_active_offset_source
  ON transaction_relationships(relationship_kind, source_transaction_id)
  WHERE status = 'active' AND relationship_kind IN (
    'refund_of', 'reimbursement_for', 'reversal_of'
  );
CREATE UNIQUE INDEX uq_transaction_relationships_active_reversal_target
  ON transaction_relationships(target_transaction_id)
  WHERE status = 'active' AND relationship_kind = 'reversal_of';

CREATE TRIGGER transaction_relationship_validate_insert
BEFORE INSERT ON transaction_relationships
BEGIN
  SELECT CASE WHEN NEW.status <> 'active'
    THEN RAISE(ABORT, 'transaction relationships must be created active')
  END;

  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM transactions source
    JOIN transactions target
      ON target.id = NEW.target_transaction_id
    WHERE source.id = NEW.source_transaction_id
      AND (
        (
          NEW.relationship_kind = 'transfer_pair'
          AND source.flow_kind = target.flow_kind
          AND source.flow_kind IN ('internal_transfer', 'card_payment')
          AND source.account_id <> target.account_id
          AND source.amount_cents < 0
          AND target.amount_cents > 0
          AND source.amount_cents + target.amount_cents = 0
        )
        OR (
          NEW.relationship_kind = 'refund_of'
          AND source.flow_kind = 'refund'
          AND target.flow_kind IN ('purchase', 'fee')
          AND source.amount_cents > 0
          AND target.amount_cents < 0
          AND source.amount_cents <= -target.amount_cents
        )
        OR (
          NEW.relationship_kind = 'reimbursement_for'
          AND source.flow_kind = 'reimbursement'
          AND target.flow_kind IN ('purchase', 'fee')
          AND source.amount_cents > 0
          AND target.amount_cents < 0
          AND source.amount_cents <= -target.amount_cents
        )
        OR (
          NEW.relationship_kind = 'payment_for'
          AND source.flow_kind = 'card_payment'
          AND target.flow_kind IN ('purchase', 'fee')
        )
        OR (
          NEW.relationship_kind = 'reversal_of'
          AND source.flow_kind = 'reversal'
          AND source.amount_cents > 0
          AND target.flow_kind IN ('purchase', 'fee')
          AND target.amount_cents < 0
          AND source.amount_cents + target.amount_cents = 0
        )
      )
  ) THEN RAISE(ABORT, 'invalid transaction relationship') END;
END;

-- Cross-edge invariants cannot be represented by one row CHECK. SQLite
-- serializes writers; evaluating these predicates in the same INSERT statement
-- makes cumulative caps and one-pair membership transaction-safe.
CREATE TRIGGER transaction_relationship_v1_provenance_insert
BEFORE INSERT ON transaction_relationships
WHEN NEW.status = 'active'
BEGIN
  SELECT CASE WHEN NEW.relationship_kind = 'transfer_pair' AND (
    (SELECT amount_cents FROM transactions WHERE id=NEW.source_transaction_id) >= 0
    OR (SELECT amount_cents FROM transactions WHERE id=NEW.target_transaction_id) <= 0
    OR EXISTS (
      SELECT 1
      FROM transaction_relationships relationship
      WHERE relationship.status='active'
        AND relationship.relationship_kind='transfer_pair'
        AND (
          relationship.source_transaction_id IN (
            NEW.source_transaction_id, NEW.target_transaction_id
          )
          OR relationship.target_transaction_id IN (
            NEW.source_transaction_id, NEW.target_transaction_id
          )
        )
    )
  ) THEN RAISE(ABORT, 'transfer leg already paired or pair direction is invalid') END;

  SELECT CASE WHEN NEW.relationship_kind IN (
    'refund_of', 'reimbursement_for'
  ) AND (
    EXISTS (
      SELECT 1
      FROM transaction_relationships relationship
      WHERE relationship.status='active'
        AND relationship.relationship_kind='reversal_of'
        AND relationship.target_transaction_id=NEW.target_transaction_id
    )
    OR (
      SELECT COALESCE(SUM(source.amount_cents), 0)
      FROM transaction_relationships relationship
      JOIN transactions source
        ON source.id=relationship.source_transaction_id
      WHERE relationship.status='active'
        AND relationship.relationship_kind IN (
          'refund_of', 'reimbursement_for'
        )
        AND relationship.target_transaction_id=NEW.target_transaction_id
    ) + (
      SELECT amount_cents
      FROM transactions
      WHERE id=NEW.source_transaction_id
    ) > -(
      SELECT amount_cents
      FROM transactions
      WHERE id=NEW.target_transaction_id
    )
  ) THEN RAISE(ABORT, 'aggregate offsets exceed target amount or target is reversed') END;

  SELECT CASE WHEN NEW.relationship_kind = 'reversal_of' AND (
    (SELECT flow_kind FROM transactions WHERE id=NEW.source_transaction_id)
      <> 'reversal'
    OR (SELECT amount_cents FROM transactions WHERE id=NEW.source_transaction_id)
      <= 0
    OR (SELECT flow_kind FROM transactions WHERE id=NEW.target_transaction_id)
      NOT IN ('purchase', 'fee')
    OR (SELECT amount_cents FROM transactions WHERE id=NEW.target_transaction_id)
      >= 0
    OR EXISTS (
      SELECT 1
      FROM transaction_relationships relationship
      WHERE relationship.status='active'
        AND relationship.relationship_kind IN (
          'refund_of', 'reimbursement_for', 'reversal_of'
        )
        AND relationship.target_transaction_id=NEW.target_transaction_id
    )
  ) THEN RAISE(ABORT, 'reversal requires an exclusive purchase or fee target') END;
END;

CREATE TRIGGER transaction_relationship_validate_update
BEFORE UPDATE OF
  relationship_kind,
  source_transaction_id,
  target_transaction_id
ON transaction_relationships
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM transactions source
    JOIN transactions target
      ON target.id = NEW.target_transaction_id
    WHERE source.id = NEW.source_transaction_id
      AND (
        (
          NEW.relationship_kind = 'transfer_pair'
          AND source.flow_kind = target.flow_kind
          AND source.flow_kind IN ('internal_transfer', 'card_payment')
          AND source.account_id <> target.account_id
          AND source.amount_cents < 0
          AND target.amount_cents > 0
          AND source.amount_cents + target.amount_cents = 0
        )
        OR (
          NEW.relationship_kind = 'refund_of'
          AND source.flow_kind = 'refund'
          AND target.flow_kind IN ('purchase', 'fee')
          AND source.amount_cents > 0
          AND target.amount_cents < 0
          AND source.amount_cents <= -target.amount_cents
        )
        OR (
          NEW.relationship_kind = 'reimbursement_for'
          AND source.flow_kind = 'reimbursement'
          AND target.flow_kind IN ('purchase', 'fee')
          AND source.amount_cents > 0
          AND target.amount_cents < 0
          AND source.amount_cents <= -target.amount_cents
        )
        OR (
          NEW.relationship_kind = 'payment_for'
          AND source.flow_kind = 'card_payment'
          AND target.flow_kind IN ('purchase', 'fee')
        )
        OR (
          NEW.relationship_kind = 'reversal_of'
          AND source.flow_kind = 'reversal'
          AND source.amount_cents > 0
          AND target.flow_kind IN ('purchase', 'fee')
          AND target.amount_cents < 0
          AND source.amount_cents + target.amount_cents = 0
        )
      )
  ) THEN RAISE(ABORT, 'invalid transaction relationship') END;
END;

CREATE TRIGGER transaction_relationship_no_delete
BEFORE DELETE ON transaction_relationships
BEGIN
  SELECT RAISE(ABORT, 'transaction relationships are append-only; revoke instead');
END;

-- The sole update is an active -> revoked transition that preserves all
-- creation-time evidence. A revoked edge cannot be rewritten or reactivated.
CREATE TRIGGER transaction_relationship_append_only_update
BEFORE UPDATE ON transaction_relationships
WHEN NOT (
  OLD.status = 'active'
  AND NEW.status = 'revoked'
  AND NEW.id = OLD.id
  AND NEW.relationship_kind = OLD.relationship_kind
  AND NEW.source_transaction_id = OLD.source_transaction_id
  AND NEW.target_transaction_id = OLD.target_transaction_id
  AND NEW.created_by = OLD.created_by
  AND NEW.reason = OLD.reason
  AND NEW.created_at = OLD.created_at
)
BEGIN
  SELECT RAISE(ABORT, 'transaction relationships are append-only; revoke once');
END;

-- Existing writers may edit transaction amount/account fields. Never allow one
-- of those edits (or a direct flow tag update) to invalidate an active edge.
CREATE TRIGGER transaction_relationship_guard_source_update
BEFORE UPDATE OF flow_kind, amount_cents, account_id ON transactions
WHEN EXISTS (
  SELECT 1
  FROM transaction_relationships relationship
  JOIN transactions target
    ON target.id = relationship.target_transaction_id
  WHERE relationship.status = 'active'
    AND relationship.source_transaction_id = OLD.id
    AND NOT (
      (
        relationship.relationship_kind = 'transfer_pair'
        AND NEW.flow_kind = target.flow_kind
        AND NEW.flow_kind IN ('internal_transfer', 'card_payment')
        AND NEW.account_id <> target.account_id
        AND NEW.amount_cents < 0
        AND target.amount_cents > 0
        AND NEW.amount_cents + target.amount_cents = 0
      )
      OR (
        relationship.relationship_kind = 'refund_of'
        AND NEW.flow_kind = 'refund'
        AND target.flow_kind IN ('purchase', 'fee')
        AND NEW.amount_cents > 0
        AND target.amount_cents < 0
        AND NEW.amount_cents <= -target.amount_cents
      )
      OR (
        relationship.relationship_kind = 'reimbursement_for'
        AND NEW.flow_kind = 'reimbursement'
        AND target.flow_kind IN ('purchase', 'fee')
        AND NEW.amount_cents > 0
        AND target.amount_cents < 0
        AND NEW.amount_cents <= -target.amount_cents
      )
      OR (
        relationship.relationship_kind = 'payment_for'
        AND NEW.flow_kind = 'card_payment'
        AND target.flow_kind IN ('purchase', 'fee')
      )
      OR (
        relationship.relationship_kind = 'reversal_of'
        AND NEW.flow_kind = 'reversal'
        AND NEW.amount_cents > 0
        AND target.flow_kind IN ('purchase', 'fee')
        AND target.amount_cents < 0
        AND NEW.amount_cents + target.amount_cents = 0
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'transaction update would invalidate active relationship');
END;

CREATE TRIGGER transaction_relationship_guard_target_update
BEFORE UPDATE OF flow_kind, amount_cents, account_id ON transactions
WHEN EXISTS (
  SELECT 1
  FROM transaction_relationships relationship
  JOIN transactions source
    ON source.id = relationship.source_transaction_id
  WHERE relationship.status = 'active'
    AND relationship.target_transaction_id = OLD.id
    AND NOT (
      (
        relationship.relationship_kind = 'transfer_pair'
        AND source.flow_kind = NEW.flow_kind
        AND NEW.flow_kind IN ('internal_transfer', 'card_payment')
        AND source.account_id <> NEW.account_id
        AND source.amount_cents < 0
        AND NEW.amount_cents > 0
        AND source.amount_cents + NEW.amount_cents = 0
      )
      OR (
        relationship.relationship_kind = 'refund_of'
        AND source.flow_kind = 'refund'
        AND NEW.flow_kind IN ('purchase', 'fee')
        AND source.amount_cents > 0
        AND NEW.amount_cents < 0
        AND source.amount_cents <= -NEW.amount_cents
      )
      OR (
        relationship.relationship_kind = 'reimbursement_for'
        AND source.flow_kind = 'reimbursement'
        AND NEW.flow_kind IN ('purchase', 'fee')
        AND source.amount_cents > 0
        AND NEW.amount_cents < 0
        AND source.amount_cents <= -NEW.amount_cents
      )
      OR (
        relationship.relationship_kind = 'payment_for'
        AND source.flow_kind = 'card_payment'
        AND NEW.flow_kind IN ('purchase', 'fee')
      )
      OR (
        relationship.relationship_kind = 'reversal_of'
        AND source.flow_kind = 'reversal'
        AND source.amount_cents > 0
        AND NEW.flow_kind IN ('purchase', 'fee')
        AND NEW.amount_cents < 0
        AND source.amount_cents + NEW.amount_cents = 0
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'transaction update would invalidate active relationship');
END;

CREATE TRIGGER transaction_relationship_guard_offset_source_total
BEFORE UPDATE OF amount_cents ON transactions
WHEN EXISTS (
  SELECT 1
  FROM transaction_relationships current_relationship
  JOIN transactions target
    ON target.id=current_relationship.target_transaction_id
  WHERE current_relationship.status='active'
    AND current_relationship.relationship_kind IN (
      'refund_of', 'reimbursement_for'
    )
    AND current_relationship.source_transaction_id=OLD.id
    AND NEW.amount_cents + (
      SELECT COALESCE(SUM(other_source.amount_cents), 0)
      FROM transaction_relationships other_relationship
      JOIN transactions other_source
        ON other_source.id=other_relationship.source_transaction_id
      WHERE other_relationship.status='active'
        AND other_relationship.relationship_kind IN (
          'refund_of', 'reimbursement_for'
        )
        AND other_relationship.target_transaction_id=
          current_relationship.target_transaction_id
        AND other_relationship.id <> current_relationship.id
    ) > -target.amount_cents
)
BEGIN
  SELECT RAISE(ABORT, 'transaction update exceeds aggregate offset cap');
END;

CREATE TRIGGER transaction_relationship_guard_offset_target_total
BEFORE UPDATE OF amount_cents ON transactions
WHEN EXISTS (
  SELECT 1
  FROM transaction_relationships relationship
  WHERE relationship.status='active'
    AND relationship.relationship_kind IN (
      'refund_of', 'reimbursement_for'
    )
    AND relationship.target_transaction_id=OLD.id
    AND (
      SELECT COALESCE(SUM(source.amount_cents), 0)
      FROM transaction_relationships aggregate_relationship
      JOIN transactions source
        ON source.id=aggregate_relationship.source_transaction_id
      WHERE aggregate_relationship.status='active'
        AND aggregate_relationship.relationship_kind IN (
          'refund_of', 'reimbursement_for'
        )
        AND aggregate_relationship.target_transaction_id=OLD.id
    ) > -NEW.amount_cents
)
BEGIN
  SELECT RAISE(ABORT, 'transaction update exceeds aggregate offset cap');
END;

CREATE TRIGGER transaction_flow_review_after_insert
AFTER INSERT ON transactions
WHEN NEW.flow_kind = 'unknown'
BEGIN
  INSERT OR IGNORE INTO transaction_flow_reviews(transaction_id, reason)
  VALUES (NEW.id, 'writer explicitly deferred flow semantics');
END;

CREATE TRIGGER transaction_flow_review_after_unknown
AFTER UPDATE OF flow_kind ON transactions
WHEN NEW.flow_kind = 'unknown' AND OLD.flow_kind <> NEW.flow_kind
BEGIN
  INSERT INTO transaction_flow_reviews(
    transaction_id, status, reason, created_at,
    resolved_at, resolved_by, resolution_flow_kind
  )
  VALUES (
    NEW.id, 'pending', 'flow semantics were explicitly deferred',
    CURRENT_TIMESTAMP, NULL, '', ''
  )
  ON CONFLICT(transaction_id) DO UPDATE SET
    status = 'pending',
    reason = excluded.reason,
    created_at = CURRENT_TIMESTAMP,
    resolved_at = NULL,
    resolved_by = '',
    resolution_flow_kind = '';
END;

CREATE TRIGGER transaction_flow_review_after_resolution
AFTER UPDATE OF flow_kind ON transactions
WHEN NEW.flow_kind <> 'unknown' AND OLD.flow_kind <> NEW.flow_kind
BEGIN
  UPDATE transaction_flow_reviews
  SET
    status = 'resolved',
    resolved_at = CURRENT_TIMESTAMP,
    resolved_by = 'system',
    resolution_flow_kind = NEW.flow_kind
  WHERE transaction_id = NEW.id AND status = 'pending';
END;

-- Keep the original purpose category visible, but make the legacy category_kind
-- projection semantic so all existing reports stop treating transfer/payment rows
-- as income or spending and treat offsets as spending reductions.
DROP VIEW IF EXISTS v_transactions_recent;
DROP VIEW IF EXISTS v_category_totals;
DROP VIEW IF EXISTS v_category_monthly;
DROP VIEW IF EXISTS v_cashflow_monthly;
DROP VIEW IF EXISTS v_split_detail;
DROP VIEW IF EXISTS v_transaction_flow_status;

CREATE VIEW v_transaction_flow_status AS
WITH semantic_status AS (
  SELECT
    transaction_row.id AS transaction_id,
    transaction_row.flow_kind,
    CASE
      WHEN transaction_row.flow_kind = 'unknown' THEN 'unknown'
      WHEN transaction_row.flow_kind IN (
        'internal_transfer', 'card_payment'
      ) AND NOT EXISTS (
        SELECT 1
        FROM transaction_relationships relationship
        WHERE relationship.status='active'
          AND relationship.relationship_kind='transfer_pair'
          AND (
            relationship.source_transaction_id=transaction_row.id
            OR relationship.target_transaction_id=transaction_row.id
          )
      ) THEN 'missing_relationship'
      WHEN transaction_row.flow_kind = 'refund' AND NOT EXISTS (
        SELECT 1
        FROM transaction_relationships relationship
        WHERE relationship.status='active'
          AND relationship.relationship_kind='refund_of'
          AND relationship.source_transaction_id=transaction_row.id
      ) THEN 'missing_relationship'
      WHEN transaction_row.flow_kind = 'reimbursement' AND NOT EXISTS (
        SELECT 1
        FROM transaction_relationships relationship
        WHERE relationship.status='active'
          AND relationship.relationship_kind='reimbursement_for'
          AND relationship.source_transaction_id=transaction_row.id
      ) THEN 'missing_relationship'
      WHEN transaction_row.flow_kind = 'reversal' AND NOT EXISTS (
        SELECT 1
        FROM transaction_relationships relationship
        WHERE relationship.status='active'
          AND relationship.relationship_kind='reversal_of'
          AND relationship.source_transaction_id=transaction_row.id
      ) THEN 'missing_relationship'
      ELSE 'complete'
    END AS semantic_status
  FROM transactions transaction_row
)
SELECT
  transaction_id,
  flow_kind,
  semantic_status,
  CASE
    WHEN semantic_status='unknown' THEN 'flow meaning is unknown'
    WHEN semantic_status='missing_relationship'
      THEN 'active provenance relationship is required'
    ELSE ''
  END AS semantic_reason,
  CASE WHEN semantic_status='complete' THEN 1 ELSE 0 END AS report_eligible
FROM semantic_status;

CREATE VIEW v_split_detail AS
SELECT
  t.id AS transaction_id,
  t.posted_on,
  strftime('%Y-%m', t.posted_on) AS month,
  a.name AS account_name,
  a.kind AS account_kind,
  t.description,
  t.counterparty,
  t.amount_cents AS transaction_amount_cents,
  t.flow_kind,
  flow_status.semantic_status,
  flow_status.semantic_reason,
  flow_status.report_eligible,
  s.amount_cents AS split_amount_cents,
  c.id AS category_id,
  c.name AS category_name,
  CASE
    WHEN flow_status.report_eligible = 0 THEN 'review'
    WHEN t.flow_kind IN ('income', 'interest') THEN 'income'
    WHEN t.flow_kind IN (
      'purchase', 'refund', 'reimbursement', 'fee', 'reversal'
    ) THEN 'expense'
    WHEN t.flow_kind IN (
      'internal_transfer', 'card_payment', 'adjustment', 'opening'
    ) THEN 'transfer'
    ELSE 'review'
  END AS category_kind,
  c.kind AS purpose_category_kind,
  c.brand_owner,
  c.color,
  s.memo
FROM transaction_splits s
JOIN transactions t ON t.id = s.transaction_id
JOIN v_transaction_flow_status flow_status
  ON flow_status.transaction_id = t.id
JOIN accounts a ON a.id = t.account_id
JOIN categories c ON c.id = s.category_id;

CREATE VIEW v_cashflow_monthly AS
SELECT
  month,
  COALESCE(SUM(
    CASE WHEN category_kind = 'income' THEN split_amount_cents ELSE 0 END
  ), 0) AS income_cents,
  -COALESCE(SUM(
    CASE WHEN category_kind = 'expense' THEN split_amount_cents ELSE 0 END
  ), 0) AS expense_cents,
  COALESCE(SUM(
    CASE WHEN category_kind IN ('income', 'expense')
      THEN split_amount_cents ELSE 0 END
  ), 0) AS net_cents
FROM v_split_detail
GROUP BY month;

CREATE VIEW v_category_monthly AS
SELECT
  month,
  category_id,
  category_name,
  category_kind,
  brand_owner,
  color,
  SUM(split_amount_cents) AS amount_cents,
  CASE
    WHEN category_kind = 'expense' THEN -SUM(split_amount_cents)
    ELSE SUM(split_amount_cents)
  END AS magnitude_cents
FROM v_split_detail
WHERE category_kind IN ('income', 'expense')
GROUP BY month, category_id, category_kind;

CREATE VIEW v_category_totals AS
WITH totals AS (
  SELECT
    category_id,
    category_name,
    category_kind,
    brand_owner,
    color,
    SUM(split_amount_cents) AS total_cents,
    CASE
      WHEN category_kind = 'expense' THEN -SUM(split_amount_cents)
      ELSE SUM(split_amount_cents)
    END AS magnitude_cents
  FROM v_split_detail
  WHERE category_kind IN ('income', 'expense')
  GROUP BY category_id, category_kind
), grand AS (
  SELECT SUM(magnitude_cents) AS total_magnitude_cents FROM totals
)
SELECT
  totals.*,
  CASE
    WHEN grand.total_magnitude_cents = 0 THEN 0
    ELSE ROUND(100.0 * totals.magnitude_cents / grand.total_magnitude_cents, 1)
  END AS pct_of_total
FROM totals, grand;

CREATE VIEW v_transactions_recent AS
SELECT
  t.id,
  t.posted_on,
  a.name AS account_name,
  t.description,
  t.counterparty,
  t.amount_cents,
  t.flow_kind,
  flow_status.semantic_status,
  flow_status.semantic_reason,
  GROUP_CONCAT(c.name, ', ') AS categories
FROM transactions t
JOIN v_transaction_flow_status flow_status
  ON flow_status.transaction_id=t.id
JOIN accounts a ON a.id = t.account_id
LEFT JOIN transaction_splits s ON s.transaction_id = t.id
LEFT JOIN categories c ON c.id = s.category_id
GROUP BY t.id
ORDER BY t.posted_on DESC, t.id DESC;

-- These legacy derived views were originally sign/category based. Rebuild them
-- from semantically complete purchase/fee rows so unknown movements and owned
-- transfers never masquerade as subscriptions or recurring expenses.
DROP VIEW IF EXISTS v_recurring_payment_deltas;
DROP VIEW IF EXISTS v_recurring_payment_series_monthly;
DROP VIEW IF EXISTS v_subscription_watchlist_candidates;

CREATE VIEW v_recurring_payment_series_monthly AS
WITH monthly AS (
  SELECT
    TRIM(COALESCE(NULLIF(t.counterparty, ''), t.description)) AS merchant,
    t.account_id,
    a.name AS account_name,
    strftime('%Y-%m', t.posted_on) AS month,
    MIN(t.posted_on) AS first_posted_on,
    MAX(t.posted_on) AS last_posted_on,
    COUNT(DISTINCT t.id) AS tx_count,
    SUM(ABS(ts.amount_cents)) AS amount_cents,
    GROUP_CONCAT(DISTINCT t.id) AS transaction_ids,
    GROUP_CONCAT(DISTINCT sl.id) AS statement_line_ids,
    GROUP_CONCAT(DISTINCT c.name) AS category_names
  FROM transactions t
  JOIN v_transaction_flow_status flow_status
    ON flow_status.transaction_id=t.id
  JOIN accounts a ON a.id = t.account_id
  JOIN transaction_splits ts ON ts.transaction_id = t.id
  JOIN categories c ON c.id = ts.category_id
  LEFT JOIN statement_lines sl ON sl.matched_transaction_id = t.id
  WHERE t.recon_status = 'cleared'
    AND flow_status.report_eligible = 1
    AND t.flow_kind IN ('purchase', 'fee')
    AND t.amount_cents < 0
    AND c.kind = 'expense'
    AND TRIM(COALESCE(NULLIF(t.counterparty, ''), t.description)) <> ''
  GROUP BY merchant, t.account_id, month
)
SELECT
  monthly.*,
  COUNT(*) OVER (PARTITION BY merchant, account_id) AS months_seen,
  MIN(month) OVER (PARTITION BY merchant, account_id) AS first_month,
  MAX(month) OVER (PARTITION BY merchant, account_id) AS last_month,
  ROUND(AVG(amount_cents) OVER (PARTITION BY merchant, account_id), 2)
    AS avg_monthly_cents
FROM monthly;

CREATE VIEW v_recurring_payment_deltas AS
SELECT
  curr.merchant,
  curr.account_id,
  curr.account_name,
  curr.month,
  prev.month AS previous_month,
  curr.first_month,
  curr.last_month,
  curr.months_seen,
  curr.current_amount_cents,
  curr.previous_amount_cents,
  curr.amount_delta_cents,
  curr.pct_change,
  CASE
    WHEN curr.amount_delta_cents > 0 THEN 'increase'
    WHEN curr.amount_delta_cents < 0 THEN 'decrease'
    ELSE 'stable'
  END AS direction,
  CASE
    WHEN ABS(curr.amount_delta_cents) >= 500
     AND ABS(COALESCE(curr.pct_change, 0)) >= 5.0
    THEN 1
    ELSE 0
  END AS is_meaningful_delta,
  curr.tx_count AS current_tx_count,
  prev.tx_count AS previous_tx_count,
  curr.transaction_ids AS current_transaction_ids,
  prev.transaction_ids AS previous_transaction_ids,
  curr.statement_line_ids AS current_statement_line_ids,
  prev.statement_line_ids AS previous_statement_line_ids,
  curr.category_names
FROM (
  SELECT
    series.*,
    prev.month AS previous_month,
    series.amount_cents AS current_amount_cents,
    prev.amount_cents AS previous_amount_cents,
    series.amount_cents - prev.amount_cents AS amount_delta_cents,
    CASE
      WHEN prev.amount_cents IS NULL OR prev.amount_cents = 0 THEN NULL
      ELSE ROUND(
        100.0 * (series.amount_cents - prev.amount_cents) / prev.amount_cents,
        1
      )
    END AS pct_change
  FROM v_recurring_payment_series_monthly series
  JOIN v_recurring_payment_series_monthly prev
    ON prev.merchant = series.merchant
   AND prev.account_id = series.account_id
   AND prev.month = strftime('%Y-%m', date(series.month || '-01', '-1 month'))
  WHERE series.months_seen >= 2
) curr
JOIN v_recurring_payment_series_monthly prev
  ON prev.merchant = curr.merchant
 AND prev.account_id = curr.account_id
 AND prev.month = curr.previous_month
ORDER BY curr.month DESC, ABS(curr.amount_delta_cents) DESC, curr.merchant;

CREATE VIEW v_subscription_watchlist_candidates AS
WITH monthly AS (
  SELECT
    TRIM(COALESCE(NULLIF(t.counterparty, ''), t.description)) AS merchant,
    t.account_id,
    a.name AS account_name,
    strftime('%Y-%m', t.posted_on) AS month,
    MIN(t.posted_on) AS first_posted_on,
    MAX(t.posted_on) AS last_posted_on,
    COUNT(DISTINCT t.id) AS tx_count,
    SUM(ABS(ts.amount_cents)) AS amount_cents,
    GROUP_CONCAT(DISTINCT t.id) AS transaction_ids,
    GROUP_CONCAT(DISTINCT sl.id) AS statement_line_ids,
    GROUP_CONCAT(DISTINCT c.name) AS category_names
  FROM transactions t
  JOIN v_transaction_flow_status flow_status
    ON flow_status.transaction_id=t.id
  JOIN accounts a ON a.id = t.account_id
  JOIN transaction_splits ts ON ts.transaction_id = t.id
  JOIN categories c ON c.id = ts.category_id
  LEFT JOIN statement_lines sl ON sl.matched_transaction_id = t.id
  WHERE t.recon_status = 'cleared'
    AND flow_status.report_eligible = 1
    AND t.flow_kind IN ('purchase', 'fee')
    AND t.amount_cents < 0
    AND c.kind = 'expense'
    AND TRIM(COALESCE(NULLIF(t.counterparty, ''), t.description)) <> ''
  GROUP BY merchant, t.account_id, month
),
series AS (
  SELECT
    monthly.*,
    COUNT(*) OVER (PARTITION BY merchant, account_id) AS months_seen,
    MIN(month) OVER (PARTITION BY merchant, account_id) AS first_month,
    MAX(month) OVER (PARTITION BY merchant, account_id) AS last_month
  FROM monthly
)
SELECT
  curr.merchant,
  curr.account_id,
  curr.account_name,
  'new_subscription_likely' AS candidate_type,
  prev.month AS first_month,
  curr.month AS last_month,
  prev.first_posted_on AS first_seen_on,
  curr.last_posted_on AS last_seen_on,
  date(curr.last_posted_on, '+1 month') AS expected_next_charge_on,
  CAST(ROUND((curr.amount_cents + prev.amount_cents) / 2.0) AS INTEGER)
    AS estimated_amount_cents,
  curr.months_seen,
  curr.amount_cents AS current_amount_cents,
  prev.amount_cents AS previous_amount_cents,
  curr.tx_count AS current_tx_count,
  prev.tx_count AS previous_tx_count,
  prev.transaction_ids || ',' || curr.transaction_ids AS transaction_ids,
  TRIM(
    COALESCE(prev.statement_line_ids, '') || ',' ||
    COALESCE(curr.statement_line_ids, ''),
    ','
  ) AS statement_line_ids,
  COALESCE(curr.category_names, prev.category_names) AS category_names
FROM series curr
JOIN series prev
  ON prev.merchant = curr.merchant
 AND prev.account_id = curr.account_id
 AND prev.month = strftime('%Y-%m', date(curr.month || '-01', '-1 month'))
WHERE curr.months_seen = 2
  AND curr.first_month = prev.month
  AND curr.last_month = curr.month
ORDER BY curr.month DESC, estimated_amount_cents DESC, curr.merchant;
