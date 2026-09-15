-- FN-149B: scoped, auditable merchant and expense-category knowledge.
--
-- Merchant identity and category are deliberately independent claims.  Every
-- durable fact is append-only and scope-bound; model, web, and legacy evidence
-- can only remain untrusted until a distinct human acceptance exists.

CREATE TABLE merchant_entities (
  id INTEGER PRIMARY KEY,
  entity_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(entity_key)) BETWEEN 1 AND 160),
  canonical_name TEXT NOT NULL
    CHECK (length(trim(canonical_name)) BETWEEN 1 AND 160),
  normalized_name TEXT NOT NULL
    CHECK (length(trim(normalized_name)) BETWEEN 1 AND 160),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE merchant_descriptor_patterns (
  id INTEGER PRIMARY KEY,
  pattern_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(pattern_key)) BETWEEN 1 AND 200),
  normalization_version TEXT NOT NULL
    CHECK (normalization_version = 'descriptor-v2'),
  pattern_kind TEXT NOT NULL CHECK (pattern_kind = 'exact_tokens'),
  pattern_json TEXT NOT NULL
    CHECK (
      json_valid(pattern_json)
      AND json_type(pattern_json) = 'array'
      AND json_array_length(pattern_json) BETWEEN 1 AND 24
    ),
  pattern_fingerprint TEXT NOT NULL
    CHECK (length(pattern_fingerprint) = 64),
  household_scope TEXT NOT NULL
    CHECK (length(trim(household_scope)) BETWEEN 1 AND 80),
  account_id INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
  provider_identity_hash TEXT NOT NULL DEFAULT ''
    CHECK (length(provider_identity_hash) IN (0, 64)),
  processor_family TEXT NOT NULL DEFAULT ''
    CHECK (length(processor_family) <= 64),
  region TEXT NOT NULL DEFAULT '' CHECK (length(region) <= 32),
  scope_fingerprint TEXT NOT NULL
    CHECK (length(scope_fingerprint) = 64),
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (pattern_fingerprint, scope_fingerprint),
  CHECK (provider_identity_hash = '' OR account_id IS NOT NULL)
);

CREATE INDEX idx_merchant_patterns_scope
  ON merchant_descriptor_patterns(
    household_scope, account_id, provider_identity_hash,
    processor_family, region, pattern_fingerprint
  );

CREATE TABLE merchant_resolution_claims (
  id INTEGER PRIMARY KEY,
  claim_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(claim_key)) BETWEEN 1 AND 200),
  pattern_id INTEGER NOT NULL
    REFERENCES merchant_descriptor_patterns(id) ON DELETE RESTRICT,
  claim_kind TEXT NOT NULL
    CHECK (claim_kind IN ('canonical_merchant', 'expense_category')),
  merchant_entity_id INTEGER
    REFERENCES merchant_entities(id) ON DELETE RESTRICT,
  category_id INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
  supersedes_claim_id INTEGER
    REFERENCES merchant_resolution_claims(id) ON DELETE RESTRICT,
  created_by TEXT NOT NULL CHECK (length(trim(created_by)) > 0),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (
      claim_kind = 'canonical_merchant'
      AND merchant_entity_id IS NOT NULL
      AND category_id IS NULL
    )
    OR (
      claim_kind = 'expense_category'
      AND merchant_entity_id IS NULL
      AND category_id IS NOT NULL
    )
  ),
  CHECK (supersedes_claim_id IS NULL OR supersedes_claim_id <> id)
);

CREATE INDEX idx_merchant_claims_pattern
  ON merchant_resolution_claims(pattern_id, claim_kind, id);
CREATE INDEX idx_merchant_claims_supersedes
  ON merchant_resolution_claims(supersedes_claim_id)
  WHERE supersedes_claim_id IS NOT NULL;

