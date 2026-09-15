-- FN-143: deterministic CSV/OFX statement imports.
--
-- Each mapping/account choice is an immutable attempt over one content-addressed
-- original. Raw provider account identifiers and FITIDs never enter SQLite:
-- normalized values are hashed before persistence. Confirmed rows and every
-- pending-to-posted replacement retain append-only identity/audit evidence.

ALTER TABLE statement_reviews ADD COLUMN source_kind TEXT
  NOT NULL DEFAULT 'page_document'
  CHECK (source_kind IN ('page_document', 'structured_rows'));

CREATE TABLE structured_statement_imports (
  id INTEGER PRIMARY KEY,
  source_document_id INTEGER NOT NULL
    REFERENCES source_documents(id) ON DELETE RESTRICT,
  statement_review_id INTEGER UNIQUE
    REFERENCES statement_reviews(id) ON DELETE RESTRICT,
  supersedes_import_id INTEGER
    REFERENCES structured_statement_imports(id) ON DELETE RESTRICT,
  attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
  config_fingerprint TEXT NOT NULL CHECK (length(config_fingerprint) = 64),
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
  adapter_id TEXT NOT NULL
    CHECK (adapter_id IN ('mapped_csv', 'ofx_sgml', 'ofx_xml')),
  adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) > 0),
  mapping_version TEXT NOT NULL DEFAULT '',
  mapping_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(mapping_json)),
  provider_identity_hash TEXT NOT NULL DEFAULT ''
    CHECK (length(provider_identity_hash) IN (0, 64)),
  account_last4 TEXT NOT NULL DEFAULT ''
    CHECK (length(account_last4) <= 4),
  period_start_on TEXT,
  period_end_on TEXT,
  statement_issued_on TEXT,
  currency TEXT NOT NULL DEFAULT ''
    CHECK (length(currency) IN (0, 3)),
  opening_balance_cents INTEGER,
  closing_balance_cents INTEGER,
  manual_fields_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
      json_valid(manual_fields_json)
      AND json_type(manual_fields_json) = 'array'
    ),
  row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count >= 0),
  staged_count INTEGER NOT NULL DEFAULT 0 CHECK (staged_count >= 0),
  duplicate_count INTEGER NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
  overlap_kind TEXT NOT NULL DEFAULT 'none'
    CHECK (overlap_kind IN ('none', 'exact', 'partial', 'ambiguous', 'supersession')),
  status TEXT NOT NULL DEFAULT 'preview_ready'
    CHECK (
      status IN (
        'preview_ready', 'needs_review', 'confirmed', 'duplicate', 'rejected'
      )
    ),
  review_reasons_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
      json_valid(review_reasons_json)
      AND json_type(review_reasons_json) = 'array'
    ),
  diagnostics_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
      json_valid(diagnostics_json)
      AND json_type(diagnostics_json) = 'array'
    ),
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
  evaluated_at TEXT,
  confirmed_at TEXT,
  confirmed_by TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (source_document_id, attempt_number),
  UNIQUE (source_document_id, config_fingerprint),
  CHECK (supersedes_import_id IS NULL OR supersedes_import_id <> id),
  CHECK (
    period_start_on IS NULL
    OR (
      length(period_start_on) = 10
      AND date(period_start_on, '+0 days') = period_start_on
    )
  ),
  CHECK (
    period_end_on IS NULL
    OR (
      length(period_end_on) = 10
      AND date(period_end_on, '+0 days') = period_end_on
    )
  ),
  CHECK (
    statement_issued_on IS NULL
    OR (
      length(statement_issued_on) = 10
      AND date(statement_issued_on, '+0 days') = statement_issued_on
    )
  ),
  CHECK (
    period_start_on IS NULL
    OR period_end_on IS NULL
    OR period_start_on <= period_end_on
  ),
  CHECK (staged_count + duplicate_count <= row_count),
  CHECK (
    (
      status = 'confirmed'
      AND statement_review_id IS NOT NULL
      AND confirmed_at IS NOT NULL
      AND confirmed_by IS NOT NULL
      AND length(trim(confirmed_by)) > 0
    )
    OR (
      status = 'duplicate'
      AND confirmed_at IS NOT NULL
      AND confirmed_by IS NOT NULL
      AND length(trim(confirmed_by)) > 0
    )
    OR status IN ('preview_ready', 'needs_review', 'rejected')
  )
);

