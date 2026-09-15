-- FN-144: explicit positive-flow review, auditable decisions, and semantic
-- statement coverage.
--
-- A positive amount is direction, not meaning.  Decision events are immutable
-- operator evidence; report truth remains derived from transaction.flow_kind
-- plus an active typed relationship.

CREATE TABLE positive_flow_decision_events (
  id INTEGER PRIMARY KEY,
  operation_key TEXT NOT NULL UNIQUE
    CHECK (length(trim(operation_key)) > 0),
  subject_transaction_id INTEGER NOT NULL
    REFERENCES transactions(id) ON DELETE RESTRICT,
  statement_line_id INTEGER NOT NULL
    REFERENCES statement_lines(id) ON DELETE RESTRICT,
  statement_line_revision INTEGER NOT NULL
    CHECK (statement_line_revision > 0),
  proposal_key TEXT NOT NULL CHECK (length(trim(proposal_key)) > 0),
  evidence_fingerprint TEXT NOT NULL
    CHECK (length(evidence_fingerprint) = 64),
  action_kind TEXT NOT NULL
    CHECK (action_kind IN (
      'recover_positive_line',
      'reject_proposal',
      'restore_proposal',
      'accept_classification',
      'accept_pair',
      'undo_acceptance'
    )),
  selected_statement_month TEXT NOT NULL
    CHECK (
      selected_statement_month GLOB
        '[0-9][0-9][0-9][0-9]-[0-9][0-9]'
    ),
  request_fingerprint TEXT NOT NULL
    CHECK (length(request_fingerprint) = 64),
  event_kind TEXT NOT NULL
    CHECK (event_kind IN ('intake', 'reject', 'restore', 'accept', 'undo')),
  proposed_flow_kind TEXT NOT NULL DEFAULT ''
    CHECK (proposed_flow_kind IN (
      '', 'unknown', 'income', 'interest', 'refund', 'reimbursement',
      'internal_transfer', 'card_payment', 'reversal'
    )),
  relationship_kind TEXT NOT NULL DEFAULT ''
    CHECK (relationship_kind IN (
      '', 'transfer_pair', 'refund_of', 'reimbursement_for', 'reversal_of'
    )),
  candidate_transaction_id INTEGER
    REFERENCES transactions(id) ON DELETE RESTRICT,
  proposed_candidate_flow_kind TEXT NOT NULL DEFAULT ''
    CHECK (proposed_candidate_flow_kind IN (
      '', 'purchase', 'fee', 'internal_transfer', 'card_payment'
    )),
  accepted_relationship_id INTEGER
    REFERENCES transaction_relationships(id) ON DELETE RESTRICT,
  prior_subject_flow_kind TEXT NOT NULL DEFAULT ''
    CHECK (prior_subject_flow_kind IN (
      '', 'unknown', 'purchase', 'income', 'refund', 'reimbursement',
      'internal_transfer', 'card_payment', 'fee', 'interest', 'reversal',
      'adjustment', 'opening'
    )),
  prior_candidate_flow_kind TEXT NOT NULL DEFAULT ''
    CHECK (prior_candidate_flow_kind IN (
      '', 'unknown', 'purchase', 'income', 'refund', 'reimbursement',
      'internal_transfer', 'card_payment', 'fee', 'interest', 'reversal',
      'adjustment', 'opening'
    )),
  selected_category_id INTEGER
    REFERENCES categories(id) ON DELETE RESTRICT,
  allocation_explicit INTEGER NOT NULL DEFAULT 0
    CHECK (allocation_explicit IN (0, 1)),
  prior_statement_match_status TEXT NOT NULL DEFAULT ''
    CHECK (
      prior_statement_match_status IN (
        '', 'ignored', 'unmatched', 'needs_review'
      )
    ),
  prior_subject_splits_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
      json_valid(prior_subject_splits_json)
      AND json_type(prior_subject_splits_json) = 'array'
    ),
  accepted_subject_splits_json TEXT NOT NULL DEFAULT '[]'
    CHECK (
      json_valid(accepted_subject_splits_json)
      AND json_type(accepted_subject_splits_json) = 'array'
    ),
  evidence_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(evidence_json) AND json_type(evidence_json) = 'object'),
  actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  reverts_event_id INTEGER
    REFERENCES positive_flow_decision_events(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (
    (
      event_kind = 'intake'
      AND action_kind = 'recover_positive_line'
      AND proposed_flow_kind IN (
        'unknown', 'income', 'interest', 'refund', 'reimbursement',
        'internal_transfer', 'card_payment', 'reversal'
      )
      AND relationship_kind = ''
      AND candidate_transaction_id IS NULL
      AND proposed_candidate_flow_kind = ''
      AND accepted_relationship_id IS NULL
      AND prior_subject_flow_kind = ''
      AND prior_candidate_flow_kind = ''
      AND selected_category_id IS NULL
      AND allocation_explicit = 0
      AND prior_statement_match_status <> ''
      AND reverts_event_id IS NULL
      AND json_array_length(prior_subject_splits_json) = 0
      AND json_array_length(accepted_subject_splits_json) > 0
    )
    OR (
      event_kind IN ('reject', 'restore')
      AND (
        (event_kind = 'reject' AND action_kind = 'reject_proposal')
        OR (
          event_kind = 'restore'
          AND action_kind = 'restore_proposal'
        )
      )
      AND accepted_relationship_id IS NULL
      AND reverts_event_id IS NULL
      AND prior_subject_flow_kind = ''
      AND prior_candidate_flow_kind = ''
      AND selected_category_id IS NULL
      AND allocation_explicit = 0
      AND prior_statement_match_status = ''
      AND json_array_length(prior_subject_splits_json) = 0
      AND json_array_length(accepted_subject_splits_json) = 0
      AND (
        (
          proposed_flow_kind IN ('income', 'interest')
          AND relationship_kind = ''
          AND candidate_transaction_id IS NULL
          AND proposed_candidate_flow_kind = ''
        )
        OR (
          proposed_flow_kind IN (
            'refund', 'reimbursement', 'internal_transfer',
            'card_payment', 'reversal'
          )
          AND relationship_kind <> ''
          AND candidate_transaction_id IS NOT NULL
          AND proposed_candidate_flow_kind <> ''
        )
      )
    )
    OR (
      event_kind = 'accept'
      AND action_kind IN ('accept_classification', 'accept_pair')
      AND reverts_event_id IS NULL
      AND prior_subject_flow_kind <> ''
      AND prior_statement_match_status = ''
      AND json_array_length(prior_subject_splits_json) > 0
      AND json_array_length(accepted_subject_splits_json) > 0
      AND (
        (
          action_kind = 'accept_classification'
          AND proposed_flow_kind IN ('income', 'interest')
          AND relationship_kind = ''
          AND candidate_transaction_id IS NULL
          AND proposed_candidate_flow_kind = ''
          AND accepted_relationship_id IS NULL
          AND prior_candidate_flow_kind = ''
          AND selected_category_id IS NULL
          AND allocation_explicit = 0
        )
        OR (
          action_kind = 'accept_pair'
          AND proposed_flow_kind IN (
            'refund', 'reimbursement', 'internal_transfer',
            'card_payment', 'reversal'
          )
          AND relationship_kind <> ''
          AND candidate_transaction_id IS NOT NULL
          AND proposed_candidate_flow_kind <> ''
          AND accepted_relationship_id IS NOT NULL
          AND prior_candidate_flow_kind <> ''
          AND (
            (
              proposed_flow_kind IN (
                'refund', 'reimbursement', 'reversal'
              )
              AND selected_category_id IS NOT NULL
            )
            OR (
              proposed_flow_kind IN (
                'internal_transfer', 'card_payment'
              )
              AND selected_category_id IS NULL
              AND allocation_explicit = 0
            )
          )
        )
      )
    )
    OR (
      event_kind = 'undo'
      AND action_kind = 'undo_acceptance'
      AND reverts_event_id IS NOT NULL
      AND prior_subject_flow_kind <> ''
      AND prior_statement_match_status = ''
      AND json_array_length(prior_subject_splits_json) > 0
      AND json_array_length(accepted_subject_splits_json) > 0
      AND (
        (
          proposed_flow_kind IN ('income', 'interest')
          AND selected_category_id IS NULL
          AND allocation_explicit = 0
        )
        OR (
          proposed_flow_kind IN (
            'refund', 'reimbursement', 'reversal'
          )
          AND selected_category_id IS NOT NULL
        )
        OR (
          proposed_flow_kind IN (
            'internal_transfer', 'card_payment'
          )
          AND selected_category_id IS NULL
          AND allocation_explicit = 0
        )
      )
    )
  ),
  CHECK (
    candidate_transaction_id IS NULL
    OR candidate_transaction_id <> subject_transaction_id
  ),
  CHECK (
    (
      proposed_flow_kind IN ('unknown', 'income', 'interest')
      AND relationship_kind = ''
    )
    OR (proposed_flow_kind = 'refund' AND relationship_kind = 'refund_of')
    OR (
      proposed_flow_kind = 'reimbursement'
      AND relationship_kind = 'reimbursement_for'
    )
    OR (proposed_flow_kind = 'reversal' AND relationship_kind = 'reversal_of')
    OR (
      proposed_flow_kind IN ('internal_transfer', 'card_payment')
      AND relationship_kind = 'transfer_pair'
      AND proposed_candidate_flow_kind = proposed_flow_kind
    )
  ),
  CHECK (
    relationship_kind NOT IN (
      'refund_of', 'reimbursement_for', 'reversal_of'
    )
    OR proposed_candidate_flow_kind IN ('purchase', 'fee')
  )
);