CREATE TABLE merchant_resolution_events (
  id INTEGER PRIMARY KEY,
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) BETWEEN 1 AND 240),
  claim_id INTEGER NOT NULL
    REFERENCES merchant_resolution_claims(id) ON DELETE RESTRICT,
  event_kind TEXT NOT NULL
    CHECK (
      event_kind IN (
        'legacy_imported', 'proposed', 'accepted', 'corrected',
        'rejected', 'retired', 'undo'
      )
    ),
  trust_state TEXT NOT NULL
    CHECK (
      trust_state IN (
        'legacy_unverified', 'untrusted_proposal', 'human_confirmed',
        'rejected', 'retired'
      )
    ),
  actor_kind TEXT NOT NULL
    CHECK (
      actor_kind IN (
        'human', 'system', 'migration', 'model', 'web', 'evaluator'
      )
    ),
  actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 160),
  reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
  provenance_kind TEXT NOT NULL
    CHECK (
      provenance_kind IN (
        'legacy_alias', 'manual_match', 'manual_recategorization',
        'operator', 'model_proposal', 'web_search',
        'deterministic_fixture', 'system'
      )
    ),
  provenance_ref TEXT NOT NULL DEFAULT ''
    CHECK (length(provenance_ref) <= 240),
  statement_line_id INTEGER
    REFERENCES statement_lines(id) ON DELETE RESTRICT,
  transaction_id INTEGER REFERENCES transactions(id) ON DELETE RESTRICT,
  transaction_split_id INTEGER
    REFERENCES transaction_splits(id) ON DELETE RESTRICT,
  source_anchor_id INTEGER
    REFERENCES statement_source_anchors(id) ON DELETE RESTRICT,
  proposed_action_id INTEGER
    REFERENCES proposed_actions(id) ON DELETE RESTRICT,
  citation_url TEXT NOT NULL DEFAULT '' CHECK (length(citation_url) <= 1000),
  consent_version TEXT NOT NULL DEFAULT ''
    CHECK (length(consent_version) <= 120),
  reverses_event_id INTEGER
    REFERENCES merchant_resolution_events(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (event_kind = 'legacy_imported' AND trust_state = 'legacy_unverified')
    OR (event_kind = 'proposed' AND trust_state = 'untrusted_proposal')
    OR (
      event_kind IN ('accepted', 'corrected')
      AND trust_state = 'human_confirmed'
    )
    OR (event_kind = 'rejected' AND trust_state = 'rejected')
    OR (event_kind IN ('retired', 'undo') AND trust_state = 'retired')
  ),
  CHECK (
    actor_kind NOT IN ('model', 'web')
    OR (
      event_kind = 'proposed'
      AND trust_state = 'untrusted_proposal'
    )
  ),
  CHECK (
    trust_state <> 'human_confirmed'
    OR actor_kind = 'human'
    OR (
      actor_kind = 'evaluator'
      AND provenance_kind = 'deterministic_fixture'
    )
  ),
  CHECK (
    trust_state <> 'human_confirmed'
    OR actor_kind = 'evaluator'
    OR statement_line_id IS NOT NULL
    OR transaction_id IS NOT NULL
    OR proposed_action_id IS NOT NULL
  ),
  CHECK (
    provenance_kind <> 'web_search'
    OR event_kind <> 'proposed'
    OR length(trim(citation_url)) > 0
  ),
  CHECK (
    provenance_kind <> 'web_search'
    OR trust_state <> 'human_confirmed'
    OR (
      length(trim(citation_url)) > 0
      AND length(trim(consent_version)) > 0
    )
  ),
  CHECK (
    citation_url = ''
    OR provenance_kind = 'web_search'
  ),
  CHECK (
    source_anchor_id IS NULL
    OR statement_line_id IS NOT NULL
  ),
  CHECK (
    reverses_event_id IS NULL
    OR event_kind IN ('retired', 'undo')
  )
);

CREATE INDEX idx_merchant_events_claim
  ON merchant_resolution_events(claim_id, id);
CREATE INDEX idx_merchant_events_statement
  ON merchant_resolution_events(statement_line_id, claim_id, id)
  WHERE statement_line_id IS NOT NULL;
CREATE INDEX idx_merchant_events_transaction
  ON merchant_resolution_events(transaction_id, claim_id, id)
  WHERE transaction_id IS NOT NULL;
CREATE INDEX idx_merchant_events_split
  ON merchant_resolution_events(transaction_split_id, claim_id, id)
  WHERE transaction_split_id IS NOT NULL;
CREATE UNIQUE INDEX uq_merchant_event_reversal
  ON merchant_resolution_events(reverses_event_id)
  WHERE reverses_event_id IS NOT NULL;

-- Existing global aliases are preserved as historical, untrusted evidence.
-- Merchant and category become separate claims even though the legacy row
-- coupled them.
INSERT INTO merchant_entities(
  entity_key, canonical_name, normalized_name, created_by
)
SELECT
  'legacy-merchant:' || alias.id,
  CASE
    WHEN length(trim(alias.canonical)) > 0 THEN trim(alias.canonical)
    ELSE trim(alias.raw_pattern)
  END,
  trim(alias.raw_pattern),
  'migration:035'
FROM merchant_aliases alias
WHERE length(trim(alias.raw_pattern)) > 0;

