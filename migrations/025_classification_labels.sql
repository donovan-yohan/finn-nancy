CREATE TABLE IF NOT EXISTS classification_labels (
  id INTEGER PRIMARY KEY,
  transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  category_name TEXT NOT NULL,
  merchant TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL,
  neighbor_ids_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0.0,
  source TEXT NOT NULL DEFAULT '',
  proposed_action_id INTEGER REFERENCES proposed_actions(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_classification_labels_transaction
  ON classification_labels(transaction_id);
CREATE INDEX IF NOT EXISTS idx_classification_labels_category
  ON classification_labels(category_id);
CREATE INDEX IF NOT EXISTS idx_classification_labels_source
  ON classification_labels(source);
