-- FN-142: canonical statement metadata, immutable source anchors, and audited
-- editable review rows.
--
-- The mutable tables below are projections.  Extracted claims, page identity,
-- source anchors, and audit events are append-only evidence.  Statement rows
-- are excluded/restored with a tombstone instead of being physically deleted.

CREATE TABLE statement_reviews (
  id INTEGER PRIMARY KEY,
  source_document_id INTEGER NOT NULL UNIQUE
    REFERENCES source_documents(id) ON DELETE RESTRICT,
  extraction_id INTEGER
    REFERENCES ingest_extractions(id) ON DELETE RESTRICT,
  account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
  period_start_on TEXT,
  period_end_on TEXT,
  statement_issued_on TEXT,
  period_month TEXT,
  opening_balance_cents INTEGER,
  closing_balance_cents INTEGER,
  currency TEXT NOT NULL DEFAULT '',
  account_fingerprint TEXT NOT NULL DEFAULT '',
  fingerprint_version TEXT NOT NULL DEFAULT 'v1'
    CHECK (length(trim(fingerprint_version)) > 0),
  activity_kind TEXT NOT NULL DEFAULT 'unknown'
    CHECK (activity_kind IN ('unknown', 'transactions', 'zero_activity')),
  declared_page_count INTEGER CHECK (
    declared_page_count IS NULL OR declared_page_count > 0
  ),
  declared_row_count INTEGER CHECK (
    declared_row_count IS NULL OR declared_row_count >= 0
  ),
  observed_page_count INTEGER NOT NULL DEFAULT 0
    CHECK (observed_page_count >= 0),
  extracted_page_count INTEGER NOT NULL DEFAULT 0
    CHECK (
      extracted_page_count >= 0
      AND extracted_page_count <= observed_page_count
    ),
  extraction_truncated INTEGER NOT NULL DEFAULT 0
    CHECK (extraction_truncated IN (0, 1)),
  review_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (
      review_state IN (
        'pending', 'legacy_unverified', 'approved', 'approved_with_override'
      )
    ),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  last_operation_key TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT,
  reviewed_by TEXT,
  override_reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    period_start_on IS NULL
    OR (
      length(period_start_on) = 10
      AND period_start_on GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
      AND date(period_start_on, '+0 days') = period_start_on
    )
  ),
  CHECK (
    period_end_on IS NULL
    OR (
      length(period_end_on) = 10
      AND period_end_on GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
      AND date(period_end_on, '+0 days') = period_end_on
    )
  ),
  CHECK (
    statement_issued_on IS NULL
    OR (
      length(statement_issued_on) = 10
      AND statement_issued_on GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
      AND date(statement_issued_on, '+0 days') = statement_issued_on
    )
  ),
  CHECK (
    period_month IS NULL
    OR (
      length(period_month) = 7
      AND period_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
      AND CAST(substr(period_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12
    )
  ),
  CHECK (
    period_start_on IS NULL
    OR period_end_on IS NULL
    OR period_start_on <= period_end_on
  ),
  CHECK (
    period_end_on IS NULL
    OR period_month IS NULL
    OR substr(period_end_on, 1, 7) = period_month
  ),
  CHECK (
    (
      review_state IN ('approved', 'approved_with_override')
      AND reviewed_at IS NOT NULL
      AND reviewed_by IS NOT NULL
      AND length(trim(reviewed_by)) > 0
    )
    OR (
      review_state IN ('pending', 'legacy_unverified')
      AND reviewed_at IS NULL
      AND reviewed_by IS NULL
    )
  ),
  CHECK (
    (
      review_state = 'approved_with_override'
      AND override_reason IS NOT NULL
      AND length(trim(override_reason)) > 0
    )
    OR (
      review_state <> 'approved_with_override'
      AND override_reason IS NULL
    )
  )
);

CREATE INDEX idx_statement_reviews_period
  ON statement_reviews(period_month, review_state, account_id);
CREATE INDEX idx_statement_reviews_account
  ON statement_reviews(account_id, period_month, id);

CREATE TABLE statement_review_pages (
  id INTEGER PRIMARY KEY,
  statement_review_id INTEGER NOT NULL
    REFERENCES statement_reviews(id) ON DELETE RESTRICT,
  page_number INTEGER NOT NULL CHECK (page_number > 0),
  source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
  page_sha256 TEXT NOT NULL CHECK (length(page_sha256) = 64),
  digest_version TEXT NOT NULL DEFAULT 'render-v1'
    CHECK (length(trim(digest_version)) > 0),
  included_in_extraction INTEGER NOT NULL DEFAULT 1
    CHECK (included_in_extraction IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (statement_review_id, page_number)
);

CREATE TABLE statement_source_anchors (
  id INTEGER PRIMARY KEY,
  statement_review_id INTEGER NOT NULL
    REFERENCES statement_reviews(id) ON DELETE RESTRICT,
  page_id INTEGER REFERENCES statement_review_pages(id) ON DELETE RESTRICT,
  locator_kind TEXT NOT NULL
    CHECK (locator_kind IN ('page', 'page_region', 'raw_row')),
  locator_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(locator_json)),
  source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (locator_kind IN ('page', 'page_region') AND page_id IS NOT NULL)
    OR locator_kind = 'raw_row'
  )
);