INSERT INTO merchant_descriptor_patterns(
  pattern_key, normalization_version, pattern_kind, pattern_json,
  pattern_fingerprint, household_scope, scope_fingerprint, created_by
)
SELECT
  'legacy-pattern:' || alias.id,
  'descriptor-v2',
  'exact_tokens',
  json_array(trim(alias.raw_pattern)),
  lower(printf('%064x', alias.id + 1000000)),
  COALESCE(
    (
      SELECT value FROM app_settings
      WHERE key = '__finn_nancy_database_uuid'
    ),
    'legacy-local-database'
  ),
  lower(printf('%064x', 1)),
  'migration:035'
FROM merchant_aliases alias
WHERE length(trim(alias.raw_pattern)) > 0;

INSERT INTO merchant_resolution_claims(
  claim_key, pattern_id, claim_kind, merchant_entity_id, created_by
)
SELECT
  'legacy-merchant-claim:' || alias.id,
  pattern.id,
  'canonical_merchant',
  entity.id,
  'migration:035'
FROM merchant_aliases alias
JOIN merchant_descriptor_patterns pattern
  ON pattern.pattern_key = 'legacy-pattern:' || alias.id
JOIN merchant_entities entity
  ON entity.entity_key = 'legacy-merchant:' || alias.id
WHERE length(trim(alias.raw_pattern)) > 0;

INSERT INTO merchant_resolution_events(
  operation_key, claim_id, event_kind, trust_state, actor_kind, actor,
  reason, provenance_kind, provenance_ref
)
SELECT
  'migration:035:legacy-merchant:' || alias.id,
  claim.id,
  'legacy_imported',
  'legacy_unverified',
  'migration',
  'migration:035',
  'preserved legacy merchant alias as unverified evidence',
  'legacy_alias',
  'merchant_aliases:' || alias.id
FROM merchant_aliases alias
JOIN merchant_resolution_claims claim
  ON claim.claim_key = 'legacy-merchant-claim:' || alias.id
WHERE length(trim(alias.raw_pattern)) > 0;

INSERT INTO merchant_resolution_claims(
  claim_key, pattern_id, claim_kind, category_id, created_by
)
SELECT
  'legacy-category-claim:' || alias.id,
  pattern.id,
  'expense_category',
  alias.category_id,
  'migration:035'
FROM merchant_aliases alias
JOIN merchant_descriptor_patterns pattern
  ON pattern.pattern_key = 'legacy-pattern:' || alias.id
JOIN categories category
  ON category.id = alias.category_id
 AND category.kind = 'expense'
WHERE length(trim(alias.raw_pattern)) > 0
  AND alias.category_id IS NOT NULL;

INSERT INTO merchant_resolution_events(
  operation_key, claim_id, event_kind, trust_state, actor_kind, actor,
  reason, provenance_kind, provenance_ref
)
SELECT
  'migration:035:legacy-category:' || alias.id,
  claim.id,
  'legacy_imported',
  'legacy_unverified',
  'migration',
  'migration:035',
  'preserved legacy category alias as unverified evidence',
  'legacy_alias',
  'merchant_aliases:' || alias.id
FROM merchant_aliases alias
JOIN merchant_resolution_claims claim
  ON claim.claim_key = 'legacy-category-claim:' || alias.id
WHERE length(trim(alias.raw_pattern)) > 0
  AND alias.category_id IS NOT NULL;

-- New writes must carry exact descriptor-v2 tokens and the current opaque
-- database identity.  Legacy backfill above is deliberately inert.
CREATE TRIGGER merchant_pattern_scope_insert
BEFORE INSERT ON merchant_descriptor_patterns
WHEN NOT EXISTS (
  SELECT 1
  FROM app_settings setting
  WHERE setting.key = '__finn_nancy_database_uuid'
    AND setting.value = NEW.household_scope
)
OR (
  NEW.provider_identity_hash <> ''
  AND NOT EXISTS (
    SELECT 1
    FROM structured_provider_account_bindings binding
    WHERE binding.provider_identity_hash = NEW.provider_identity_hash
      AND binding.account_id = NEW.account_id
  )
)
BEGIN
  SELECT RAISE(ABORT, 'merchant descriptor scope is invalid');
END;

CREATE TRIGGER merchant_pattern_tokens_insert
BEFORE INSERT ON merchant_descriptor_patterns
WHEN EXISTS (
  SELECT 1
  FROM json_each(NEW.pattern_json) token
  WHERE token.type <> 'text'
     OR length(trim(CAST(token.value AS TEXT))) NOT BETWEEN 1 AND 64
     OR CAST(token.value AS TEXT) <> lower(trim(CAST(token.value AS TEXT)))
     OR instr(CAST(token.value AS TEXT), ' ') > 0
)
BEGIN
  SELECT RAISE(ABORT, 'merchant descriptor tokens are invalid');
END;