CREATE INDEX idx_positive_flow_events_subject
  ON positive_flow_decision_events(subject_transaction_id, id);
CREATE INDEX idx_positive_flow_events_proposal
  ON positive_flow_decision_events(
    subject_transaction_id, proposal_key, id
  );
CREATE UNIQUE INDEX uq_positive_flow_event_undo
  ON positive_flow_decision_events(reverts_event_id)
  WHERE event_kind = 'undo';
CREATE UNIQUE INDEX uq_positive_flow_event_relationship
  ON positive_flow_decision_events(accepted_relationship_id)
  WHERE event_kind = 'accept' AND accepted_relationship_id IS NOT NULL;

-- Explicit transitions make stale or repeated actions fail closed:
-- available -> reject -> suppressed -> restore -> available
-- available -> accept -> resolved -> undo -> available
CREATE TRIGGER positive_flow_event_transition_insert
BEFORE INSERT ON positive_flow_decision_events
BEGIN
  SELECT CASE
    WHEN NEW.event_kind = 'reject'
      AND COALESCE((
        SELECT prior.event_kind
        FROM positive_flow_decision_events prior
        WHERE prior.subject_transaction_id = NEW.subject_transaction_id
          AND prior.proposal_key = NEW.proposal_key
        ORDER BY prior.id DESC
        LIMIT 1
      ), '') NOT IN ('', 'restore', 'undo')
    THEN RAISE(ABORT, 'positive-flow proposal is not available to reject')
  END;

  SELECT CASE
    WHEN NEW.event_kind = 'restore'
      AND COALESCE((
        SELECT prior.event_kind
        FROM positive_flow_decision_events prior
        WHERE prior.subject_transaction_id = NEW.subject_transaction_id
          AND prior.proposal_key = NEW.proposal_key
        ORDER BY prior.id DESC
        LIMIT 1
      ), '') <> 'reject'
    THEN RAISE(ABORT, 'only a rejected positive-flow proposal can be restored')
  END;

  SELECT CASE
    WHEN NEW.event_kind = 'accept'
      AND (
        COALESCE((
          SELECT prior.event_kind
          FROM positive_flow_decision_events prior
          WHERE prior.subject_transaction_id = NEW.subject_transaction_id
            AND prior.proposal_key = NEW.proposal_key
          ORDER BY prior.id DESC
          LIMIT 1
        ), '') = 'reject'
        OR EXISTS (
          SELECT 1
          FROM positive_flow_decision_events accepted
          WHERE accepted.subject_transaction_id = NEW.subject_transaction_id
            AND accepted.event_kind = 'accept'
            AND NOT EXISTS (
              SELECT 1
              FROM positive_flow_decision_events undone
              WHERE undone.event_kind = 'undo'
                AND undone.reverts_event_id = accepted.id
            )
        )
      )
    THEN RAISE(ABORT, 'positive-flow proposal is not available to accept')
  END;

  SELECT CASE
    WHEN NEW.event_kind = 'undo'
      AND NOT EXISTS (
        SELECT 1
        FROM positive_flow_decision_events accepted
        WHERE accepted.id = NEW.reverts_event_id
          AND accepted.event_kind = 'accept'
          AND accepted.subject_transaction_id = NEW.subject_transaction_id
          AND accepted.statement_line_id = NEW.statement_line_id
          AND accepted.statement_line_revision
                = NEW.statement_line_revision
          AND accepted.selected_statement_month
                = NEW.selected_statement_month
          AND accepted.proposal_key = NEW.proposal_key
          AND accepted.proposed_flow_kind = NEW.proposed_flow_kind
          AND accepted.relationship_kind = NEW.relationship_kind
          AND accepted.candidate_transaction_id
                IS NEW.candidate_transaction_id
          AND accepted.accepted_relationship_id
                IS NEW.accepted_relationship_id
          AND accepted.prior_subject_flow_kind
                = NEW.prior_subject_flow_kind
          AND accepted.prior_candidate_flow_kind
                = NEW.prior_candidate_flow_kind
          AND accepted.selected_category_id
                IS NEW.selected_category_id
          AND accepted.allocation_explicit
                = NEW.allocation_explicit
          AND accepted.prior_subject_splits_json
                = NEW.prior_subject_splits_json
          AND accepted.accepted_subject_splits_json
                = NEW.accepted_subject_splits_json
      )
    THEN RAISE(ABORT, 'undo must reference its exact positive-flow acceptance')
  END;
