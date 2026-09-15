CREATE TABLE budgets (
  id INTEGER PRIMARY KEY,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  period_month TEXT NOT NULL DEFAULT ''
     CHECK (period_month = '' OR period_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  rollover INTEGER NOT NULL DEFAULT 0 CHECK (rollover IN (0,1)),
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (category_id, period_month)
);

CREATE TABLE app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO app_settings(key, value)
VALUES ('big_ticket_threshold_cents', '30000');

ALTER TABLE categories ADD COLUMN is_leisure INTEGER NOT NULL DEFAULT 0 CHECK (is_leisure IN (0,1));