CREATE TRIGGER merchant_claim_category_insert
BEFORE INSERT ON merchant_resolution_claims
WHEN NEW.claim_kind = 'expense_category'
  AND NOT EXISTS (
    SELECT 1 FROM categories category
    WHERE category.id = NEW.category_id
      AND category.kind = 'expense'
      AND category.name <> 'Uncategorized'
  )
BEGIN
  SELECT RAISE(ABORT, 'merchant category claim must target an expense category');
END;

CREATE TRIGGER merchant_claim_supersession_insert
BEFORE INSERT ON merchant_resolution_claims
WHEN NEW.supersedes_claim_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM merchant_resolution_claims prior
    JOIN merchant_descriptor_patterns prior_pattern
      ON prior_pattern.id = prior.pattern_id
    JOIN merchant_descriptor_patterns replacement_pattern
      ON replacement_pattern.id = NEW.pattern_id
    WHERE prior.id = NEW.supersedes_claim_id
      AND prior.claim_kind = NEW.claim_kind
      AND prior_pattern.household_scope = replacement_pattern.household_scope
  )
BEGIN
  SELECT RAISE(ABORT, 'merchant correction claim scope is invalid');
END;

CREATE TRIGGER merchant_event_transition_insert
BEFORE INSERT ON merchant_resolution_events
BEGIN
  SELECT CASE
    WHEN NOT EXISTS (
      SELECT 1 FROM merchant_resolution_events
      WHERE claim_id = NEW.claim_id
    )
    AND NEW.event_kind NOT IN (
      'legacy_imported', 'proposed', 'accepted', 'corrected'
    )
    THEN RAISE(ABORT, 'merchant claim initial event is invalid')
  END;

  SELECT CASE
    WHEN EXISTS (
      SELECT 1 FROM merchant_resolution_events
      WHERE claim_id = NEW.claim_id
    )
    AND (
      NEW.event_kind IN ('legacy_imported', 'proposed', 'corrected')
      OR (
        NEW.event_kind IN ('accepted', 'rejected')
        AND COALESCE((
          SELECT prior.event_kind
          FROM merchant_resolution_events prior
          WHERE prior.claim_id = NEW.claim_id
          ORDER BY prior.id DESC
          LIMIT 1
        ), '') NOT IN ('legacy_imported', 'proposed')
      )
      OR (
        NEW.event_kind IN ('retired', 'undo')
        AND COALESCE((
          SELECT prior.event_kind
          FROM merchant_resolution_events prior
          WHERE prior.claim_id = NEW.claim_id
          ORDER BY prior.id DESC
          LIMIT 1
        ), '') NOT IN ('accepted', 'corrected')
      )
    )
    THEN RAISE(ABORT, 'merchant claim event transition is invalid')
  END;

  SELECT CASE
    WHEN NEW.event_kind IN ('retired', 'undo')
    AND NOT EXISTS (
      SELECT 1
      FROM merchant_resolution_events accepted
      WHERE accepted.id = NEW.reverses_event_id
        AND accepted.claim_id = NEW.claim_id
        AND accepted.event_kind IN ('accepted', 'corrected')
    )
    THEN RAISE(ABORT, 'merchant claim reversal must reference its acceptance')
  END;
END;

CREATE TRIGGER merchant_event_correction_insert
BEFORE INSERT ON merchant_resolution_events
WHEN NEW.event_kind = 'corrected'
  AND NOT EXISTS (
    SELECT 1
    FROM merchant_resolution_claims replacement
    JOIN merchant_resolution_claims prior
      ON prior.id = replacement.supersedes_claim_id
    JOIN merchant_resolution_events prior_event
      ON prior_event.claim_id = prior.id
    WHERE replacement.id = NEW.claim_id
      AND replacement.claim_kind = prior.claim_kind
      AND prior_event.id = (
        SELECT MAX(latest.id)
        FROM merchant_resolution_events latest
        WHERE latest.claim_id = prior.id
      )
      AND prior_event.event_kind IN ('accepted', 'corrected')
      AND prior_event.trust_state = 'human_confirmed'
  )
BEGIN
  SELECT RAISE(ABORT, 'merchant correction must supersede an active trusted claim');
END;