END;

-- A decision proposal is always anchored to one current, active positive
-- statement row.  Undo deliberately relies on the immutable acceptance anchor:
-- unreconciliation may have detached the line before an operator undoes it.
CREATE TRIGGER positive_flow_event_statement_scope_insert
BEFORE INSERT ON positive_flow_decision_events
WHEN NEW.event_kind <> 'undo'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM statement_lines line
    JOIN transactions subject
      ON subject.id = NEW.subject_transaction_id
    JOIN accounts account ON account.id = line.account_id
    LEFT JOIN statement_reviews review
      ON review.source_document_id = line.source_document_id
    LEFT JOIN statement_expectation_documents expectation_link
      ON expectation_link.source_document_id = line.source_document_id
     AND expectation_link.status = 'active'
    LEFT JOIN account_statement_expectations expectation
      ON expectation.id = expectation_link.expectation_id
    WHERE line.id = NEW.statement_line_id
      AND line.review_disposition = 'active'
      AND line.review_revision = NEW.statement_line_revision
      AND line.match_status IN ('matched', 'promoted')
      AND line.matched_transaction_id = subject.id
      AND line.amount_cents = subject.amount_cents
      AND line.account_id IS subject.account_id
      AND UPPER(TRIM(line.currency)) = UPPER(TRIM(account.currency))
      AND subject.amount_cents > 0
      AND (
        NEW.event_kind <> 'intake'
        OR (
          line.match_status = 'promoted'
          AND line.is_pending = 0
          AND subject.source = 'statement'
          AND subject.source_document_id = line.source_document_id
          AND subject.external_id = line.row_hash
          AND subject.posted_on = line.posted_on
        )
      )
      AND COALESCE(
        expectation.period_month,
        review.period_month,
        line.statement_period,
        substr(line.posted_on, 1, 7)
      ) = NEW.selected_statement_month
  ) THEN RAISE(ABORT, 'positive-flow statement evidence scope mismatch') END;
