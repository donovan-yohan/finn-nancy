-- Append-only, content-free record of each Hosted Link completion check.
-- Never stores tokens, account names/masks or credentials; detail holds only
-- counts, provider identifiers, institution ids/names and error/exit codes.
CREATE TABLE bank_link_checks (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES bank_link_sessions(id),
  checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT NOT NULL CHECK (source IN ('browser','server')),
  outcome TEXT NOT NULL CHECK (length(outcome) BETWEEN 1 AND 80),
  detail TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail))
);
CREATE INDEX bank_link_checks_session ON bank_link_checks(session_id, id);