CREATE TRIGGER merchant_event_evidence_scope_insert
BEFORE INSERT ON merchant_resolution_events
WHEN (
  NEW.source_anchor_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM statement_lines line
    WHERE line.id = NEW.statement_line_id
      AND line.source_anchor_id = NEW.source_anchor_id
  )
)
OR (
  NEW.statement_line_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM merchant_resolution_claims claim
    JOIN merchant_descriptor_patterns pattern ON pattern.id = claim.pattern_id
    JOIN statement_lines line ON line.id = NEW.statement_line_id
    WHERE claim.id = NEW.claim_id
      AND (
        pattern.account_id IS NULL
        OR pattern.account_id = line.account_id
      )
      AND (
        pattern.provider_identity_hash = ''
        OR EXISTS (
          SELECT 1
          FROM structured_statement_import_rows imported_row
          WHERE imported_row.statement_line_id = line.id
            AND imported_row.provider_identity_hash
                  = pattern.provider_identity_hash
        )
      )
  )
)
OR (
  NEW.transaction_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM merchant_resolution_claims claim
    JOIN merchant_descriptor_patterns pattern ON pattern.id = claim.pattern_id
    JOIN transactions txn ON txn.id = NEW.transaction_id
    WHERE claim.id = NEW.claim_id
      AND (
        pattern.account_id IS NULL
        OR pattern.account_id = txn.account_id
      )
  )
)
OR (
  NEW.transaction_split_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM merchant_resolution_claims claim
    JOIN transaction_splits split
      ON split.id = NEW.transaction_split_id
    WHERE claim.id = NEW.claim_id
      AND NEW.transaction_id IS NOT NULL
      AND split.transaction_id = NEW.transaction_id
      AND (
        claim.claim_kind <> 'expense_category'
        OR NEW.event_kind NOT IN ('accepted', 'corrected')
        OR split.category_id = claim.category_id
      )
  )
)
OR (
  NEW.statement_line_id IS NOT NULL
  AND NEW.transaction_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM statement_lines line
    WHERE line.id = NEW.statement_line_id
      AND line.matched_transaction_id = NEW.transaction_id
      AND line.review_disposition = 'active'
  )
)
BEGIN
  SELECT RAISE(ABORT, 'merchant resolution evidence scope mismatch');
END;

CREATE TRIGGER merchant_event_category_acceptance_insert
BEFORE INSERT ON merchant_resolution_events
WHEN NEW.event_kind IN ('accepted', 'corrected')
  AND EXISTS (
    SELECT 1
    FROM merchant_resolution_claims claim
    WHERE claim.id = NEW.claim_id
      AND claim.claim_kind = 'expense_category'
  )
  AND (
    NEW.transaction_id IS NULL
    OR NEW.transaction_split_id IS NULL
  )
BEGIN
  SELECT RAISE(
    ABORT,
    'accepted category claim requires transaction and split evidence'
  );
END;

CREATE TRIGGER merchant_entities_no_update
BEFORE UPDATE ON merchant_entities
BEGIN
  SELECT RAISE(ABORT, 'merchant entities are immutable');
END;
CREATE TRIGGER merchant_entities_no_delete
BEFORE DELETE ON merchant_entities
BEGIN
  SELECT RAISE(ABORT, 'merchant entities are append-only');
END;

CREATE TRIGGER merchant_patterns_no_update
BEFORE UPDATE ON merchant_descriptor_patterns
BEGIN
  SELECT RAISE(ABORT, 'merchant descriptor patterns are immutable');
END;
CREATE TRIGGER merchant_patterns_no_delete
BEFORE DELETE ON merchant_descriptor_patterns
BEGIN
  SELECT RAISE(ABORT, 'merchant descriptor patterns are append-only');
END;

CREATE TRIGGER merchant_claims_no_update
BEFORE UPDATE ON merchant_resolution_claims
BEGIN
  SELECT RAISE(ABORT, 'merchant resolution claims are immutable');
END;
CREATE TRIGGER merchant_claims_no_delete
BEFORE DELETE ON merchant_resolution_claims
BEGIN
  SELECT RAISE(ABORT, 'merchant resolution claims are append-only');
END;

CREATE TRIGGER merchant_events_no_update
BEFORE UPDATE ON merchant_resolution_events
BEGIN
  SELECT RAISE(ABORT, 'merchant resolution events are append-only');
END;
CREATE TRIGGER merchant_events_no_delete
BEFORE DELETE ON merchant_resolution_events
BEGIN
  SELECT RAISE(ABORT, 'merchant resolution events are append-only');
END;

CREATE TRIGGER legacy_merchant_aliases_no_insert
BEFORE INSERT ON merchant_aliases
BEGIN
  SELECT RAISE(
    ABORT,
    'merchant_aliases is frozen; use scoped merchant resolution claims'
  );
END;
CREATE TRIGGER legacy_merchant_aliases_no_update
BEFORE UPDATE ON merchant_aliases
BEGIN
  SELECT RAISE(
    ABORT,
    'merchant_aliases is frozen; use scoped merchant resolution claims'
  );