END;

CREATE TRIGGER positive_flow_event_intake_state_insert
BEFORE INSERT ON positive_flow_decision_events
WHEN NEW.event_kind = 'intake'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM transactions subject
    WHERE subject.id = NEW.subject_transaction_id
      AND subject.flow_kind = NEW.proposed_flow_kind
      AND subject.recon_status = 'cleared'
  ) THEN RAISE(ABORT, 'positive-flow intake subject state mismatch') END;

  SELECT CASE
    WHEN NEW.proposed_flow_kind <> 'unknown'
      AND NOT EXISTS (
        SELECT 1
        FROM positive_flow_decision_events accepted
        WHERE accepted.subject_transaction_id = NEW.subject_transaction_id
          AND accepted.statement_line_id = NEW.statement_line_id
          AND accepted.event_kind = 'accept'
          AND accepted.proposed_flow_kind = NEW.proposed_flow_kind
          AND accepted.selected_statement_month
                = NEW.selected_statement_month
          AND NOT EXISTS (
            SELECT 1
            FROM positive_flow_decision_events undone
            WHERE undone.event_kind = 'undo'
              AND undone.reverts_event_id = accepted.id
          )
      )
    THEN RAISE(
      ABORT,
      'positive-flow decided intake must preserve its live acceptance'
    )
  END;

  SELECT CASE WHEN
    EXISTS (
      SELECT 1
      FROM json_each(NEW.accepted_subject_splits_json) snapshot
      LEFT JOIN transaction_splits split
        ON split.id = json_extract(snapshot.value, '$.id')
       AND split.transaction_id = NEW.subject_transaction_id
      WHERE split.id IS NULL
         OR split.category_id
              <> json_extract(snapshot.value, '$.category_id')
         OR split.amount_cents
              <> json_extract(snapshot.value, '$.amount_cents')
         OR split.memo <> json_extract(snapshot.value, '$.memo')
    )
    OR (
      SELECT COUNT(*)
      FROM transaction_splits
      WHERE transaction_id = NEW.subject_transaction_id
    ) <> json_array_length(NEW.accepted_subject_splits_json)
  THEN RAISE(ABORT, 'positive-flow intake split snapshot mismatch') END;
