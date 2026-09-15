-- Statements group spend by card, but a card number is not a person.
--
-- A credit-card statement splits its transactions into per-card sections, so a
-- household with supplemental cards needs those sections attributed to people
-- before any per-person spend report means anything. The statement itself only
-- ever prints the primary holder's name, so the mapping has to be recorded once
-- by the user.
--
-- An unnamed card is deliberately not an error: a new supplemental card
-- appearing mid-statement is ordinary, and it reports under a stable synthetic
-- label until someone names it.

CREATE TABLE card_holders (
  id INTEGER PRIMARY KEY,
  account_id INTEGER REFERENCES accounts(id),
  card_last4 TEXT NOT NULL
    CHECK (length(card_last4) = 4 AND card_last4 GLOB '[0-9][0-9][0-9][0-9]'),
  display_name TEXT NOT NULL DEFAULT ''
    CHECK (length(display_name) <= 120),
  first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- account_id is nullable, and SQLite treats NULLs as distinct in a UNIQUE
-- constraint, so the scope key is normalised here instead.
CREATE UNIQUE INDEX uq_card_holders_scope
  ON card_holders(IFNULL(account_id, 0), card_last4);