END;
CREATE TRIGGER legacy_merchant_aliases_no_delete
BEFORE DELETE ON merchant_aliases
BEGIN
  SELECT RAISE(
    ABORT,
    'merchant_aliases is frozen; use scoped merchant resolution claims'
  );
END;

-- Process-local claimed sets are useful feedback, but persistence must also
-- prohibit two active statement lines from consuming one ledger transaction.
CREATE UNIQUE INDEX uq_active_statement_match_transaction
  ON statement_lines(matched_transaction_id)
  WHERE matched_transaction_id IS NOT NULL
    AND review_disposition = 'active'
    AND match_status IN ('matched', 'promoted');

CREATE VIEW v_current_merchant_resolution_claims AS
SELECT
  claim.id AS claim_id,
  claim.claim_key,
  claim.claim_kind,
  claim.pattern_id,
  claim.merchant_entity_id,
  claim.category_id,
  claim.supersedes_claim_id,
  pattern.pattern_key,
  pattern.normalization_version,
  pattern.pattern_kind,
  pattern.pattern_json,
  pattern.pattern_fingerprint,
  pattern.household_scope,
  pattern.account_id,
  pattern.provider_identity_hash,
  pattern.processor_family,
  pattern.region,
  pattern.scope_fingerprint,
  entity.canonical_name,
  entity.normalized_name,
  category.name AS category_name,
  event.id AS current_event_id,
  event.event_kind,
  event.trust_state,
  event.actor_kind,
  event.actor,
  event.reason,
  event.provenance_kind,
  event.provenance_ref,
  event.statement_line_id,
  event.transaction_id,
  event.transaction_split_id,
  event.source_anchor_id,
  event.proposed_action_id,
  event.citation_url,
  event.consent_version,
  event.created_at AS event_at
FROM merchant_resolution_claims claim
JOIN merchant_descriptor_patterns pattern ON pattern.id = claim.pattern_id
LEFT JOIN merchant_entities entity ON entity.id = claim.merchant_entity_id
LEFT JOIN categories category ON category.id = claim.category_id
JOIN merchant_resolution_events event ON event.claim_id = claim.id
WHERE event.id = (
  SELECT MAX(latest.id)
  FROM merchant_resolution_events latest
  WHERE latest.claim_id = claim.id
);

CREATE VIEW v_active_merchant_resolution_claims AS
SELECT
  claim.id AS claim_id,
  claim.claim_key,
  claim.claim_kind,
  claim.pattern_id,
  claim.merchant_entity_id,
  claim.category_id,
  claim.supersedes_claim_id,
  pattern.pattern_key,
  pattern.normalization_version,
  pattern.pattern_kind,
  pattern.pattern_json,
  pattern.pattern_fingerprint,
  pattern.household_scope,
  pattern.account_id,
  pattern.provider_identity_hash,
  pattern.processor_family,
  pattern.region,
  pattern.scope_fingerprint,
  entity.canonical_name,
  entity.normalized_name,
  category.name AS category_name,
  event.id AS acceptance_event_id,
  event.actor,
  event.reason,
  event.provenance_kind,
  event.provenance_ref,
  event.statement_line_id,
  event.transaction_id,
  event.transaction_split_id,
  event.source_anchor_id,
  event.proposed_action_id,
  event.citation_url,
  event.consent_version,
  event.created_at AS accepted_at
FROM merchant_resolution_claims claim
JOIN merchant_descriptor_patterns pattern ON pattern.id = claim.pattern_id
LEFT JOIN merchant_entities entity ON entity.id = claim.merchant_entity_id
LEFT JOIN categories category ON category.id = claim.category_id
JOIN merchant_resolution_events event ON event.claim_id = claim.id
WHERE event.id = (
  SELECT MAX(latest.id)
  FROM merchant_resolution_events latest
  WHERE latest.claim_id = claim.id
)
  AND event.event_kind IN ('accepted', 'corrected')
  AND event.trust_state = 'human_confirmed';

CREATE VIEW v_transaction_split_category_resolution AS
SELECT
  active.transaction_id,
  active.transaction_split_id,
  MIN(active.category_id) AS category_id,
  MIN(active.category_name) AS category_name,
  COUNT(DISTINCT active.claim_id) AS accepted_claim_count,
  MAX(active.accepted_at) AS accepted_at
FROM v_active_merchant_resolution_claims active
WHERE active.claim_kind = 'expense_category'
  AND active.transaction_id IS NOT NULL
  AND active.transaction_split_id IS NOT NULL
GROUP BY active.transaction_id, active.transaction_split_id
HAVING COUNT(DISTINCT active.category_id) = 1;