END;

-- An acceptance event cannot claim accounting state that does not exist.
CREATE TRIGGER positive_flow_event_accounting_state_insert
BEFORE INSERT ON positive_flow_decision_events
WHEN NEW.event_kind = 'accept'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM transactions subject
    WHERE subject.id = NEW.subject_transaction_id
      AND subject.amount_cents > 0
      AND subject.flow_kind = NEW.proposed_flow_kind
  ) THEN RAISE(ABORT, 'positive-flow acceptance subject state mismatch') END;

  SELECT CASE
    WHEN NEW.proposed_flow_kind IN ('income', 'interest')
      AND EXISTS (
        SELECT 1
        FROM transaction_splits split
        JOIN categories category ON category.id = split.category_id
        WHERE split.transaction_id = NEW.subject_transaction_id
          AND category.kind <> 'income'
      )
    THEN RAISE(ABORT, 'positive-flow income acceptance category mismatch')
  END;

  SELECT CASE
    WHEN NEW.proposed_flow_kind IN (
      'refund', 'reimbursement', 'reversal'
    )
      AND EXISTS (
        SELECT 1
        FROM transaction_splits split
        JOIN categories category ON category.id = split.category_id
        WHERE split.transaction_id = NEW.subject_transaction_id
          AND category.kind <> 'expense'
      )
    THEN RAISE(ABORT, 'positive-flow offset acceptance category mismatch')
  END;

  SELECT CASE
    WHEN NEW.proposed_flow_kind IN (
      'refund', 'reimbursement', 'reversal'
    )
      AND NOT EXISTS (
        SELECT 1
        FROM categories category
        JOIN transaction_splits candidate_split
          ON candidate_split.category_id = category.id
        WHERE category.id = NEW.selected_category_id
          AND category.kind = 'expense'
          AND candidate_split.transaction_id
                = NEW.candidate_transaction_id
      )
    THEN RAISE(
      ABORT,
      'positive-flow selected offset category is not a candidate split'
    )
  END;

  SELECT CASE
    WHEN NEW.proposed_flow_kind IN (
      'refund', 'reimbursement', 'reversal'
    )
      AND EXISTS (
        SELECT 1
        FROM transaction_splits split
        WHERE split.transaction_id = NEW.subject_transaction_id
          AND split.category_id <> NEW.selected_category_id
      )
    THEN RAISE(
      ABORT,
      'positive-flow accepted offset split allocation mismatch'
    )
  END;

  SELECT CASE
    WHEN NEW.proposed_flow_kind IN (
      'internal_transfer', 'card_payment'
    )
      AND EXISTS (
        SELECT 1
        FROM transaction_splits split
        JOIN categories category ON category.id = split.category_id
        WHERE split.transaction_id = NEW.subject_transaction_id
          AND category.kind <> 'transfer'
      )
    THEN RAISE(ABORT, 'positive-flow transfer acceptance category mismatch')
  END;

  SELECT CASE WHEN
    EXISTS (
      SELECT 1
      FROM json_each(NEW.accepted_subject_splits_json) snapshot
      LEFT JOIN transaction_splits split
        ON split.id = json_extract(snapshot.value, '$.id')
       AND split.transaction_id = NEW.subject_transaction_id
      WHERE split.id IS NULL
         OR split.category_id
              <> json_extract(snapshot.value, '$.category_id')
         OR split.amount_cents
              <> json_extract(snapshot.value, '$.amount_cents')
         OR split.memo <> json_extract(snapshot.value, '$.memo')
    )
    OR (
      SELECT COUNT(*)
      FROM transaction_splits
      WHERE transaction_id = NEW.subject_transaction_id
    ) <> json_array_length(NEW.accepted_subject_splits_json)
  THEN RAISE(ABORT, 'positive-flow accepted split snapshot mismatch') END;

  SELECT CASE WHEN NEW.relationship_kind <> '' AND NOT EXISTS (
    SELECT 1
    FROM transactions candidate
    JOIN transaction_relationships relationship
      ON relationship.id = NEW.accepted_relationship_id
    WHERE candidate.id = NEW.candidate_transaction_id
      AND candidate.flow_kind = NEW.proposed_candidate_flow_kind
      AND relationship.status = 'active'
      AND relationship.relationship_kind = NEW.relationship_kind
      AND (
        (
          NEW.relationship_kind = 'transfer_pair'
          AND relationship.source_transaction_id
                = NEW.candidate_transaction_id
          AND relationship.target_transaction_id
                = NEW.subject_transaction_id
        )
        OR (
          NEW.relationship_kind IN (
            'refund_of', 'reimbursement_for', 'reversal_of'
          )
          AND relationship.source_transaction_id
                = NEW.subject_transaction_id
          AND relationship.target_transaction_id
                = NEW.candidate_transaction_id
        )
      )
  ) THEN RAISE(ABORT, 'positive-flow acceptance relationship state mismatch') END;
