CREATE TABLE insight_prose (
  id INTEGER PRIMARY KEY,
  period_month TEXT NOT NULL,
  scope TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  model TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(period_month, scope, kind)
);
