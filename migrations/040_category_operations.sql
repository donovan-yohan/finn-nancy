-- Stable machine operation receipts outlive proposal decisions and edits.
CREATE TABLE category_operations (
  operation_key TEXT PRIMARY KEY CHECK(length(operation_key) BETWEEN 1 AND 120),
  actor TEXT NOT NULL CHECK(actor='agent:namako'),
  request_json TEXT NOT NULL,
  proposed_action_id INTEGER NOT NULL UNIQUE REFERENCES proposed_actions(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER category_operations_no_update BEFORE UPDATE ON category_operations
BEGIN SELECT RAISE(ABORT, 'category operations are immutable'); END;
CREATE TRIGGER category_operations_no_delete BEFORE DELETE ON category_operations
BEGIN SELECT RAISE(ABORT, 'category operations are immutable'); END;
CREATE TRIGGER proposed_action_audit_no_update BEFORE UPDATE ON proposed_action_audit
BEGIN SELECT RAISE(ABORT, 'proposal audit is append-only'); END;
CREATE TRIGGER proposed_action_audit_no_delete BEFORE DELETE ON proposed_action_audit
BEGIN SELECT RAISE(ABORT, 'proposal audit is append-only'); END;