END;

-- A live decision owns the statement row's evidence identity.  Unreconcile may
-- detach and later reattach the same transaction, but a row correction or a
-- rematch to a different ledger effect requires undoing the decision first.
CREATE TRIGGER positive_flow_live_decision_line_guard
BEFORE UPDATE OF
  account_id, posted_on, amount_cents, currency, row_hash,
  source_anchor_id, review_disposition
ON statement_lines
WHEN EXISTS (
  SELECT 1
  FROM positive_flow_decision_events accepted
  WHERE accepted.statement_line_id = OLD.id
    AND accepted.event_kind = 'accept'
    AND NOT EXISTS (
      SELECT 1
      FROM positive_flow_decision_events undone
      WHERE undone.event_kind = 'undo'
        AND undone.reverts_event_id = accepted.id
    )
)
BEGIN
  SELECT RAISE(
    ABORT,
    'undo the positive-flow decision before correcting its statement row'
  );
END;

CREATE TRIGGER positive_flow_live_decision_rematch_guard
BEFORE UPDATE OF matched_transaction_id ON statement_lines
WHEN NEW.matched_transaction_id IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM positive_flow_decision_events accepted
    WHERE accepted.statement_line_id = OLD.id
      AND accepted.event_kind = 'accept'
      AND accepted.subject_transaction_id <> NEW.matched_transaction_id
      AND NOT EXISTS (
        SELECT 1
        FROM positive_flow_decision_events undone
        WHERE undone.event_kind = 'undo'
          AND undone.reverts_event_id = accepted.id
      )
  )