CREATE VIEW v_merchant_category_statistics AS
SELECT
  active.pattern_id,
  active.category_id,
  active.category_name,
  COUNT(DISTINCT CASE
    WHEN active.transaction_split_id IS NOT NULL
      THEN 'split:' || active.transaction_split_id
    WHEN active.transaction_id IS NOT NULL
      THEN 'transaction:' || active.transaction_id
    WHEN active.statement_line_id IS NOT NULL
      THEN 'statement-line:' || active.statement_line_id
    ELSE 'claim:' || active.claim_id
  END) AS accepted_evidence_count,
  MIN(active.accepted_at) AS first_accepted_at,
  MAX(active.accepted_at) AS last_accepted_at
FROM v_active_merchant_resolution_claims active
WHERE active.claim_kind = 'expense_category'
GROUP BY active.pattern_id, active.category_id, active.category_name;

CREATE VIEW v_expense_resolution_status AS
SELECT
  txn.id AS transaction_id,
  split.id AS transaction_split_id,
  txn.posted_on,
  txn.amount_cents AS transaction_amount_cents,
  split.amount_cents AS split_amount_cents,
  split.category_id,
  category.name AS category_name,
  category.kind AS category_kind,
  CASE
    WHEN category.kind <> 'expense' THEN 'unresolved'
    WHEN category.name = 'Uncategorized' THEN 'unresolved'
    WHEN resolved.transaction_split_id IS NULL THEN 'unresolved'
    WHEN resolved.category_id <> split.category_id THEN 'unresolved'
    ELSE 'resolved'
  END AS resolution_status,
  resolved.accepted_claim_count,
  resolved.accepted_at
FROM transactions txn
JOIN transaction_splits split ON split.transaction_id = txn.id
JOIN categories category ON category.id = split.category_id
LEFT JOIN v_transaction_split_category_resolution resolved
  ON resolved.transaction_id = txn.id
 AND resolved.transaction_split_id = split.id
WHERE txn.flow_kind IN ('purchase', 'fee');

-- Cash movement and category trust are separate controls. This view accounts
-- for every purchase/fee split, including purpose-category mistakes, and names
-- exactly what is usable in accepted category breakdowns.
CREATE VIEW v_expense_resolution_monthly_control AS
SELECT
  strftime('%Y-%m', posted_on) AS month,
  COUNT(DISTINCT transaction_id) AS transaction_count,
  COUNT(*) AS split_count,
  COALESCE(SUM(ABS(split_amount_cents)), 0) AS money_out_cents,
  COALESCE(SUM(
    CASE WHEN resolution_status='resolved'
      THEN ABS(split_amount_cents) ELSE 0 END
  ), 0) AS resolved_expense_cents,
  COALESCE(SUM(
    CASE WHEN resolution_status='unresolved'
      THEN ABS(split_amount_cents) ELSE 0 END
  ), 0) AS excluded_expense_cents,
  SUM(CASE WHEN resolution_status='resolved' THEN 1 ELSE 0 END)
    AS resolved_split_count,
  SUM(CASE WHEN resolution_status='unresolved' THEN 1 ELSE 0 END)
    AS unresolved_split_count
FROM v_expense_resolution_status
GROUP BY strftime('%Y-%m', posted_on);

CREATE VIEW v_resolved_expense_category_monthly AS
SELECT
  strftime('%Y-%m', status.posted_on) AS month,
  status.category_id,
  status.category_name,
  'expense' AS category_kind,
  category.brand_owner,
  category.color,
  SUM(status.split_amount_cents) AS amount_cents,
  -SUM(status.split_amount_cents) AS magnitude_cents,
  COUNT(*) AS resolved_split_count
FROM v_expense_resolution_status status
JOIN categories category ON category.id=status.category_id
WHERE status.resolution_status='resolved'
GROUP BY
  strftime('%Y-%m', status.posted_on),
  status.category_id,
  status.category_name,
  category.brand_owner,
  category.color;

