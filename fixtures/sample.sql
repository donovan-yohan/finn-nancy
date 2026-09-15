-- SYNTHETIC_FIXTURE_V1: deterministic records invented for repository tests.
INSERT INTO accounts (id, name, institution, kind, currency) VALUES
  (1, 'Day-to-day checking', 'Example Credit Union', 'chequing', 'CAD'),
  (2, 'Rainy day savings', 'Example Credit Union', 'savings', 'CAD'),
  (3, 'SYNTHETIC_CARD_9001', 'Example Card Issuer', 'credit', 'CAD');

INSERT INTO account_statement_policies(
  account_id, effective_from_month, configuration_state, requirement_mode,
  cadence, anchor_month, active_from, active_to, created_by, reason
) VALUES
  (1, '2026-01', 'configured', 'required', 'monthly', NULL, NULL, NULL,
   'fixture:sample', 'synthetic checking statement policy'),
  (2, '2026-01', 'configured', 'required', 'monthly', NULL, NULL, NULL,
   'fixture:sample', 'synthetic savings statement policy'),
  (3, '2026-01', 'configured', 'required', 'monthly', NULL, NULL, NULL,
   'fixture:sample', 'synthetic card statement policy');

INSERT INTO categories (id, name, kind, brand_owner, color) VALUES
  (1, 'Salary', 'income', 'finn', '#4EA1FF'),
  (2, 'Freelance', 'income', 'nancy', '#FF9F43'),
  (3, 'Rent', 'expense', 'finn', '#7DBBFF'),
  (4, 'Groceries', 'expense', 'finn', '#61D394'),
  (5, 'Restaurants', 'expense', 'nancy', '#FFB86B'),
  (6, 'Subscriptions', 'expense', 'finn', '#9AA9FF'),
  (7, 'Travel', 'expense', 'nancy', '#FF7A59'),
  (8, 'Savings transfer', 'transfer', 'shared', '#C9D1D9'),
  (9, 'Utilities', 'expense', 'finn', '#5BC0EB'),
  (10, 'Marketing experiments', 'expense', 'nancy', '#FFC857');

INSERT INTO transactions (
  id, account_id, posted_on, description, counterparty, amount_cents,
  source, external_id, flow_kind
) VALUES
  (1, 1, '2026-01-01', 'payroll deposit', 'Synthetic Employer', 520000, 'sample', 'fixture-txn-001', 'income'),
  (2, 1, '2026-01-02', 'rent payment', 'Example Housing Co', -210000, 'sample', 'fixture-txn-002', 'purchase'),
  (3, 3, '2026-01-05', 'weekly groceries', 'Synthetic Market', -16243, 'sample', 'fixture-txn-003', 'purchase'),
  (4, 3, '2026-01-08', 'ramen and coffee', 'Example Cafe', -4850, 'sample', 'fixture-txn-004', 'purchase'),
  (5, 1, '2026-01-15', 'freelance invoice', 'Synthetic Studio', 140000, 'sample', 'fixture-txn-005', 'income'),
  (6, 3, '2026-01-20', 'cloud subscriptions', 'Example Software', -7599, 'sample', 'fixture-txn-006', 'purchase'),
  (7, 1, '2026-01-28', 'transfer to savings', 'Rainy day savings', -50000, 'sample', 'fixture-txn-007', 'internal_transfer'),
  (8, 2, '2026-01-28', 'transfer from checking', 'Day-to-day checking', 50000, 'sample', 'fixture-txn-008', 'internal_transfer'),
  (9, 1, '2026-02-01', 'payroll deposit', 'Synthetic Employer', 520000, 'sample', 'fixture-txn-009', 'income'),
  (10, 1, '2026-02-02', 'rent payment', 'Example Housing Co', -210000, 'sample', 'fixture-txn-010', 'purchase'),
  (11, 3, '2026-02-06', 'weekly groceries', 'Synthetic Market', -17102, 'sample', 'fixture-txn-011', 'purchase'),
  (12, 3, '2026-02-12', 'domain and newsletter test', 'Example Marketing', -4421, 'sample', 'fixture-txn-012', 'purchase'),
  (13, 3, '2026-02-16', 'utility bill', 'Example Utility', -9234, 'sample', 'fixture-txn-013', 'purchase'),
  (14, 3, '2026-02-21', 'weekend trip', 'Example Transit', -18430, 'sample', 'fixture-txn-014', 'purchase'),
  (15, 1, '2026-03-01', 'payroll deposit', 'Synthetic Employer', 520000, 'sample', 'fixture-txn-015', 'income'),
  (16, 1, '2026-03-02', 'rent payment', 'Example Housing Co', -210000, 'sample', 'fixture-txn-016', 'purchase'),
  (17, 3, '2026-03-05', 'weekly groceries', 'Synthetic Market', -15870, 'sample', 'fixture-txn-017', 'purchase'),
  (18, 3, '2026-03-11', 'dinner with friends', 'Example Noodle Shop', -6760, 'sample', 'fixture-txn-018', 'purchase'),
  (19, 1, '2026-03-14', 'freelance invoice', 'Synthetic Studio', 90000, 'sample', 'fixture-txn-019', 'income'),
  (20, 3, '2026-03-19', 'cloud subscriptions', 'Example Software', -7899, 'sample', 'fixture-txn-020', 'purchase');

INSERT INTO transaction_relationships(
  relationship_kind, source_transaction_id, target_transaction_id, created_by, reason
) VALUES (
  'transfer_pair', 7, 8, 'fixture:sample', 'synthetic equal-and-opposite transfer'
);

INSERT INTO transaction_splits (transaction_id, category_id, amount_cents, memo) VALUES
  (1, 1, 520000, 'salary'),
  (2, 3, -210000, 'rent'),
  (3, 4, -16243, 'groceries'),
  (4, 5, -4850, 'restaurants'),
  (5, 2, 140000, 'freelance'),
  (6, 6, -7599, 'subscriptions'),
  (7, 8, -50000, 'transfer out'),
  (8, 8, 50000, 'transfer in'),
  (9, 1, 520000, 'salary'),
  (10, 3, -210000, 'rent'),
  (11, 4, -17102, 'groceries'),
  (12, 10, -4421, 'marketing'),
  (13, 9, -9234, 'utilities'),
  (14, 7, -18430, 'travel'),
  (15, 1, 520000, 'salary'),
  (16, 3, -210000, 'rent'),
  (17, 4, -15870, 'groceries'),
  (18, 5, -6760, 'restaurants'),
  (19, 2, 90000, 'freelance'),
  (20, 6, -7899, 'subscriptions');