CREATE INDEX idx_structured_imports_status
  ON structured_statement_imports(status, account_id, period_end_on, id);
CREATE INDEX idx_structured_imports_provider
  ON structured_statement_imports(provider_identity_hash, account_id, id);

-- Header-level evidence is deliberately separate from transaction anchors. It
-- can never be selected as a source row by add/edit operations.
CREATE TABLE structured_statement_import_headers (
  id INTEGER PRIMARY KEY,
  import_id INTEGER NOT NULL UNIQUE
    REFERENCES structured_statement_imports(id) ON DELETE RESTRICT,
  statement_review_id INTEGER NOT NULL
    REFERENCES statement_reviews(id) ON DELETE RESTRICT,
  locator_kind TEXT NOT NULL DEFAULT 'structured_header'
    CHECK (locator_kind = 'structured_header'),
  locator_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(locator_json)),
  source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE statement_field_evidence ADD COLUMN structured_header_id INTEGER
  REFERENCES structured_statement_import_headers(id) ON DELETE RESTRICT;

CREATE TABLE structured_statement_import_rows (
  id INTEGER PRIMARY KEY,
  import_id INTEGER NOT NULL
    REFERENCES structured_statement_imports(id) ON DELETE RESTRICT,
  source_row_number INTEGER NOT NULL CHECK (source_row_number > 0),
  source_anchor_id INTEGER NOT NULL
    REFERENCES statement_source_anchors(id) ON DELETE RESTRICT,
  statement_line_id INTEGER NOT NULL
    REFERENCES statement_lines(id) ON DELETE RESTRICT,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  provider_identity_hash TEXT NOT NULL DEFAULT ''
    CHECK (length(provider_identity_hash) IN (0, 64)),
  fitid_hash TEXT NOT NULL DEFAULT ''
    CHECK (length(fitid_hash) IN (0, 64)),
  weak_key_hash TEXT NOT NULL CHECK (length(weak_key_hash) = 64),
  coarse_key_hash TEXT NOT NULL CHECK (length(coarse_key_hash) = 64),
  occurrence_ordinal INTEGER NOT NULL CHECK (occurrence_ordinal >= 0),
  is_pending INTEGER NOT NULL DEFAULT 0 CHECK (is_pending IN (0, 1)),
  currency TEXT NOT NULL CHECK (length(currency) = 3),
  disposition TEXT NOT NULL DEFAULT 'staged'
    CHECK (disposition = 'staged'),
  overlap_state TEXT NOT NULL
    CHECK (overlap_state IN ('new', 'supersede_pending')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (import_id, source_row_number)
);

-- Exact weak identities have bag/multiset semantics through occurrence_ordinal.
CREATE UNIQUE INDEX idx_structured_rows_weak_staged
  ON structured_statement_import_rows(
    account_id, weak_key_hash, occurrence_ordinal
  );
CREATE INDEX idx_structured_rows_fitid
  ON structured_statement_import_rows(
    provider_identity_hash, fitid_hash, is_pending, id
  )
  WHERE fitid_hash <> '';
CREATE INDEX idx_structured_rows_coarse
  ON structured_statement_import_rows(account_id, coarse_key_hash, id);
CREATE INDEX idx_structured_rows_import
  ON structured_statement_import_rows(import_id, source_row_number);

CREATE TABLE structured_statement_row_supersessions (
  id INTEGER PRIMARY KEY,
  pending_import_row_id INTEGER NOT NULL UNIQUE
    REFERENCES structured_statement_import_rows(id) ON DELETE RESTRICT,
  posted_import_row_id INTEGER NOT NULL UNIQUE
    REFERENCES structured_statement_import_rows(id) ON DELETE RESTRICT,
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (pending_import_row_id <> posted_import_row_id)
);

CREATE TABLE structured_provider_account_bindings (
  provider_identity_hash TEXT PRIMARY KEY CHECK (length(provider_identity_hash) = 64),
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  first_import_id INTEGER NOT NULL
    REFERENCES structured_statement_imports(id) ON DELETE RESTRICT,
  verified_by TEXT NOT NULL CHECK (length(trim(verified_by)) > 0),
  verification_reason TEXT NOT NULL CHECK (length(trim(verification_reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE structured_statement_import_audit (
  id INTEGER PRIMARY KEY,
  operation_key TEXT NOT NULL UNIQUE CHECK (length(trim(operation_key)) > 0),
  import_id INTEGER NOT NULL
    REFERENCES structured_statement_imports(id) ON DELETE RESTRICT,
  event_kind TEXT NOT NULL
    CHECK (
      event_kind IN (
        'preview_created', 'confirmation_blocked', 'confirmed',
        'duplicate_confirmed', 'rejected'
      )
    ),
  old_values_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(old_values_json)),
  new_values_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(new_values_json)),
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_structured_import_audit_import
  ON structured_statement_import_audit(import_id, id);

CREATE TRIGGER structured_import_source_scope_insert
BEFORE INSERT ON structured_statement_imports
WHEN NOT EXISTS (
  SELECT 1 FROM source_documents document
  WHERE document.id=NEW.source_document_id
    AND document.sha256=NEW.source_sha256
)
OR (
  NEW.supersedes_import_id IS NULL
  AND NEW.attempt_number <> 1
)
OR (
  NEW.supersedes_import_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM structured_statement_imports prior
    WHERE prior.id=NEW.supersedes_import_id
      AND prior.source_document_id=NEW.source_document_id
      AND prior.attempt_number + 1=NEW.attempt_number
  )
)
OR (
  NEW.statement_review_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM statement_reviews review
    WHERE review.id=NEW.statement_review_id
      AND review.source_document_id=NEW.source_document_id
      AND review.account_id=NEW.account_id
      AND review.source_kind='structured_rows'
  )
)
BEGIN
  SELECT RAISE(ABORT, 'structured import source or review scope mismatch');
END;

-- Generic extraction and deterministic structured parsing must never own the
-- same immutable original. Enforce both insertion orders so a caller cannot
-- create a race between capture, preview, and a later reprocess enqueue.
CREATE TRIGGER structured_import_no_generic_job_insert
BEFORE INSERT ON structured_statement_imports
WHEN EXISTS (
  SELECT 1 FROM jobs
  WHERE source_document_id=NEW.source_document_id
    AND type='ingest_document'
)
BEGIN
  SELECT RAISE(ABORT, 'generic ingest job already owns structured import source');
END;

CREATE TRIGGER structured_ingest_job_no_import_insert
BEFORE INSERT ON jobs
WHEN NEW.type='ingest_document'
  AND NEW.source_document_id IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM structured_statement_imports imported
    WHERE imported.source_document_id=NEW.source_document_id
  )
BEGIN
  SELECT RAISE(ABORT, 'structured import already owns generic ingest source');
END;

CREATE TRIGGER structured_import_review_scope_update
BEFORE UPDATE OF statement_review_id, account_id, source_document_id
ON structured_statement_imports
WHEN NEW.statement_review_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM statement_reviews review
    WHERE review.id=NEW.statement_review_id
      AND review.source_document_id=NEW.source_document_id
      AND review.account_id=NEW.account_id
      AND review.source_kind='structured_rows'
  )