-- Budget performance is a category-purpose report, so actuals must use the
-- same accepted split-level category evidence as other category breakdowns.
-- Unresolved money-out remains visible in v_expense_resolution_monthly_control
-- but cannot consume a category budget until its assignment is accepted.
CREATE VIEW v_report_budget_vs_actual AS
WITH expense_categories AS (
  SELECT id AS category_id
  FROM categories
  WHERE kind = 'expense'
),
actuals AS (
  SELECT
    month,
    category_id,
    SUM(magnitude_cents) AS actual_cents
  FROM v_resolved_expense_category_monthly
  GROUP BY month, category_id
),
grid AS (
  SELECT spine.month, category.category_id
  FROM v_month_spine spine
  CROSS JOIN expense_categories category
),
resolved AS (
  SELECT
    grid.month,
    grid.category_id,
    COALESCE(monthly.amount_cents, default_budget.amount_cents, 0)
      AS budget_cents,
    COALESCE(
      monthly.owner_member_id,
      default_budget.owner_member_id
    ) AS owner_member_id,
    COALESCE(monthly.owner, default_budget.owner, 'shared')
      AS legacy_budget_owner
  FROM grid
  LEFT JOIN budgets monthly
    ON monthly.category_id = grid.category_id
   AND monthly.period_month = grid.month
  LEFT JOIN budgets default_budget
    ON default_budget.category_id = grid.category_id
   AND default_budget.period_month = ''
)
SELECT
  resolved.month,
  resolved.category_id,
  category.name AS category_name,
  category.brand_owner,
  category.color,
  category.is_leisure,
  resolved.budget_cents,
  resolved.owner_member_id,
  CASE
    WHEN member.name IS NOT NULL THEN member.name
    WHEN resolved.legacy_budget_owner = 'sample_member_a' THEN 'Sample Member A'
    WHEN resolved.legacy_budget_owner = 'sample_member_b' THEN 'Sample Member B'
    ELSE 'Shared'
  END AS budget_owner,
  COALESCE(actuals.actual_cents, 0) AS actual_cents,
  resolved.budget_cents - COALESCE(actuals.actual_cents, 0)
    AS remaining_cents,
  CASE
    WHEN resolved.budget_cents > 0 THEN ROUND(
      100.0 * COALESCE(actuals.actual_cents, 0)
      / resolved.budget_cents,
      1
    )
    ELSE NULL
  END AS pct_used
FROM resolved
JOIN categories category
  ON category.id = resolved.category_id
LEFT JOIN household_members member
  ON member.id = resolved.owner_member_id
LEFT JOIN actuals
  ON actuals.category_id = resolved.category_id
 AND actuals.month = resolved.month
WHERE resolved.budget_cents > 0
   OR COALESCE(actuals.actual_cents, 0) > 0;

-- Income remains governed by accepted flow meaning. Expense-purpose breakdowns
-- come only from accepted split-level category evidence.
CREATE VIEW v_report_category_monthly AS
SELECT
  month,
  category_id,
  category_name,
  category_kind,
  brand_owner,
  color,
  amount_cents,
  magnitude_cents
FROM v_category_monthly
WHERE category_kind='income'
UNION ALL
SELECT
  month,
  category_id,
  category_name,
  category_kind,
  brand_owner,
  color,
  amount_cents,
  magnitude_cents
FROM v_resolved_expense_category_monthly;

CREATE VIEW v_report_category_totals AS
WITH totals AS (
  SELECT
    category_id,
    category_name,
    category_kind,
    brand_owner,
    color,
    SUM(amount_cents) AS total_cents,
    SUM(magnitude_cents) AS magnitude_cents
  FROM v_report_category_monthly
  GROUP BY category_id, category_kind
), grand AS (
  SELECT COALESCE(SUM(magnitude_cents), 0) AS total_magnitude_cents
  FROM totals
)
SELECT
  totals.*,
  CASE
    WHEN grand.total_magnitude_cents = 0 THEN 0
    ELSE ROUND(
      100.0 * totals.magnitude_cents / grand.total_magnitude_cents,
      1
    )
  END AS pct_of_total
FROM totals, grand;

CREATE VIEW v_report_category_monthly_trend AS
SELECT
  current.month,
  current.category_id,
  current.category_name,
  current.category_kind,
  current.brand_owner,
  current.color,
  current.amount_cents,
  current.magnitude_cents,
  COALESCE(previous.amount_cents, 0) AS prev_amount_cents,
  current.amount_cents - COALESCE(previous.amount_cents, 0)
    AS amount_delta_cents,
  COALESCE(previous.magnitude_cents, 0) AS prev_magnitude_cents,
  current.magnitude_cents - COALESCE(previous.magnitude_cents, 0)
    AS magnitude_delta_cents,
  CASE
    WHEN previous.magnitude_cents IS NULL
      OR previous.magnitude_cents = 0
      THEN NULL
    ELSE ROUND(
      100.0
      * (current.magnitude_cents - previous.magnitude_cents)
      / previous.magnitude_cents,
      1
    )
  END AS magnitude_pct_change
FROM v_report_category_monthly current
LEFT JOIN v_report_category_monthly previous
  ON previous.category_id=current.category_id
 AND previous.category_kind=current.category_kind
 AND previous.month=strftime(
   '%Y-%m',
   date(current.month || '-01', '-1 month')
 )
ORDER BY current.month, current.category_id, current.category_kind;
