-- Canonical bank feed: observations are evidence, never statement coverage.
CREATE TABLE bank_consents (
 id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES household_members(id),
 version TEXT NOT NULL, granted INTEGER NOT NULL CHECK(granted IN (0,1)),
 actor TEXT NOT NULL DEFAULT 'operator:household', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_connections (
 id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES household_members(id),
 item_id TEXT NOT NULL UNIQUE, token_ref TEXT NOT NULL, consent_version TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','needs_repair','remove_uncertain','removing','removed')),
 cursor TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_link_sessions (
 id TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, member_id INTEGER NOT NULL REFERENCES household_members(id),
 token_ref TEXT NOT NULL, consent_id INTEGER NOT NULL REFERENCES bank_consents(id),
 connection_id INTEGER REFERENCES bank_connections(id),
 state TEXT NOT NULL CHECK(state IN ('pending','exchanging','uncertain','complete','exited')),
 expires_at INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_accounts (
 id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES bank_connections(id),
 provider_account_id TEXT NOT NULL, name TEXT NOT NULL, currency TEXT NOT NULL, kind TEXT NOT NULL,
 UNIQUE(connection_id,provider_account_id)
);
CREATE TABLE bank_bindings (
 id INTEGER PRIMARY KEY, bank_account_id INTEGER NOT NULL REFERENCES bank_accounts(id),
 revision INTEGER NOT NULL, account_id INTEGER NOT NULL REFERENCES accounts(id),
 included INTEGER NOT NULL CHECK(included IN (0,1)), ownership TEXT NOT NULL CHECK(ownership IN ('shared','personal')),
 owner_member_id INTEGER REFERENCES household_members(id),
 role TEXT NOT NULL CHECK(role IN ('primary','secondary')), actor TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(bank_account_id,revision),
 CHECK((ownership='shared' AND owner_member_id IS NULL) OR (ownership='personal' AND owner_member_id IS NOT NULL))
);
CREATE VIEW v_bank_bindings AS SELECT binding.* FROM bank_bindings binding
 WHERE NOT EXISTS (SELECT 1 FROM bank_bindings newer WHERE newer.bank_account_id=binding.bank_account_id AND newer.revision>binding.revision);
CREATE TRIGGER bank_primary_unique BEFORE INSERT ON bank_bindings
 WHEN NEW.included=1 AND NEW.role='primary' AND EXISTS (
 SELECT 1 FROM v_bank_bindings WHERE bank_account_id<>NEW.bank_account_id AND account_id=NEW.account_id AND included=1 AND role='primary')
 BEGIN SELECT RAISE(ABORT,'canonical account already has a primary feed'); END;
CREATE TABLE bank_windows (
 id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES bank_connections(id),
 generation INTEGER NOT NULL, from_cursor TEXT NOT NULL, to_cursor TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(connection_id,generation)
);
CREATE TABLE bank_observations (
 id INTEGER PRIMARY KEY, window_id INTEGER NOT NULL REFERENCES bank_windows(id),
 connection_id INTEGER NOT NULL REFERENCES bank_connections(id), provider_transaction_id TEXT NOT NULL,
 provider_account_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('added','modified','removed')),
 amount_cents INTEGER, currency TEXT NOT NULL, posted_on TEXT NOT NULL, pending INTEGER NOT NULL CHECK(pending IN (0,1)),
 pending_predecessor TEXT, merchant TEXT NOT NULL, description TEXT NOT NULL, suggestion_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_subjects (
 id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES bank_connections(id),
 provider_transaction_id TEXT NOT NULL, observation_id INTEGER NOT NULL REFERENCES bank_observations(id),
 UNIQUE(connection_id,provider_transaction_id)
);
CREATE TABLE bank_operations (
 operation_key TEXT PRIMARY KEY, request_json TEXT NOT NULL, result_json TEXT NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_associations (
 id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL REFERENCES bank_subjects(id),
 observation_id INTEGER NOT NULL REFERENCES bank_observations(id), binding_id INTEGER NOT NULL REFERENCES bank_bindings(id),
 transaction_id INTEGER NOT NULL REFERENCES transactions(id), operation_key TEXT NOT NULL REFERENCES bank_operations(operation_key),
 actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_revocations (
 id INTEGER PRIMARY KEY, association_id INTEGER NOT NULL UNIQUE REFERENCES bank_associations(id),
 actor TEXT NOT NULL, reason TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE VIEW v_bank_associations AS SELECT a.* FROM bank_associations a WHERE NOT EXISTS
 (SELECT 1 FROM bank_revocations r WHERE r.association_id=a.id);
CREATE TABLE bank_exclusions (
 id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL REFERENCES bank_subjects(id), observation_id INTEGER NOT NULL REFERENCES bank_observations(id),
 actor TEXT NOT NULL, reason TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_receipt_evidence (
 id INTEGER PRIMARY KEY, extraction_id INTEGER NOT NULL UNIQUE REFERENCES ingest_extractions(id),
 source_document_id INTEGER NOT NULL REFERENCES source_documents(id), transaction_id INTEGER NOT NULL REFERENCES transactions(id),
 actor TEXT NOT NULL, reason TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE bank_receipt_decisions (
 id INTEGER PRIMARY KEY, extraction_id INTEGER NOT NULL UNIQUE REFERENCES ingest_extractions(id),
 source_document_id INTEGER NOT NULL REFERENCES source_documents(id),
 transaction_id INTEGER NOT NULL REFERENCES transactions(id),
 decision TEXT NOT NULL CHECK(decision='separate_purchase'),
 operation_key TEXT NOT NULL UNIQUE CHECK(length(trim(operation_key)) BETWEEN 1 AND 180),
 actor TEXT NOT NULL CHECK(length(trim(actor))>0),
 reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 500),
 original_json TEXT NOT NULL CHECK(json_valid(original_json)),
 reviewed_json TEXT NOT NULL CHECK(json_valid(reviewed_json)),
 candidate_transaction_ids_json TEXT NOT NULL CHECK(json_valid(candidate_transaction_ids_json)),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER bank_receipt_decision_validate BEFORE INSERT ON bank_receipt_decisions
 WHEN NOT EXISTS (
 SELECT 1 FROM ingest_extractions e JOIN source_documents d ON d.id=e.source_document_id
 JOIN transactions t ON t.id=NEW.transaction_id
 WHERE e.id=NEW.extraction_id AND e.source_document_id=NEW.source_document_id
 AND e.doc_kind='receipt' AND d.kind='receipt' AND e.review_status='pending' AND e.transaction_id IS NULL
 AND e.extracted_json=NEW.original_json AND t.source='receipt' AND t.source_document_id=d.id)
 BEGIN SELECT RAISE(ABORT,'invalid separate receipt decision'); END;
CREATE TRIGGER bank_receipt_decision_period BEFORE INSERT ON bank_receipt_decisions
 WHEN EXISTS(SELECT 1 FROM v_period_policy_locked_months m WHERE m.month IN (
 SELECT substr(posted_on,1,7) FROM transactions WHERE id=NEW.transaction_id UNION
 SELECT CASE WHEN json_valid(NEW.original_json) THEN substr(json_extract(NEW.original_json,'$.purchased_on'),1,7) END))
 BEGIN SELECT RAISE(ABORT,'closed period separate receipt decision'); END;
CREATE TRIGGER bank_receipt_decisions_no_update BEFORE UPDATE ON bank_receipt_decisions
 BEGIN SELECT RAISE(ABORT,'bank receipt decisions are immutable'); END;
CREATE TRIGGER bank_receipt_decisions_no_delete BEFORE DELETE ON bank_receipt_decisions
 BEGIN SELECT RAISE(ABORT,'bank receipt decisions are immutable'); END;
CREATE VIEW v_bank_feed_blockers AS
 SELECT DISTINCT s.id AS subject_id, substr(o.posted_on,1,7) AS month, 'unreviewed' AS kind
 FROM bank_subjects s JOIN bank_observations o ON o.id=s.observation_id
 JOIN bank_accounts ba ON ba.connection_id=s.connection_id AND ba.provider_account_id=o.provider_account_id
 JOIN v_bank_bindings b ON b.bank_account_id=ba.id AND b.included=1
 WHERE o.kind<>'removed' AND o.pending=0
 AND NOT EXISTS (SELECT 1 FROM v_bank_associations a WHERE a.subject_id=s.id)
 AND NOT EXISTS (SELECT 1 FROM bank_exclusions e WHERE e.subject_id=s.id AND e.observation_id=o.id)
 UNION
 SELECT a.subject_id, substr(o.posted_on,1,7), 'changed_evidence'
 FROM v_bank_associations a JOIN bank_subjects s ON s.id=a.subject_id
 JOIN bank_observations o ON o.id=a.observation_id
 WHERE a.observation_id<>s.observation_id
 UNION
 SELECT a.subject_id, substr(o.posted_on,1,7), 'changed_evidence'
 FROM v_bank_associations a JOIN bank_subjects s ON s.id=a.subject_id
 JOIN bank_observations o ON o.id=s.observation_id
 WHERE a.observation_id<>s.observation_id;
CREATE TRIGGER bank_close_guard BEFORE INSERT ON period_close_events
 WHEN NEW.event_kind IN ('clean_closed','closed_with_exceptions') AND EXISTS (
 SELECT 1 FROM period_close_cycles c JOIN v_bank_feed_blockers b ON b.month=c.month WHERE c.id=NEW.cycle_id)
 BEGIN SELECT RAISE(ABORT,'bank feed review required before close'); END;
CREATE TRIGGER bank_association_validate BEFORE INSERT ON bank_associations
 WHEN NOT EXISTS (
 SELECT 1 FROM bank_subjects s JOIN bank_observations o ON o.id=s.observation_id
 JOIN bank_accounts ba ON ba.connection_id=s.connection_id AND ba.provider_account_id=o.provider_account_id
 JOIN v_bank_bindings b ON b.bank_account_id=ba.id JOIN transactions t ON t.id=NEW.transaction_id
 WHERE s.id=NEW.subject_id AND o.id=NEW.observation_id AND b.id=NEW.binding_id AND b.included=1
 AND o.kind<>'removed' AND o.pending=0 AND b.account_id=t.account_id AND o.amount_cents=t.amount_cents
 AND o.currency=(SELECT currency FROM accounts WHERE id=t.account_id))
 OR EXISTS(SELECT 1 FROM v_bank_associations WHERE subject_id=NEW.subject_id)
 BEGIN SELECT RAISE(ABORT,'invalid bank association'); END;
CREATE TRIGGER bank_association_period BEFORE INSERT ON bank_associations
 WHEN EXISTS(SELECT 1 FROM v_period_policy_locked_months m WHERE m.month IN (
 SELECT substr(posted_on,1,7) FROM transactions WHERE id=NEW.transaction_id UNION
 SELECT substr(posted_on,1,7) FROM bank_observations WHERE id=NEW.observation_id))
 BEGIN SELECT RAISE(ABORT,'closed period bank evidence'); END;
CREATE TRIGGER bank_revocation_period BEFORE INSERT ON bank_revocations
 WHEN EXISTS(SELECT 1 FROM bank_associations a JOIN transactions t ON t.id=a.transaction_id
 JOIN bank_observations o ON o.id=a.observation_id JOIN v_period_policy_locked_months m
 ON m.month=substr(t.posted_on,1,7) OR m.month=substr(o.posted_on,1,7) WHERE a.id=NEW.association_id)
 BEGIN SELECT RAISE(ABORT,'closed period bank evidence'); END;
CREATE TRIGGER bank_transaction_evidence_guard BEFORE UPDATE OF account_id,amount_cents,posted_on ON transactions
 WHEN (NEW.account_id<>OLD.account_id OR NEW.amount_cents<>OLD.amount_cents OR NEW.posted_on<>OLD.posted_on)
 AND EXISTS(SELECT 1 FROM v_bank_associations WHERE transaction_id=OLD.id)
 BEGIN SELECT RAISE(ABORT,'revoke bank association before transaction edit'); END;
-- Retaining raw closed-month observations is allowed. Activating an excluded
-- or previously unmapped account is an accounting change and requires reopen.
CREATE TRIGGER bank_binding_activation_period BEFORE INSERT ON bank_bindings
 WHEN NEW.included=1
 AND NOT EXISTS(SELECT 1 FROM v_bank_bindings b
   WHERE b.bank_account_id=NEW.bank_account_id AND b.included=1)
 AND EXISTS(SELECT 1 FROM bank_accounts ba
   JOIN bank_observations o ON o.connection_id=ba.connection_id AND o.provider_account_id=ba.provider_account_id
   JOIN v_period_policy_locked_months m ON m.month=substr(o.posted_on,1,7)
   WHERE ba.id=NEW.bank_account_id)
 BEGIN SELECT RAISE(ABORT,'closed period bank binding activation'); END;
CREATE TRIGGER bank_binding_evidence_guard BEFORE INSERT ON bank_bindings
 WHEN EXISTS(SELECT 1 FROM v_bank_associations a JOIN bank_bindings b ON b.id=a.binding_id WHERE b.bank_account_id=NEW.bank_account_id)
 BEGIN SELECT RAISE(ABORT,'revoke bank associations before binding edit'); END;
CREATE TRIGGER bank_account_currency_guard BEFORE UPDATE OF currency ON accounts
 WHEN NEW.currency<>OLD.currency AND EXISTS(SELECT 1 FROM v_bank_associations a JOIN transactions t ON t.id=a.transaction_id WHERE t.account_id=OLD.id)
 BEGIN SELECT RAISE(ABORT,'revoke bank associations before account edit'); END;
CREATE TRIGGER bank_exclusion_period BEFORE INSERT ON bank_exclusions
 WHEN EXISTS(SELECT 1 FROM bank_observations o JOIN v_period_policy_locked_months m
 ON m.month=substr(o.posted_on,1,7) WHERE o.id=NEW.observation_id)
 BEGIN SELECT RAISE(ABORT,'closed period bank exclusion evidence'); END;
CREATE TRIGGER bank_exclusion_validate BEFORE INSERT ON bank_exclusions
 WHEN NOT EXISTS(SELECT 1 FROM bank_subjects WHERE id=NEW.subject_id AND observation_id=NEW.observation_id)
 OR EXISTS(SELECT 1 FROM v_bank_associations WHERE subject_id=NEW.subject_id)
 BEGIN SELECT RAISE(ABORT,'invalid bank exclusion evidence'); END;
-- CASE is intentional: SQLite may reorder AND predicates, so malformed JSON
-- must never reach json_extract/json_type even if another guard rejects it.
CREATE TRIGGER bank_receipt_validate BEFORE INSERT ON bank_receipt_evidence
 WHEN NOT EXISTS (
 SELECT 1 FROM ingest_extractions e JOIN source_documents d ON d.id=e.source_document_id
 JOIN transactions t ON t.id=NEW.transaction_id JOIN accounts a ON a.id=t.account_id
 WHERE e.id=NEW.extraction_id AND e.source_document_id=NEW.source_document_id
 AND d.kind='receipt' AND e.doc_kind='receipt' AND e.review_status='pending' AND e.transaction_id IS NULL
 AND (t.source IN ('bank_feed','statement') OR EXISTS (
   SELECT 1 FROM bank_associations ba WHERE ba.transaction_id=t.id))
 AND CASE WHEN json_valid(e.extracted_json) THEN
   json_type(e.extracted_json)='object'
   AND json_type(e.extracted_json,'$.purchased_on')='text'
   AND json_extract(e.extracted_json,'$.purchased_on') GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
   AND date(json_extract(e.extracted_json,'$.purchased_on'),'+0 days')=json_extract(e.extracted_json,'$.purchased_on')
   AND json_type(e.extracted_json,'$.total_cents')='integer'
   AND json_extract(e.extracted_json,'$.total_cents')>0
   AND t.amount_cents=-json_extract(e.extracted_json,'$.total_cents')
   AND json_type(e.extracted_json,'$.currency')='text'
   AND json_extract(e.extracted_json,'$.currency')=a.currency
   AND a.currency GLOB '[A-Z][A-Z][A-Z]'
   AND t.posted_on BETWEEN date(json_extract(e.extracted_json,'$.purchased_on'),'-7 days')
                      AND date(json_extract(e.extracted_json,'$.purchased_on'),'+7 days')
 ELSE 0 END
 ) OR length(trim(NEW.actor))=0 OR length(trim(NEW.reason))=0 OR length(trim(NEW.operation_key))=0
 BEGIN SELECT RAISE(ABORT,'invalid bank receipt evidence'); END;
CREATE TRIGGER bank_receipt_period BEFORE INSERT ON bank_receipt_evidence
 WHEN EXISTS(SELECT 1 FROM v_period_policy_locked_months m WHERE m.month IN (
 SELECT substr(posted_on,1,7) FROM transactions WHERE id=NEW.transaction_id UNION
 SELECT CASE WHEN json_valid(extracted_json) THEN substr(json_extract(extracted_json,'$.purchased_on'),1,7) END
 FROM ingest_extractions WHERE id=NEW.extraction_id))
 BEGIN SELECT RAISE(ABORT,'closed period bank receipt evidence'); END;
CREATE TRIGGER bank_observations_no_update BEFORE UPDATE ON bank_observations BEGIN SELECT RAISE(ABORT,'bank observations are immutable'); END;
CREATE TRIGGER bank_observations_no_delete BEFORE DELETE ON bank_observations BEGIN SELECT RAISE(ABORT,'bank observations are immutable'); END;
CREATE TRIGGER bank_windows_no_update BEFORE UPDATE ON bank_windows BEGIN SELECT RAISE(ABORT,'bank windows are immutable'); END;
CREATE TRIGGER bank_windows_no_delete BEFORE DELETE ON bank_windows BEGIN SELECT RAISE(ABORT,'bank windows are immutable'); END;
CREATE TRIGGER bank_bindings_no_update BEFORE UPDATE ON bank_bindings BEGIN SELECT RAISE(ABORT,'bank bindings are immutable'); END;
CREATE TRIGGER bank_bindings_no_delete BEFORE DELETE ON bank_bindings BEGIN SELECT RAISE(ABORT,'bank bindings are immutable'); END;
CREATE TRIGGER bank_associations_no_update BEFORE UPDATE ON bank_associations BEGIN SELECT RAISE(ABORT,'bank associations are immutable'); END;
CREATE TRIGGER bank_associations_no_delete BEFORE DELETE ON bank_associations BEGIN SELECT RAISE(ABORT,'bank associations are immutable'); END;
CREATE TRIGGER bank_revocations_no_update BEFORE UPDATE ON bank_revocations BEGIN SELECT RAISE(ABORT,'bank revocations are immutable'); END;
CREATE TRIGGER bank_revocations_no_delete BEFORE DELETE ON bank_revocations BEGIN SELECT RAISE(ABORT,'bank revocations are immutable'); END;
CREATE TRIGGER bank_exclusions_no_update BEFORE UPDATE ON bank_exclusions BEGIN SELECT RAISE(ABORT,'bank exclusions are immutable'); END;
CREATE TRIGGER bank_exclusions_no_delete BEFORE DELETE ON bank_exclusions BEGIN SELECT RAISE(ABORT,'bank exclusions are immutable'); END;
CREATE TRIGGER bank_operations_no_update BEFORE UPDATE ON bank_operations BEGIN SELECT RAISE(ABORT,'bank operations are immutable'); END;
CREATE TRIGGER bank_operations_no_delete BEFORE DELETE ON bank_operations BEGIN SELECT RAISE(ABORT,'bank operations are immutable'); END;
CREATE TRIGGER bank_consents_no_update BEFORE UPDATE ON bank_consents BEGIN SELECT RAISE(ABORT,'bank consents are immutable'); END;
CREATE TRIGGER bank_consents_no_delete BEFORE DELETE ON bank_consents BEGIN SELECT RAISE(ABORT,'bank consents are immutable'); END;
CREATE TRIGGER bank_receipts_no_update BEFORE UPDATE ON bank_receipt_evidence BEGIN SELECT RAISE(ABORT,'bank receipts are immutable'); END;
CREATE TRIGGER bank_receipts_no_delete BEFORE DELETE ON bank_receipt_evidence BEGIN SELECT RAISE(ABORT,'bank receipts are immutable'); END;