CREATE INDEX idx_statement_source_anchors_review
  ON statement_source_anchors(statement_review_id, page_id, id);

CREATE TABLE statement_field_evidence (
  id INTEGER PRIMARY KEY,
  evidence_key TEXT NOT NULL UNIQUE CHECK (length(trim(evidence_key)) > 0),
  statement_review_id INTEGER NOT NULL
    REFERENCES statement_reviews(id) ON DELETE RESTRICT,
  statement_line_id INTEGER REFERENCES statement_lines(id) ON DELETE RESTRICT,
  extraction_id INTEGER
    REFERENCES ingest_extractions(id) ON DELETE RESTRICT,
  field_name TEXT NOT NULL CHECK (length(trim(field_name)) > 0),
  original_value_json TEXT NOT NULL CHECK (json_valid(original_value_json)),
  confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
  source_anchor_id INTEGER
    REFERENCES statement_source_anchors(id) ON DELETE RESTRICT,
  origin TEXT NOT NULL CHECK (origin IN ('extractor', 'manual', 'migration')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_statement_field_evidence_review
  ON statement_field_evidence(statement_review_id, statement_line_id, field_name);

CREATE TABLE statement_review_audit (
  id INTEGER PRIMARY KEY,
  operation_key TEXT NOT NULL UNIQUE CHECK (length(trim(operation_key)) > 0),
  statement_review_id INTEGER NOT NULL
    REFERENCES statement_reviews(id) ON DELETE RESTRICT,
  statement_line_id INTEGER REFERENCES statement_lines(id) ON DELETE RESTRICT,
  event_kind TEXT NOT NULL CHECK (
    event_kind IN (
      'review_created',
      'metadata_corrected',
      'account_resolved',
      'row_added',
      'row_corrected',
      'row_excluded',
      'row_restored',
      'review_approved',
      'review_approved_with_override',
      'review_reopened',
      'review_archived'
    )
  ),
  old_values_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(old_values_json)),
  new_values_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(new_values_json)),
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_statement_review_audit_review
  ON statement_review_audit(statement_review_id, id);
CREATE INDEX idx_statement_review_audit_line
  ON statement_review_audit(statement_line_id, id);

ALTER TABLE statement_lines ADD COLUMN review_disposition TEXT
  NOT NULL DEFAULT 'active'
  CHECK (review_disposition IN ('active', 'excluded'));
ALTER TABLE statement_lines ADD COLUMN review_revision INTEGER
  NOT NULL DEFAULT 1 CHECK (review_revision >= 1);
ALTER TABLE statement_lines ADD COLUMN review_operation_key TEXT
  NOT NULL DEFAULT '';
ALTER TABLE statement_lines ADD COLUMN source_anchor_id INTEGER
  REFERENCES statement_source_anchors(id) ON DELETE RESTRICT;
ALTER TABLE statement_lines ADD COLUMN row_confidence REAL
  NOT NULL DEFAULT 0 CHECK (row_confidence >= 0.0 AND row_confidence <= 1.0);

CREATE INDEX idx_statement_lines_review
  ON statement_lines(source_document_id, review_disposition, posted_on, id);

-- Foreign keys prove that referenced rows exist; these scope guards additionally
-- prove that every evidence edge belongs to this review's immutable source.
CREATE TRIGGER statement_reviews_extraction_scope_insert
BEFORE INSERT ON statement_reviews
WHEN NEW.extraction_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM ingest_extractions extraction
    WHERE extraction.id=NEW.extraction_id
      AND extraction.source_document_id=NEW.source_document_id
      AND extraction.doc_kind='statement'
  )