BEGIN
  SELECT RAISE(ABORT, 'structured import review scope mismatch');
END;

CREATE TRIGGER structured_review_import_scope_update
BEFORE UPDATE OF source_document_id, account_id, source_kind
ON statement_reviews
WHEN EXISTS (
  SELECT 1 FROM structured_statement_imports imported
  WHERE imported.statement_review_id=OLD.id
    AND (
      imported.source_document_id <> NEW.source_document_id
      OR imported.account_id <> NEW.account_id
      OR NEW.source_kind <> 'structured_rows'
    )
)
BEGIN
  SELECT RAISE(ABORT, 'structured review import scope mismatch');
END;

CREATE TRIGGER structured_import_identity_immutable
BEFORE UPDATE OF
  source_document_id, supersedes_import_id, attempt_number, config_fingerprint,
  source_sha256, account_id, adapter_id, adapter_version, mapping_version,
  mapping_json, provider_identity_hash, account_last4, period_start_on,
  period_end_on, statement_issued_on, currency, opening_balance_cents,
  closing_balance_cents, manual_fields_json, row_count, diagnostics_json,
  created_at
ON structured_statement_imports
BEGIN
  SELECT RAISE(ABORT, 'structured import attempt is immutable');
END;

-- An attempt has one evaluation write. The fields omitted from the immutable
-- trigger above are exactly the evaluation result, optimistic revision, and
-- update timestamp. Once evaluated, even those terminal facts are immutable.
CREATE TRIGGER structured_import_evaluation_once
BEFORE UPDATE ON structured_statement_imports
WHEN OLD.evaluated_at IS NOT NULL
  OR NEW.evaluated_at IS NULL
  OR length(trim(NEW.evaluated_at)) = 0
  OR NEW.status NOT IN ('needs_review', 'confirmed', 'duplicate', 'rejected')