BEGIN
  SELECT RAISE(
    ABORT,
    'positive-flow statement row must reattach to its decided transaction'
  );
END;

CREATE TRIGGER positive_flow_event_undo_state_insert
BEFORE INSERT ON positive_flow_decision_events
WHEN NEW.event_kind = 'undo'
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1
    FROM transactions subject
    WHERE subject.id = NEW.subject_transaction_id
      AND subject.flow_kind = NEW.prior_subject_flow_kind
  ) THEN RAISE(ABORT, 'positive-flow undo subject state mismatch') END;

  SELECT CASE
    WHEN NEW.candidate_transaction_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM transactions candidate
        WHERE candidate.id = NEW.candidate_transaction_id
          AND candidate.flow_kind = NEW.prior_candidate_flow_kind
      )
    THEN RAISE(ABORT, 'positive-flow undo candidate state mismatch')
  END;

  SELECT CASE
    WHEN NEW.accepted_relationship_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM transaction_relationships relationship
        WHERE relationship.id = NEW.accepted_relationship_id
          AND relationship.status = 'revoked'
      )
    THEN RAISE(ABORT, 'positive-flow undo relationship must be revoked first')
  END;

  SELECT CASE WHEN
    EXISTS (
      SELECT 1
      FROM json_each(NEW.prior_subject_splits_json) snapshot
      LEFT JOIN transaction_splits split
        ON split.id = json_extract(snapshot.value, '$.id')
       AND split.transaction_id = NEW.subject_transaction_id
      WHERE split.id IS NULL
         OR split.category_id
              <> json_extract(snapshot.value, '$.category_id')
         OR split.amount_cents
              <> json_extract(snapshot.value, '$.amount_cents')
         OR split.memo <> json_extract(snapshot.value, '$.memo')
    )
    OR (
      SELECT COUNT(*)
      FROM transaction_splits
      WHERE transaction_id = NEW.subject_transaction_id
    ) <> json_array_length(NEW.prior_subject_splits_json)
  THEN RAISE(ABORT, 'positive-flow undo split snapshot mismatch') END;
END;

CREATE TRIGGER positive_flow_event_no_update
BEFORE UPDATE ON positive_flow_decision_events
BEGIN
  SELECT RAISE(
    ABORT,
    'positive-flow decision events are append-only'
  );
END;

CREATE TRIGGER positive_flow_event_no_delete
BEFORE DELETE ON positive_flow_decision_events
BEGIN
  SELECT RAISE(
    ABORT,
    'positive-flow decision events are append-only'
  );
END;