BEGIN
  SELECT RAISE(ABORT, 'statement review extraction scope mismatch');
END;

CREATE TRIGGER statement_reviews_identity_immutable
BEFORE UPDATE OF source_document_id, extraction_id, fingerprint_version
ON statement_reviews
WHEN NEW.source_document_id IS NOT OLD.source_document_id
  OR NEW.extraction_id IS NOT OLD.extraction_id
  OR NEW.fingerprint_version IS NOT OLD.fingerprint_version
BEGIN
  SELECT RAISE(ABORT, 'statement review evidence identity is immutable');
END;

CREATE TRIGGER statement_review_pages_source_hash_scope_insert
BEFORE INSERT ON statement_review_pages
WHEN NOT EXISTS (
  SELECT 1
  FROM statement_reviews review
  JOIN source_documents document
    ON document.id=review.source_document_id
  WHERE review.id=NEW.statement_review_id
    AND document.sha256=NEW.source_sha256
)
BEGIN
  SELECT RAISE(ABORT, 'statement page source hash scope mismatch');
END;

CREATE TRIGGER statement_source_anchors_page_scope_insert
BEFORE INSERT ON statement_source_anchors
WHEN NEW.page_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM statement_review_pages page
    WHERE page.id=NEW.page_id
      AND page.statement_review_id=NEW.statement_review_id
  )
BEGIN
  SELECT RAISE(ABORT, 'statement source anchor page scope mismatch');
END;

CREATE TRIGGER statement_source_anchors_hash_scope_insert
BEFORE INSERT ON statement_source_anchors
WHEN NOT EXISTS (
    SELECT 1
    FROM statement_reviews review
    JOIN source_documents document
      ON document.id=review.source_document_id
    WHERE review.id=NEW.statement_review_id
      AND document.sha256=NEW.source_sha256
  )
  OR (
    NEW.page_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM statement_review_pages page
      WHERE page.id=NEW.page_id
        AND page.statement_review_id=NEW.statement_review_id
        AND page.source_sha256=NEW.source_sha256
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'statement source anchor hash scope mismatch');
END;

CREATE TRIGGER statement_field_evidence_scope_insert
BEFORE INSERT ON statement_field_evidence
WHEN (
    NEW.extraction_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM statement_reviews review
      JOIN ingest_extractions extraction
        ON extraction.id=NEW.extraction_id
       AND extraction.source_document_id=review.source_document_id
       AND extraction.doc_kind='statement'
      WHERE review.id=NEW.statement_review_id
    )
  )
  OR (
    NEW.statement_line_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM statement_reviews review
      JOIN statement_lines line
        ON line.id=NEW.statement_line_id
       AND line.source_document_id=review.source_document_id
      WHERE review.id=NEW.statement_review_id
    )
  )
  OR (
    NEW.source_anchor_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM statement_source_anchors anchor
      WHERE anchor.id=NEW.source_anchor_id
        AND anchor.statement_review_id=NEW.statement_review_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'statement field evidence scope mismatch');
END;

CREATE TRIGGER statement_review_audit_line_scope_insert
BEFORE INSERT ON statement_review_audit
WHEN NEW.statement_line_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM statement_reviews review
    JOIN statement_lines line
      ON line.id=NEW.statement_line_id
     AND line.source_document_id=review.source_document_id
    WHERE review.id=NEW.statement_review_id
  )
BEGIN
  SELECT RAISE(ABORT, 'statement review audit line scope mismatch');
END;

CREATE TRIGGER statement_lines_anchor_scope_insert
BEFORE INSERT ON statement_lines
WHEN NEW.source_anchor_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM statement_reviews review
    JOIN statement_source_anchors anchor
      ON anchor.statement_review_id=review.id
    WHERE review.source_document_id=NEW.source_document_id
      AND anchor.id=NEW.source_anchor_id
  )
BEGIN
  SELECT RAISE(ABORT, 'statement row source anchor scope mismatch');
END;

CREATE TRIGGER statement_lines_anchor_scope_update
BEFORE UPDATE OF source_anchor_id ON statement_lines
WHEN NEW.source_anchor_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM statement_reviews review
    JOIN statement_source_anchors anchor
      ON anchor.statement_review_id=review.id
    WHERE review.source_document_id=NEW.source_document_id
      AND anchor.id=NEW.source_anchor_id
  )
