CREATE TABLE IF NOT EXISTS closed_periods (
  id INTEGER PRIMARY KEY,
  month TEXT NOT NULL UNIQUE CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','reopened')),
  closed_at TEXT,
  coverage_pct REAL NOT NULL DEFAULT 0.0,
  uncategorized_count INTEGER NOT NULL DEFAULT 0,
  variance_ack INTEGER NOT NULL DEFAULT 0 CHECK (variance_ack IN (0,1)),
  net_delta_cents INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Append-only edit trail: closing a period and any post-close override write a row
-- here. The repo exposes insert + read by month only; there is no update/delete path.
CREATE TABLE IF NOT EXISTS close_audit (
  id INTEGER PRIMARY KEY,
  month TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  entity TEXT NOT NULL,
  entity_id INTEGER,
  field TEXT NOT NULL DEFAULT '',
  old_value TEXT,
  new_value TEXT,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_close_audit_month ON close_audit(month);