BEGIN
  SELECT RAISE(ABORT, 'structured import must be evaluated exactly once');
END;

CREATE TRIGGER structured_import_revision_update
BEFORE UPDATE ON structured_statement_imports
WHEN NEW.revision <> OLD.revision + 1
BEGIN
  SELECT RAISE(ABORT, 'structured import revision must advance once');
END;

CREATE TRIGGER structured_import_status_transition
BEFORE UPDATE OF status ON structured_statement_imports
WHEN NOT (
  OLD.status IN ('preview_ready', 'needs_review')
  AND NEW.status IN ('needs_review', 'confirmed', 'duplicate', 'rejected')
)
BEGIN
  SELECT RAISE(ABORT, 'structured import status transition is invalid');
END;

CREATE TRIGGER structured_import_confirmed_binding
BEFORE UPDATE OF status ON structured_statement_imports
WHEN NEW.status='confirmed'
  AND NEW.provider_identity_hash <> ''
  AND NOT EXISTS (
    SELECT 1 FROM structured_provider_account_bindings binding
    WHERE binding.provider_identity_hash=NEW.provider_identity_hash
      AND binding.account_id=NEW.account_id
  )
BEGIN
  SELECT RAISE(ABORT, 'structured provider account binding is missing');
END;

CREATE TRIGGER structured_import_no_delete
BEFORE DELETE ON structured_statement_imports
BEGIN
  SELECT RAISE(ABORT, 'structured imports are append-only');
END;

CREATE TRIGGER structured_import_header_scope_insert
BEFORE INSERT ON structured_statement_import_headers
WHEN NOT EXISTS (
  SELECT 1
  FROM structured_statement_imports imported
  JOIN statement_reviews review ON review.id=NEW.statement_review_id
  WHERE imported.id=NEW.import_id
    AND imported.source_sha256=NEW.source_sha256
    AND review.source_document_id=imported.source_document_id
    AND review.account_id=imported.account_id
    AND review.source_kind='structured_rows'
)
BEGIN
  SELECT RAISE(ABORT, 'structured import header scope mismatch');
END;

CREATE TRIGGER structured_import_headers_no_update
BEFORE UPDATE ON structured_statement_import_headers
BEGIN
  SELECT RAISE(ABORT, 'structured import headers are immutable');
END;
CREATE TRIGGER structured_import_headers_no_delete
BEFORE DELETE ON structured_statement_import_headers
BEGIN
  SELECT RAISE(ABORT, 'structured import headers are append-only');
END;

