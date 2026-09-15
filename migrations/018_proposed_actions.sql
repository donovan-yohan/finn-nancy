CREATE TABLE IF NOT EXISTS proposed_actions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  original_payload_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  confidence REAL NOT NULL DEFAULT 0.0,
  rationale TEXT NOT NULL DEFAULT '',
  agent_run_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed','approved','edited_approved','rejected','needs_evidence','snoozed','reverted')),
  feedback TEXT NOT NULL DEFAULT '',
  decided_by TEXT NOT NULL DEFAULT '',
  snoozed_until TEXT,
  revert_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  decided_at TEXT,
  applied_at TEXT
);

CREATE TABLE IF NOT EXISTS proposed_action_audit (
  id INTEGER PRIMARY KEY,
  proposed_action_id INTEGER NOT NULL REFERENCES proposed_actions(id) ON DELETE CASCADE,
  from_status TEXT,
  to_status TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  feedback TEXT NOT NULL DEFAULT '',
  payload_snapshot_json TEXT NOT NULL DEFAULT '{}',
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_proposed_actions_status
  ON proposed_actions(status);
CREATE INDEX IF NOT EXISTS idx_proposed_actions_agent_run
  ON proposed_actions(agent_run_id);
CREATE INDEX IF NOT EXISTS idx_proposed_action_audit_action
  ON proposed_action_audit(proposed_action_id);