-- Statement coverage must follow accepted accounting semantics, never amount
-- sign.  Positive rows stay in a dedicated review bucket until their matched
-- transaction is report-eligible; only income/interest contributes to income.
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
  CASE WHEN sl.amount_cents < 0 THEN ABS(sl.amount_cents) ELSE 0 END
    AS spend_cents,
  CASE
    WHEN sl.amount_cents > 0
      AND sl.match_status IN ('matched', 'promoted')
      AND flow_status.report_eligible = 1
      AND transaction_row.flow_kind IN ('income', 'interest')
    THEN sl.amount_cents
    ELSE 0
  END AS income_cents,
  CASE
    WHEN sl.amount_cents > 0
      AND NOT (
        sl.match_status IN ('matched', 'promoted')
        AND flow_status.report_eligible = 1
      )
    THEN sl.amount_cents
    ELSE 0
  END AS positive_review_cents,
  sl.match_status,
  sl.matched_transaction_id,
  sl.match_method,
  sl.match_score,
  sl.match_rationale,
  transaction_row.flow_kind AS transaction_flow_kind,
  COALESCE(flow_status.semantic_status, 'unknown') AS semantic_status,
  CASE
    WHEN sl.amount_cents > 0
      AND sl.match_status IN ('matched', 'promoted')
      AND flow_status.report_eligible = 1
      AND transaction_row.flow_kind IN ('income', 'interest')
    THEN 'income'
    WHEN sl.amount_cents > 0
      AND sl.match_status IN ('matched', 'promoted')
      AND flow_status.report_eligible = 1
    THEN 'resolved_positive'
    WHEN sl.amount_cents > 0 THEN 'flow_review'
    WHEN sl.match_status IN ('matched', 'promoted')
      AND flow_status.report_eligible = 0
    THEN 'unmatched'
    WHEN sl.match_status IN ('matched', 'promoted')
      AND transaction_row.flow_kind IN (
        'internal_transfer', 'card_payment'
      )
    THEN 'ignored'
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
    WHEN sl.amount_cents > 0
      AND NOT (
        sl.match_status IN ('matched', 'promoted')
        AND flow_status.report_eligible = 1
      )
    THEN 'positive flow review'
    WHEN sl.amount_cents >= 0 THEN ''
    WHEN sl.match_status IN ('matched', 'promoted')
      AND flow_status.report_eligible = 0
    THEN 'transaction flow review'
    WHEN sl.match_status IN ('matched', 'promoted')
      AND transaction_row.flow_kind IN (
        'internal_transfer', 'card_payment'
      )
    THEN 'ignored/internal transfer'
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
        FROM transactions candidate
        WHERE candidate.account_id IS sl.account_id
          AND candidate.amount_cents = sl.amount_cents
          AND candidate.recon_status = 'cleared'
          AND candidate.posted_on BETWEEN date(sl.posted_on, '-2 days')
                                      AND date(sl.posted_on, '+2 days')
      )
    THEN 'possible duplicate'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND EXISTS (
        SELECT 1
        FROM transactions candidate
        WHERE candidate.account_id IS sl.account_id
          AND candidate.amount_cents = sl.amount_cents
          AND candidate.recon_status = 'uncleared'
          AND candidate.posted_on BETWEEN date(sl.posted_on, '-7 days')
                                      AND date(sl.posted_on, '+1 days')
      )
    THEN 'ambiguous match'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
      AND NOT EXISTS (
        SELECT 1
        FROM transactions candidate
        WHERE lower(candidate.description)
                LIKE '%' || lower(sl.norm_merchant) || '%'
           OR lower(candidate.counterparty)
                LIKE '%' || lower(sl.norm_merchant) || '%'
      )
    THEN 'new merchant'
    WHEN sl.match_status IN ('unmatched', 'needs_review')
    THEN 'missing receipt'
    ELSE ''
  END AS attention_reason
FROM statement_lines sl
JOIN source_documents sd ON sd.id = sl.source_document_id
LEFT JOIN accounts a ON a.id = sl.account_id
LEFT JOIN transactions transaction_row
  ON transaction_row.id = sl.matched_transaction_id
LEFT JOIN v_transaction_flow_status flow_status
  ON flow_status.transaction_id = transaction_row.id
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
    SUM(positive_review_cents) AS positive_review_cents,
    SUM(
      CASE
        WHEN attention_reason <> ''
         AND attention_reason <> 'ignored/internal transfer'
        THEN spend_cents
        ELSE 0
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