CREATE TRIGGER structured_field_header_scope_insert
BEFORE INSERT ON statement_field_evidence
WHEN NEW.structured_header_id IS NOT NULL
  AND (
    NEW.source_anchor_id IS NOT NULL
    OR NOT EXISTS (
      SELECT 1 FROM structured_statement_import_headers header
      WHERE header.id=NEW.structured_header_id
        AND header.statement_review_id=NEW.statement_review_id
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'structured field header scope mismatch');
END;

CREATE TRIGGER structured_import_row_scope_insert
BEFORE INSERT ON structured_statement_import_rows
WHEN NOT EXISTS (
  SELECT 1
  FROM structured_statement_imports imported
  JOIN statement_reviews review
    ON review.id=imported.statement_review_id
  JOIN statement_source_anchors anchor
    ON anchor.id=NEW.source_anchor_id
   AND anchor.statement_review_id=review.id
   AND anchor.locator_kind='raw_row'
  JOIN statement_lines line
    ON line.id=NEW.statement_line_id
   AND line.source_document_id=imported.source_document_id
   AND line.account_id=imported.account_id
   AND line.source_anchor_id=anchor.id
  WHERE imported.id=NEW.import_id
    AND imported.account_id=NEW.account_id
    AND imported.provider_identity_hash=NEW.provider_identity_hash
    AND CAST(
      COALESCE(
        json_extract(anchor.locator_json, '$.row_number'),
        json_extract(anchor.locator_json, '$.transaction_index')
      ) AS INTEGER
    )=NEW.source_row_number
)
BEGIN
  SELECT RAISE(ABORT, 'structured import row scope mismatch');
END;

CREATE TRIGGER structured_import_rows_no_update
BEFORE UPDATE ON structured_statement_import_rows
BEGIN
  SELECT RAISE(ABORT, 'structured import row identities are immutable');
END;
CREATE TRIGGER structured_import_rows_no_delete
BEFORE DELETE ON structured_statement_import_rows
BEGIN
  SELECT RAISE(ABORT, 'structured import row identities are append-only');
END;

CREATE TRIGGER structured_supersession_scope_insert
BEFORE INSERT ON structured_statement_row_supersessions
WHEN NOT EXISTS (
  SELECT 1
  FROM structured_statement_import_rows pending
  JOIN structured_statement_import_rows posted
    ON posted.id=NEW.posted_import_row_id
  JOIN statement_lines pending_line
    ON pending_line.id=pending.statement_line_id
  JOIN statement_lines posted_line
    ON posted_line.id=posted.statement_line_id
  WHERE pending.id=NEW.pending_import_row_id
    AND pending.account_id=posted.account_id
    AND pending.provider_identity_hash=posted.provider_identity_hash
    AND pending.fitid_hash=posted.fitid_hash
    AND pending.fitid_hash <> ''
    AND pending.is_pending=1
    AND posted.is_pending=0
    AND pending.disposition='staged'
    AND posted.disposition='staged'
    AND pending_line.review_disposition='excluded'
    AND posted_line.review_disposition='active'
    AND pending_line.is_pending=1
    AND posted_line.is_pending=0
)
BEGIN
  SELECT RAISE(ABORT, 'structured row supersession scope mismatch');
END;
CREATE TRIGGER structured_supersessions_no_update
BEFORE UPDATE ON structured_statement_row_supersessions
BEGIN
  SELECT RAISE(ABORT, 'structured row supersessions are immutable');
END;
CREATE TRIGGER structured_supersessions_no_delete
BEFORE DELETE ON structured_statement_row_supersessions
BEGIN
  SELECT RAISE(ABORT, 'structured row supersessions are append-only');
END;

CREATE TRIGGER structured_provider_binding_scope_insert
BEFORE INSERT ON structured_provider_account_bindings
WHEN NOT EXISTS (
  SELECT 1 FROM structured_statement_imports imported
  WHERE imported.id=NEW.first_import_id
    AND imported.provider_identity_hash=NEW.provider_identity_hash
    AND imported.account_id=NEW.account_id
)
BEGIN
  SELECT RAISE(ABORT, 'structured provider binding scope mismatch');
END;
CREATE TRIGGER structured_provider_bindings_no_update
BEFORE UPDATE ON structured_provider_account_bindings
BEGIN
  SELECT RAISE(ABORT, 'structured provider bindings are immutable');
END;
CREATE TRIGGER structured_provider_bindings_no_delete
BEFORE DELETE ON structured_provider_account_bindings
BEGIN
  SELECT RAISE(ABORT, 'structured provider bindings are append-only');
END;

CREATE TRIGGER structured_import_audit_no_update
BEFORE UPDATE ON structured_statement_import_audit
BEGIN
  SELECT RAISE(ABORT, 'structured import audit is append-only');
END;
CREATE TRIGGER structured_import_audit_no_delete
BEFORE DELETE ON structured_statement_import_audit
BEGIN
  SELECT RAISE(ABORT, 'structured import audit is append-only');
END;