BEGIN
  SELECT RAISE(ABORT, 'statement row source anchor scope mismatch');
END;

CREATE TRIGGER statement_lines_source_identity_immutable
BEFORE UPDATE OF source_document_id ON statement_lines
WHEN NEW.source_document_id IS NOT OLD.source_document_id
  AND (
    EXISTS (
      SELECT 1 FROM statement_reviews review
      WHERE review.source_document_id=OLD.source_document_id
    )
    OR EXISTS (
      SELECT 1 FROM statement_reviews review
      WHERE review.source_document_id=NEW.source_document_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'statement row source identity is immutable');
END;

-- Immutable evidence and audit history.
CREATE TRIGGER statement_review_pages_no_update
BEFORE UPDATE ON statement_review_pages
BEGIN
  SELECT RAISE(ABORT, 'statement page evidence is immutable');
END;

CREATE TRIGGER statement_review_pages_no_delete
BEFORE DELETE ON statement_review_pages
BEGIN
  SELECT RAISE(ABORT, 'statement page evidence is append-only');
END;

CREATE TRIGGER statement_source_anchors_no_update
BEFORE UPDATE ON statement_source_anchors
BEGIN
  SELECT RAISE(ABORT, 'statement source anchors are immutable');
END;

CREATE TRIGGER statement_source_anchors_no_delete
BEFORE DELETE ON statement_source_anchors
BEGIN
  SELECT RAISE(ABORT, 'statement source anchors are append-only');
END;

CREATE TRIGGER statement_field_evidence_no_update
BEFORE UPDATE ON statement_field_evidence
BEGIN
  SELECT RAISE(ABORT, 'statement field evidence is immutable');
END;

CREATE TRIGGER statement_field_evidence_no_delete
BEFORE DELETE ON statement_field_evidence
BEGIN
  SELECT RAISE(ABORT, 'statement field evidence is append-only');
END;

CREATE TRIGGER statement_review_audit_no_update
BEFORE UPDATE ON statement_review_audit
BEGIN
  SELECT RAISE(ABORT, 'statement review audit is append-only');
END;

CREATE TRIGGER statement_review_audit_no_delete
BEFORE DELETE ON statement_review_audit
BEGIN
  SELECT RAISE(ABORT, 'statement review audit is append-only');
END;

-- Review projection changes require a preceding, uniquely keyed audit event
-- and optimistic revision advancement in the same write transaction.
CREATE TRIGGER statement_reviews_update_guard
BEFORE UPDATE ON statement_reviews
WHEN NOT (
  NEW.revision = OLD.revision + 1
  AND length(trim(NEW.last_operation_key)) > 0
  AND NEW.last_operation_key <> OLD.last_operation_key
  AND EXISTS (
    SELECT 1
    FROM statement_review_audit audit
    WHERE audit.operation_key = NEW.last_operation_key
      AND audit.statement_review_id = OLD.id
      AND audit.statement_line_id IS NULL
  )
)
BEGIN
  SELECT RAISE(
    ABORT,
    'statement review update requires matching audit and next revision'
  );
END;

-- User-visible row changes use a tombstone and require the same audit/revision
-- coupling.  Account assignment and internal row hashes remain separate
-- staging identity operations.
CREATE TRIGGER statement_lines_review_update_guard
BEFORE UPDATE OF
  posted_on, raw_description, amount_cents, currency, balance_cents,
  is_pending, review_disposition, source_anchor_id, row_confidence
ON statement_lines
WHEN (
  NEW.posted_on IS NOT OLD.posted_on
  OR NEW.raw_description IS NOT OLD.raw_description
  OR NEW.amount_cents IS NOT OLD.amount_cents
  OR NEW.currency IS NOT OLD.currency
  OR NEW.balance_cents IS NOT OLD.balance_cents
  OR NEW.is_pending IS NOT OLD.is_pending
  OR NEW.review_disposition IS NOT OLD.review_disposition
  OR NEW.source_anchor_id IS NOT OLD.source_anchor_id
  OR NEW.row_confidence IS NOT OLD.row_confidence
)
AND EXISTS (
  SELECT 1
  FROM statement_reviews review
  WHERE review.source_document_id = OLD.source_document_id
)
AND NOT (
  NEW.review_revision = OLD.review_revision + 1
  AND length(trim(NEW.review_operation_key)) > 0
  AND NEW.review_operation_key <> OLD.review_operation_key
  AND EXISTS (
    SELECT 1
    FROM statement_review_audit audit
    WHERE audit.operation_key = NEW.review_operation_key
      AND audit.statement_line_id = OLD.id
  )
)
BEGIN
  SELECT RAISE(
    ABORT,
    'statement row review update requires matching audit and next revision'
  );
END;

CREATE TRIGGER statement_lines_no_physical_delete_after_review
BEFORE DELETE ON statement_lines
WHEN EXISTS (
  SELECT 1
  FROM statement_reviews review
  WHERE review.source_document_id = OLD.source_document_id
)
BEGIN
  SELECT RAISE(
    ABORT,
    'statement review rows must be excluded instead of deleted'
  );
END;

CREATE TRIGGER excluded_statement_line_no_match
BEFORE UPDATE OF match_status, matched_transaction_id ON statement_lines
WHEN OLD.review_disposition = 'excluded'
  AND (
    NEW.match_status IS NOT OLD.match_status
    OR NEW.matched_transaction_id IS NOT OLD.matched_transaction_id
  )
BEGIN
  SELECT RAISE(ABORT, 'excluded statement rows cannot be reconciled');
END;

-- Once a source has review evidence, the source identity is immutable and the
-- source row cannot be deleted out from under its anchors.
CREATE TRIGGER source_document_statement_review_identity_guard
BEFORE UPDATE OF storage_ref, sha256, kind ON source_documents
WHEN (
    NEW.storage_ref IS NOT OLD.storage_ref
    OR NEW.sha256 IS NOT OLD.sha256
    OR NEW.kind IS NOT OLD.kind
  )
  AND EXISTS (
    SELECT 1 FROM statement_reviews review
    WHERE review.source_document_id = OLD.id
  )
BEGIN
  SELECT RAISE(ABORT, 'statement review source identity is immutable');
END;

-- Conservative legacy backfill.  Preserve existing exact line identity only;
-- never infer page completeness, confidence, statement dates, or zero activity.
INSERT INTO statement_reviews(
  source_document_id,
  extraction_id,
  account_id,
  period_month,
  currency,
  activity_kind,
  review_state
)
SELECT
  document.id,
  (
    SELECT extraction.id
    FROM ingest_extractions extraction
    WHERE extraction.source_document_id = document.id
      AND extraction.doc_kind = 'statement'
    ORDER BY extraction.id DESC
    LIMIT 1
  ),
  CASE
    WHEN (
      SELECT COUNT(DISTINCT line.account_id)
      FROM statement_lines line
      WHERE line.source_document_id = document.id
        AND line.account_id IS NOT NULL
    ) = 1
    AND NOT EXISTS (
      SELECT 1 FROM statement_lines line
      WHERE line.source_document_id = document.id
        AND line.account_id IS NULL
    )
    THEN (
      SELECT MIN(line.account_id)
      FROM statement_lines line
      WHERE line.source_document_id = document.id
    )
    ELSE NULL
  END,
  CASE
    WHEN (
      SELECT COUNT(DISTINCT line.statement_period)
      FROM statement_lines line
      WHERE line.source_document_id = document.id
        AND line.statement_period GLOB
          '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
        AND CAST(substr(line.statement_period, 6, 2) AS INTEGER)
          BETWEEN 1 AND 12
    ) = 1
    AND NOT EXISTS (
      SELECT 1 FROM statement_lines line
      WHERE line.source_document_id = document.id
        AND (
          line.statement_period IS NULL
          OR line.statement_period NOT GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
          OR CAST(substr(line.statement_period, 6, 2) AS INTEGER)
            NOT BETWEEN 1 AND 12
        )
    )
    THEN (
      SELECT MIN(line.statement_period)
      FROM statement_lines line
      WHERE line.source_document_id = document.id
    )
    ELSE NULL
  END,
  CASE
    WHEN (
      SELECT COUNT(DISTINCT line.currency)
      FROM statement_lines line
      WHERE line.source_document_id = document.id
        AND length(trim(line.currency)) > 0
    ) = 1
    THEN (
      SELECT MIN(line.currency)
      FROM statement_lines line
      WHERE line.source_document_id = document.id
        AND length(trim(line.currency)) > 0
    )
    ELSE ''
  END,
  CASE
    WHEN EXISTS (
      SELECT 1 FROM statement_lines line
      WHERE line.source_document_id = document.id
    )
    THEN 'transactions'
    ELSE 'unknown'
  END,
  'legacy_unverified'
FROM source_documents document
WHERE document.kind = 'statement';

INSERT INTO statement_review_audit(
  operation_key,
  statement_review_id,
  event_kind,
  actor,
  reason
)
SELECT
  'migration:031:review:' || review.id,
  review.id,
  'review_created',
  'migration:031',
  'conservative legacy statement review backfill'
FROM statement_reviews review;

INSERT INTO statement_field_evidence(
  evidence_key,
  statement_review_id,
  statement_line_id,
  extraction_id,
  field_name,
  original_value_json,
  confidence,
  source_anchor_id,
  origin
)
SELECT
  'migration:031:row:' || line.id,
  review.id,
  line.id,
  review.extraction_id,
  'row_snapshot',
  json_object(
    'posted_on', line.posted_on,
    'description', line.raw_description,
    'amount_cents', line.amount_cents,
    'currency', line.currency,
    'balance_cents', line.balance_cents,
    'is_pending', line.is_pending,
    'statement_period', line.statement_period
  ),
  0.0,
  NULL,
  'migration'
FROM statement_lines line
JOIN statement_reviews review
  ON review.source_document_id = line.source_document_id;

-- Accounting and close coverage are projections of active review rows only.
-- Excluded rows remain immutable evidence but must never contribute to totals.
DROP VIEW v_statement_coverage_by_doc;
DROP VIEW v_statement_coverage_lines;

CREATE VIEW v_statement_coverage_lines AS
SELECT
  sl.id AS line_id,
  sl.source_document_id,
  sd.original_name AS document_name,
  sd.status AS document_status,
  sl.account_id,
  COALESCE(a.name, 'Unassigned') AS account_name,
  sl.posted_on,
  strftime('%Y-%m', sl.posted_on) AS month,
  sl.raw_description,
  sl.norm_merchant,
  sl.amount_cents,
  CASE WHEN sl.amount_cents < 0 THEN ABS(sl.amount_cents) ELSE 0 END AS spend_cents,
  CASE WHEN sl.amount_cents > 0 THEN sl.amount_cents ELSE 0 END AS income_cents,
  sl.match_status,
  sl.matched_transaction_id,
  sl.match_method,
  sl.match_score,
  sl.match_rationale,
  CASE
    WHEN sl.amount_cents > 0 THEN 'income'
    WHEN sl.match_status = 'ignored' THEN 'ignored'
    WHEN sl.match_status IN ('matched', 'promoted') THEN 'covered'
    ELSE 'unmatched'
  END AS coverage_bucket,
  COALESCE(
    (
      SELECT GROUP_CONCAT(c.name, ', ')
      FROM transaction_splits ts
      JOIN categories c ON c.id = ts.category_id
      WHERE ts.transaction_id = sl.matched_transaction_id
    ),
    ''
  ) AS category_names,
  CASE
    WHEN sl.amount_cents >= 0 THEN ''
    WHEN sl.match_status = 'ignored' THEN 'ignored/internal transfer'
    WHEN sl.account_id IS NULL THEN 'needs account'
    WHEN sl.match_status IN ('matched', 'promoted')
      AND EXISTS (
        SELECT 1
        FROM transaction_splits ts
        JOIN categories c ON c.id = ts.category_id
        WHERE ts.transaction_id = sl.matched_transaction_id
          AND c.name = 'Uncategorized'
      )
      THEN 'needs category'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND EXISTS (
        SELECT 1
        FROM transactions t
        WHERE t.account_id IS sl.account_id
          AND t.amount_cents = sl.amount_cents
          AND t.recon_status = 'cleared'
          AND t.posted_on BETWEEN date(sl.posted_on, '-2 days')
                              AND date(sl.posted_on, '+2 days')
      )
      THEN 'possible duplicate'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND EXISTS (
        SELECT 1
        FROM transactions t
        WHERE t.account_id IS sl.account_id
          AND t.amount_cents = sl.amount_cents
          AND t.recon_status = 'uncleared'
          AND t.posted_on BETWEEN date(sl.posted_on, '-7 days')
                              AND date(sl.posted_on, '+1 days')
      )
      THEN 'ambiguous match'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND NOT EXISTS (
        SELECT 1
        FROM transactions t
        WHERE lower(t.description) LIKE '%' || lower(sl.norm_merchant) || '%'
           OR lower(t.counterparty) LIKE '%' || lower(sl.norm_merchant) || '%'
      )
      THEN 'new merchant'
    WHEN sl.match_status IN ('unmatched', 'needs_review') THEN 'missing receipt'
    ELSE ''
  END AS attention_reason
FROM statement_lines sl
JOIN source_documents sd ON sd.id = sl.source_document_id
LEFT JOIN accounts a ON a.id = sl.account_id
WHERE sl.review_disposition = 'active';

CREATE VIEW v_statement_coverage_by_doc AS
WITH grouped AS (
  SELECT
    source_document_id,
    document_name,
    document_status,
    MIN(posted_on) AS first_posted_on,
    MAX(posted_on) AS last_posted_on,
    COUNT(*) AS line_count,
    SUM(spend_cents) AS statement_spend_cents,
    SUM(CASE WHEN coverage_bucket = 'covered' THEN spend_cents ELSE 0 END)
      AS covered_spend_cents,
    SUM(CASE WHEN coverage_bucket = 'unmatched' THEN spend_cents ELSE 0 END)
      AS unmatched_spend_cents,
    SUM(CASE WHEN coverage_bucket = 'ignored' THEN spend_cents ELSE 0 END)
      AS ignored_spend_cents,
    SUM(income_cents) AS income_cents,
    SUM(
      CASE
        WHEN attention_reason <> ''
         AND attention_reason <> 'ignored/internal transfer'
        THEN spend_cents ELSE 0
      END
    ) AS attention_spend_cents
  FROM v_statement_coverage_lines
  GROUP BY source_document_id
)
SELECT
  *,
  CASE
    WHEN covered_spend_cents + unmatched_spend_cents = 0 THEN 0
    ELSE ROUND(
      100.0 * covered_spend_cents
      / (covered_spend_cents + unmatched_spend_cents),
      1
    )
  END AS coverage_pct
FROM grouped;

-- Extend the expectation transition guard so reviewed -> reconciled accepts
-- either terminal active rows or an approved, unchanged-balance zero-activity
-- proof. Excluded rows are evidence only and do not satisfy reconciliation.
DROP TRIGGER account_statement_expectation_update_guard;
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
              AND (
                (
                  EXISTS (
                    SELECT 1
                    FROM statement_expectation_documents link
                    JOIN statement_lines line
                      ON line.source_document_id = link.source_document_id
                    WHERE link.expectation_id = OLD.id
                      AND link.status = 'active'
                      AND line.review_disposition = 'active'
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM statement_expectation_documents link
                    JOIN statement_lines line
                      ON line.source_document_id = link.source_document_id
                    WHERE link.expectation_id = OLD.id
                      AND link.status = 'active'
                      AND line.review_disposition = 'active'
                      AND (
                        line.is_pending = 1
                        OR line.match_status NOT IN (
                          'matched', 'promoted', 'ignored'
                        )
                      )
                  )
                )
                OR (
                  NOT EXISTS (
                    SELECT 1
                    FROM statement_expectation_documents link
                    JOIN statement_lines line
                      ON line.source_document_id = link.source_document_id
                    WHERE link.expectation_id = OLD.id
                      AND link.status = 'active'
                      AND line.review_disposition = 'active'
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM statement_expectation_documents link
                    JOIN statement_reviews review
                      ON review.source_document_id = link.source_document_id
                    WHERE link.expectation_id = OLD.id
                      AND link.status = 'active'
                      AND review.review_state IN (
                        'approved', 'approved_with_override'
                      )
                      AND review.activity_kind = 'zero_activity'
                      AND review.account_id = OLD.account_id
                      AND review.period_month = OLD.period_month
                      AND review.opening_balance_cents IS NOT NULL
                      AND review.closing_balance_cents IS NOT NULL
                      AND review.opening_balance_cents =
                          review.closing_balance_cents
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM statement_expectation_documents link
                    LEFT JOIN statement_reviews review
                      ON review.source_document_id = link.source_document_id
                    WHERE link.expectation_id = OLD.id
                      AND link.status = 'active'
                      AND (
                        review.id IS NULL
                        OR review.review_state NOT IN (
                          'approved', 'approved_with_override'
                        )
                        OR review.activity_kind <> 'zero_activity'
                        OR review.account_id <> OLD.account_id
                        OR review.period_month <> OLD.period_month
                        OR review.opening_balance_cents IS NULL
                        OR review.closing_balance_cents IS NULL
                        OR review.opening_balance_cents <>
                            review.closing_balance_cents
                      )
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
