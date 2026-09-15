CREATE TABLE IF NOT EXISTS planning_insight_card_actions (
  id INTEGER PRIMARY KEY,
  card_key TEXT NOT NULL UNIQUE,
  action TEXT NOT NULL CHECK (
    action IN ('accepted','dismissed','snoozed')
  ),
  acted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_planning_insight_card_actions_key
  ON planning_insight_card_actions(card_key);
