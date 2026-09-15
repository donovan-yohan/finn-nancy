CREATE TABLE goals (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('savings_target','payoff','sinking_fund')),
  target_cents INTEGER NOT NULL CHECK (target_cents > 0),
  start_month  TEXT NOT NULL CHECK (start_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  target_month TEXT CHECK (target_month IS NULL OR target_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  monthly_contribution_cents INTEGER NOT NULL DEFAULT 0 CHECK (monthly_contribution_cents >= 0),
  linked_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
  linked_category_id    INTEGER REFERENCES categories(id)   ON DELETE SET NULL,
  auto_fund INTEGER NOT NULL DEFAULT 0 CHECK (auto_fund IN (0,1)),
  priority  INTEGER NOT NULL DEFAULT 100,
  status    TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','completed','cancelled')),
  brand_owner TEXT NOT NULL DEFAULT 'nancy' CHECK (brand_owner IN ('finn','nancy','shared')),
  color TEXT NOT NULL DEFAULT '#FF9F43',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_goals_status ON goals(status);

CREATE TABLE goal_ledger (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  month TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
  planned_cents INTEGER NOT NULL DEFAULT 0,
  actual_cents  INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'underspend'
         CHECK (source IN ('underspend','manual','income','transfer','adjustment')),
  transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('planned','applied','skipped')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (goal_id, month)
);
CREATE INDEX idx_goal_ledger_goal ON goal_ledger(goal_id);

DROP VIEW IF EXISTS v_goal_progress;
CREATE VIEW v_goal_progress AS
WITH funded AS (
  SELECT goal_id, SUM(actual_cents) AS contributed_cents, COUNT(*) AS months_funded,
         MAX(month) AS last_funded_month
  FROM goal_ledger WHERE status='applied' GROUP BY goal_id)
SELECT g.id AS goal_id, g.name, g.kind, g.status, g.color, g.brand_owner,
       g.target_cents, g.start_month, g.target_month, g.monthly_contribution_cents,
       COALESCE(f.contributed_cents,0) AS contributed_cents,
       g.target_cents - COALESCE(f.contributed_cents,0) AS remaining_cents,
       CASE WHEN g.target_cents=0 THEN 0
            ELSE MIN(100.0, ROUND(100.0*COALESCE(f.contributed_cents,0)/g.target_cents,1)) END AS pct_complete,
       COALESCE(f.months_funded,0) AS months_funded, f.last_funded_month
FROM goals g LEFT JOIN funded f ON f.goal_id=g.id;
