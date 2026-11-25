-- Email authorizations are supplemental evidence, not Plaid or statement rows.
CREATE TABLE rogers_alert_mailboxes (
  source_key TEXT PRIMARY KEY CHECK(length(source_key)=64),
  uid_validity INTEGER NOT NULL CHECK(uid_validity BETWEEN 1 AND 4294967295),
  last_uid INTEGER NOT NULL CHECK(last_uid BETWEEN 0 AND 4294967295),
  rejected_count INTEGER NOT NULL DEFAULT 0 CHECK(rejected_count>=0),
  conflict_count INTEGER NOT NULL DEFAULT 0 CHECK(conflict_count>=0)
);

CREATE TABLE rogers_alerts (
  id INTEGER PRIMARY KEY,
  source_key TEXT NOT NULL REFERENCES rogers_alert_mailboxes(source_key),
  message_key TEXT NOT NULL UNIQUE CHECK(length(message_key)=64),
  authorized_on TEXT NOT NULL CHECK(date(authorized_on,'+0 days') IS authorized_on),
  card_last4 TEXT NOT NULL CHECK(card_last4 GLOB '[0-9][0-9][0-9][0-9]'),
  amount_cents INTEGER NOT NULL CHECK(amount_cents<0),
  currency TEXT NOT NULL CHECK(currency GLOB '[A-Z][A-Z][A-Z]'),
  merchant TEXT NOT NULL CHECK(length(trim(merchant)) BETWEEN 1 AND 200),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE rogers_alert_occurrences (
  alert_id INTEGER NOT NULL REFERENCES rogers_alerts(id),
  source_key TEXT NOT NULL REFERENCES rogers_alert_mailboxes(source_key),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(alert_id,source_key)
) WITHOUT ROWID;

CREATE TABLE rogers_alert_retries (
  source_key TEXT NOT NULL REFERENCES rogers_alert_mailboxes(source_key),
  uid_validity INTEGER NOT NULL CHECK(uid_validity BETWEEN 1 AND 4294967295),
  uid INTEGER NOT NULL CHECK(uid BETWEEN 1 AND 4294967295),
  retry_at INTEGER NOT NULL CHECK(retry_at>=0),
  PRIMARY KEY(source_key,uid_validity,uid)
) WITHOUT ROWID;
CREATE INDEX rogers_alert_retries_due ON rogers_alert_retries(source_key,uid_validity,retry_at,uid);

CREATE TABLE rogers_alert_reviews (
  id INTEGER PRIMARY KEY,
  alert_id INTEGER NOT NULL REFERENCES rogers_alerts(id),
  previous_id INTEGER REFERENCES rogers_alert_reviews(id),
  decision TEXT NOT NULL CHECK(decision IN ('confirm','dismiss')),
  account_id INTEGER REFERENCES accounts(id),
  actor TEXT NOT NULL DEFAULT 'operator:household',
  reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 500),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK((decision='confirm')=(account_id IS NOT NULL))
);
CREATE INDEX rogers_alert_reviews_latest ON rogers_alert_reviews(alert_id,id DESC);

CREATE TRIGGER rogers_alert_review_validate BEFORE INSERT ON rogers_alert_reviews
WHEN EXISTS (SELECT 1 FROM rogers_alert_reviews WHERE id=NEW.id)
 OR NEW.previous_id IS NOT (SELECT MAX(id) FROM rogers_alert_reviews WHERE alert_id=NEW.alert_id)
 OR (NEW.decision='confirm' AND NOT EXISTS (
     SELECT 1 FROM accounts a JOIN rogers_alerts r ON r.id=NEW.alert_id
     WHERE a.id=NEW.account_id AND a.is_active=1 AND a.kind='credit' AND a.currency=r.currency))
BEGIN SELECT RAISE(ABORT,'invalid alert review'); END;

-- The generated row ID is only reliable after INSERT. Keep the latest-id
-- projection and the predecessor chain in the same strictly increasing order.
CREATE TRIGGER rogers_alert_review_order AFTER INSERT ON rogers_alert_reviews
WHEN NEW.id<=COALESCE(NEW.previous_id,0)
BEGIN SELECT RAISE(ABORT,'invalid alert review order'); END;

CREATE TRIGGER rogers_alert_review_period BEFORE INSERT ON rogers_alert_reviews
WHEN EXISTS (SELECT 1 FROM rogers_alerts a JOIN v_period_policy_locked_months m
 ON m.month=substr(a.authorized_on,1,7) WHERE a.id=NEW.alert_id)
BEGIN SELECT RAISE(ABORT,'closed period alert review'); END;

-- REPLACE can bypass delete triggers when recursive triggers are off.
CREATE TRIGGER rogers_alert_no_replace BEFORE INSERT ON rogers_alerts
WHEN EXISTS (SELECT 1 FROM rogers_alerts WHERE id=NEW.id OR message_key=NEW.message_key)
BEGIN SELECT RAISE(ABORT,'alert evidence is immutable'); END;
CREATE TRIGGER rogers_alert_no_update BEFORE UPDATE ON rogers_alerts
BEGIN SELECT RAISE(ABORT,'alert evidence is immutable'); END;
CREATE TRIGGER rogers_alert_no_delete BEFORE DELETE ON rogers_alerts
BEGIN SELECT RAISE(ABORT,'alert evidence is immutable'); END;
CREATE TRIGGER rogers_alert_occurrence_no_replace BEFORE INSERT ON rogers_alert_occurrences
WHEN EXISTS (SELECT 1 FROM rogers_alert_occurrences WHERE alert_id=NEW.alert_id AND source_key=NEW.source_key)
BEGIN SELECT RAISE(ABORT,'alert occurrences are immutable'); END;
CREATE TRIGGER rogers_alert_occurrence_no_update BEFORE UPDATE ON rogers_alert_occurrences
BEGIN SELECT RAISE(ABORT,'alert occurrences are immutable'); END;
CREATE TRIGGER rogers_alert_occurrence_no_delete BEFORE DELETE ON rogers_alert_occurrences
BEGIN SELECT RAISE(ABORT,'alert occurrences are immutable'); END;
CREATE TRIGGER rogers_alert_review_no_update BEFORE UPDATE ON rogers_alert_reviews
BEGIN SELECT RAISE(ABORT,'alert review is append-only'); END;
CREATE TRIGGER rogers_alert_review_no_delete BEFORE DELETE ON rogers_alert_reviews
BEGIN SELECT RAISE(ABORT,'alert review is append-only'); END;
