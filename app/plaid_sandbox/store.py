"""Private candidate persistence; deliberately separate from the application ledger."""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
import json
import hashlib
import stat
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from pathlib import Path

CONSENT = "plaid-sandbox.v1"
RECOVERY_CONSENT = "plaid-sandbox-recovery.v1"


def _text(value, limit):
    # These are application display bounds, not claims about Plaid limits.
    if not isinstance(value, str) or len(value) > limit or not value.strip() or "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _enrichment(row, *, stored=False):
    """Only provider-attributed display metadata, never arbitrary payload fields."""
    if not isinstance(row, dict):
        return None
    counterparties = []
    raw = row.get("counterparties")
    if isinstance(raw, list):
        for party in raw:
            if isinstance(party, dict):
                item = {key: _text(party.get(key), limit) for key, limit in
                        (("name", 512), ("entity_id", 256), ("type", 64), ("confidence_level", 64))}
                if any(item.values()):
                    counterparties.append(item)
                    if len(counterparties) == 10:
                        break
    category = row.get("category_suggestion" if stored else "personal_finance_category")
    suggestion = None
    if isinstance(category, dict):
        suggestion = {key: _text(category.get(key), limit) for key, limit in
                      (("primary", 128), ("detailed", 128), ("confidence_level", 64))}
        suggestion["version"] = _text(category.get("version"), 64)
        if not any(suggestion.values()):
            suggestion = None
    result = {**{key: _text(row.get(key), limit) for key, limit in
                 (("merchant_name", 512), ("merchant_entity_id", 256), ("original_description", 4096))},
              "counterparties": counterparties, "category_suggestion": suggestion}
    return result if any(result.values()) else None


def _enrichment_json(row):
    value = _enrichment(row)
    return json.dumps(value, sort_keys=True, separators=(",", ":")) if value is not None else None


def _project_row(row):
    result = dict(row)
    raw = result.pop("enrichment_json", None)
    try:
        enrichment = _enrichment(json.loads(raw), stored=True) if raw is not None else None
    except (ValueError, TypeError):
        enrichment = None
    result.update(enrichment=enrichment, flow_kind="unknown", booked=False)
    return result


class Store:
    def __init__(self, root: Path, *, credentials_file: Path | None = None):
        self.credentials_file = Path(credentials_file).absolute() if credentials_file else None
        root = Path(root)
        if root.is_symlink():
            raise ValueError("unsafe candidate root")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve()
        os.chmod(self.root, 0o700)
        self.path = self.root / "sandbox.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), client_id TEXT, secret TEXT, consent TEXT, enabled INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT OR IGNORE INTO config(id) VALUES(1)")
            db.execute("CREATE TABLE IF NOT EXISTS browser_sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, created REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS household_members (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS link_sessions (
                id TEXT PRIMARY KEY, browser TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES household_members(id),
                link_token TEXT NOT NULL, created REAL NOT NULL, connection_id TEXT, status TEXT NOT NULL DEFAULT 'ready',
                public_digest TEXT UNIQUE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'plaid' CHECK(provider='plaid'),
                environment TEXT NOT NULL DEFAULT 'sandbox' CHECK(environment='sandbox'),
                item_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL REFERENCES household_members(id),
                access_token TEXT, status TEXT NOT NULL DEFAULT 'connected', cursor TEXT NOT NULL DEFAULT '',
                last_local_sync TEXT, last_bank_update TEXT, error TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS accounts (
                connection_id TEXT NOT NULL REFERENCES connections(id), account_id TEXT NOT NULL,
                name TEXT NOT NULL, mask TEXT, type TEXT, subtype TEXT, selected INTEGER NOT NULL DEFAULT 0,
                ownership TEXT NOT NULL DEFAULT 'unassigned', PRIMARY KEY(connection_id,account_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id),
                transaction_id TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(connection_id,transaction_id,kind,digest))""")
            db.execute("""CREATE TABLE IF NOT EXISTS staged (
                connection_id TEXT NOT NULL REFERENCES connections(id), transaction_id TEXT NOT NULL,
                account_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT, date TEXT, name TEXT,
                state TEXT NOT NULL, pending_transaction_id TEXT, PRIMARY KEY(connection_id,transaction_id))""")
            db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, event TEXT NOT NULL, actor TEXT NOT NULL, target TEXT, at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
            if "enrichment_json" not in {row["name"] for row in db.execute("PRAGMA table_info(staged)")}:
                db.execute("ALTER TABLE staged ADD COLUMN enrichment_json TEXT")
                self._recover_enrichment(db)
            # Rollback writers omit the projection column, even when applying
            # changed metadata with identical core values. Invalidate on source
            # statements, not value differences; state-only tombstones retain it.
            db.execute("""CREATE TRIGGER IF NOT EXISTS staged_source_invalidates_enrichment
                AFTER UPDATE OF connection_id,transaction_id,account_id,amount_cents,currency,date,name,pending_transaction_id ON staged
                BEGIN
                    UPDATE staged SET enrichment_json=NULL
                    WHERE connection_id=NEW.connection_id AND transaction_id=NEW.transaction_id;
                END""")

    @staticmethod
    def _recover_enrichment(db):
        """One-time legacy projection recovery, never latest-observation inference.

        Deduplicated A/B/A observations cannot tell us which payload was applied
        last. Recover only if *all* source-matching candidates agree, including
        candidates with no enrichment. Existing schema, evidence and core rows
        are otherwise untouched; a later reopen never fills an intentional null.
        """
        for staged in db.execute("SELECT * FROM staged").fetchall():
            candidates = set()
            observations = db.execute("""SELECT payload FROM observations
                WHERE connection_id=? AND transaction_id=? AND kind IN ('added','modified')""",
                (staged["connection_id"], staged["transaction_id"]))
            for observation in observations:
                try:
                    row = json.loads(observation["payload"])
                    if not isinstance(row, dict) or any(row.get(source) != staged[target] for source, target in (
                        ("transaction_id", "transaction_id"), ("account_id", "account_id"),
                        ("iso_currency_code", "currency"), ("date", "date"), ("name", "name"),
                        ("pending_transaction_id", "pending_transaction_id"))):
                        continue
                    amount = Decimal(str(row.get("amount")))
                    if not amount.is_finite() or amount != -Decimal(staged["amount_cents"]) / 100:
                        continue
                    if staged["state"] in {"posted", "pending"}:
                        pending = row.get("pending", False)
                        if type(pending) is not bool or pending != (staged["state"] == "pending"):
                            continue
                    candidates.add(_enrichment_json(row))
                except (ValueError, TypeError, InvalidOperation):
                    continue
            if len(candidates) == 1:
                enrichment = candidates.pop()
                if enrichment is not None:
                    db.execute("UPDATE staged SET enrichment_json=? WHERE connection_id=? AND transaction_id=?",
                               (enrichment, staged["connection_id"], staged["transaction_id"]))

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add_member(self, name, actor):
        if not isinstance(name, str) or not name.strip() or len(name) > 60 or any(ord(c) < 32 for c in name):
            raise ValueError("invalid member")
        member_id = uuid.uuid4().hex
        with self.tx() as db:
            db.execute("INSERT INTO household_members VALUES(?,?)", (member_id, name.strip()))
            db.execute("INSERT INTO audit(event,actor,target) VALUES('member_added',?,?)", (actor, member_id))
        return member_id

    def map_account(self, connection_id, account_id, *, selected, ownership, actor):
        with self.tx() as db:
            members = {r[0] for r in db.execute("SELECT id FROM household_members")}
            if ownership not in members | {"shared", "unassigned"} or type(selected) is not bool:
                raise ValueError("invalid mapping")
            result = db.execute("UPDATE accounts SET selected=?,ownership=? WHERE connection_id=? AND account_id=?",
                                (selected, ownership, connection_id, account_id))
            if result.rowcount != 1:
                raise ValueError("unknown account")
            db.execute("INSERT INTO audit(event,actor,target) VALUES('account_mapped',?,?)", (actor, connection_id))

    def snapshot(self, owner_id=None):
        with self.tx() as db:
            return self._snapshot(db, owner_id)

    def _snapshot(self, db, owner_id=None, *, include_rows=True) -> dict:
        connections = [dict(r) for r in db.execute(
            "SELECT id,provider,environment,item_id,owner_id,status,last_local_sync,last_bank_update,error FROM connections ORDER BY rowid")
            if owner_id is None or r["owner_id"] == owner_id]
        ids = {c["id"] for c in connections}
        result = {"link_attempts": [dict(r) for r in db.execute("""SELECT l.id,l.owner_id,l.status,l.connection_id,
                      (SELECT MIN(at) FROM audit WHERE event='exchange_acknowledged' AND target=l.id) AS acknowledged_at
                      FROM link_sessions l ORDER BY l.rowid""") if owner_id is None or r["owner_id"] == owner_id],
                  "members": [dict(r) for r in db.execute("SELECT * FROM household_members ORDER BY rowid")],
                  "connections": connections,
                  "accounts": [dict(r) for r in db.execute("SELECT * FROM accounts ORDER BY rowid") if r["connection_id"] in ids]}
        if include_rows:
            result["staged"] = [_project_row(r) for r in db.execute(
                """SELECT s.*,a.name AS account_name,a.ownership,a.selected,c.provider,c.environment,c.item_id,c.owner_id
                   FROM staged s JOIN accounts a USING(connection_id,account_id)
                   JOIN connections c ON c.id=s.connection_id ORDER BY s.rowid""") if r["connection_id"] in ids]
        return result

    def overview(self):
        """Return one consistent import overview, never financial ledger totals."""
        with self.tx() as db:
            result = self._snapshot(db, include_rows=False)
            counts = {(row["connection_id"], row["account_id"]): dict(row) for row in db.execute(
                """SELECT connection_id,account_id,COUNT(*) AS transaction_count,
                          SUM(state='pending') AS pending_count,MAX(date) AS last_date
                   FROM staged WHERE state IN ('posted','pending') GROUP BY connection_id,account_id""")}
            result["accounts"] = [{**account, **counts.get((account["connection_id"], account["account_id"]),
                {"transaction_count": 0, "pending_count": 0, "last_date": None})} for account in result["accounts"]]
            result["summary"] = {
                "connection_count": len(result["connections"]), "account_count": len(result["accounts"]),
                "transaction_count": sum(account["transaction_count"] for account in result["accounts"]),
                "pending_count": sum(account["pending_count"] for account in result["accounts"]),
                "last_local_sync": max((c["last_local_sync"] for c in result["connections"] if c["last_local_sync"]), default=None),
            }
            result["recent"] = self._activity(db, limit=8)["rows"]
            return result

    def activity(self, **filters):
        with self.tx() as db:
            return self._activity(db, **filters)

    def _activity(self, db, *, owner_id=None, connection_id=None, account_id=None, q="",
                  date_from=None, date_to=None, state="current", inclusion="all", offset=0, limit=50):
        """Bounded, connection-scoped inspection of unbooked provider evidence."""
        if state not in {"current", "posted", "pending", "removed", "superseded", "all"} or inclusion not in {"all", "selected", "excluded"}:
            raise ValueError("invalid activity filter")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid activity page")
        if not isinstance(q, str) or len(q) > 120:
            raise ValueError("invalid activity search")
        for value in (owner_id, connection_id, account_id):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 256):
                raise ValueError("invalid activity identity")
        if account_id is not None and connection_id is None:
            raise ValueError("account requires connection identity")
        for value in (date_from, date_to):
            if value is not None and (not isinstance(value, str) or date.fromisoformat(value).isoformat() != value):
                raise ValueError("invalid activity date")
        if date_from and date_to and date_from > date_to:
            raise ValueError("invalid activity date range")

        conditions, values = [], []
        if state == "current":
            conditions.append("s.state IN ('posted','pending')")
        elif state != "all":
            conditions.append("s.state=?")
            values.append(state)
        for column, value in (("c.owner_id", owner_id), ("s.connection_id", connection_id), ("s.account_id", account_id)):
            if value is not None:
                conditions.append(column + "=?")
                values.append(value)
        if q:
            search = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            conditions.append("""(s.name LIKE ? ESCAPE '!' OR a.name LIKE ? ESCAPE '!'
                OR json_extract(CASE WHEN json_valid(s.enrichment_json) THEN s.enrichment_json END,
                                '$.merchant_name') LIKE ? ESCAPE '!'
                OR json_extract(CASE WHEN json_valid(s.enrichment_json) THEN s.enrichment_json END,
                                '$.original_description') LIKE ? ESCAPE '!')""")
            values.extend((search, search, search, search))
        for comparison, value in ((">=", date_from), ("<=", date_to)):
            if value is not None:
                conditions.append("s.date" + comparison + "?")
                values.append(value)
        if inclusion != "all":
            conditions.append("a.selected=?")
            values.append(int(inclusion == "selected"))
        source = """FROM staged s JOIN accounts a USING(connection_id,account_id)
                    JOIN connections c ON c.id=s.connection_id"""
        if conditions:
            source += " WHERE " + " AND ".join(conditions)
        total = db.execute("SELECT COUNT(*) " + source, values).fetchone()[0]
        rows = [_project_row(row) for row in db.execute(
            """SELECT s.*,a.name AS account_name,a.ownership,a.selected,
                      c.provider,c.environment,c.item_id,c.owner_id """ + source +
            " ORDER BY s.date DESC,s.connection_id,s.transaction_id LIMIT ? OFFSET ?", [*values, limit, offset])]
        return {"rows": rows, "total": total, "offset": offset, "limit": limit,
                "has_more": offset + len(rows) < total}

    @contextmanager
    def operation(self):
        # Serializes multi-transaction network workflows across threads/processes.
        import fcntl
        fd = os.open(self.root / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def new_link(self, browser, owner_id, link_token, connection_id=None):
        import time
        link_id = uuid.uuid4().hex
        with self.tx() as db:
            db.execute("INSERT INTO link_sessions(id,browser,owner_id,link_token,created,connection_id) VALUES(?,?,?,?,?,?)",
                       (link_id, browser, owner_id, link_token, time.time(), connection_id))
            db.execute("INSERT INTO audit(event,actor,target) VALUES('link_consented',?,?)", (browser, owner_id))
        return link_id

    def link(self, session_id):
        with self.tx() as db:
            row = db.execute("SELECT * FROM link_sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def acknowledge_exchange(self, session_id, consent, actor):
        """Record a manual dashboard check, never remote outcome or replay permission."""
        if consent != RECOVERY_CONSENT:
            raise ValueError("recovery consent required")
        with self.operation(), self.tx() as db:
            link = db.execute("SELECT status,connection_id FROM link_sessions WHERE id=?", (session_id,)).fetchone()
            if not link or link["status"] != "uncertain" or link["connection_id"] is not None:
                raise ValueError("uncertain new-item attempt required")
            if not db.execute("SELECT 1 FROM audit WHERE event='exchange_acknowledged' AND target=?", (session_id,)).fetchone():
                db.execute("INSERT INTO audit(event,actor,target) VALUES('exchange_acknowledged',?,?)", (actor, session_id))

    def claim_exchange(self, session_id, digest):
        with self.tx() as db:
            db.execute("UPDATE link_sessions SET status='uncertain',public_digest=? WHERE id=?", (digest, session_id))

    def finish_exchange(self, session_id, owner_id, item_id, access_token, actor):
        with self.tx() as db:
            existing = db.execute("SELECT id,owner_id FROM connections WHERE item_id=?", (item_id,)).fetchone()
            if existing:
                if existing["owner_id"] != owner_id:
                    raise ValueError("owner conflict")
                connection_id = existing["id"]
            else:
                connection_id = uuid.uuid4().hex
                db.execute("INSERT INTO connections(id,item_id,owner_id,access_token) VALUES(?,?,?,?)",
                           (connection_id, item_id, owner_id, access_token))
            db.execute("UPDATE link_sessions SET status='complete',connection_id=? WHERE id=?", (connection_id, session_id))
            db.execute("INSERT INTO audit(event,actor,target) VALUES('item_linked',?,?)", (actor, connection_id))
        return connection_id

    def connection(self, connection_id):
        with self.tx() as db:
            row = db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
            if not row:
                raise ValueError("unknown connection")
            return dict(row)

    def save_accounts(self, connection_id, accounts):
        with self.tx() as db:
            for account in accounts:
                db.execute("""INSERT INTO accounts(connection_id,account_id,name,mask,type,subtype) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(connection_id,account_id) DO UPDATE SET name=excluded.name,mask=excluded.mask,type=excluded.type,subtype=excluded.subtype""",
                    (connection_id, account["account_id"], account["name"], account.get("mask"), account.get("type"), account.get("subtype")))

    def commit_window(self, connection_id, original_cursor, cursor, changes, bank_update, actor):
        with self.tx() as db:
            current = db.execute("SELECT cursor FROM connections WHERE id=?", (connection_id,)).fetchone()
            if current[0] != original_cursor:
                raise ValueError("stale cursor")
            for kind, row in changes:
                payload = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO observations(connection_id,transaction_id,kind,digest,payload) VALUES(?,?,?,?,?)",
                           (connection_id, row["transaction_id"], kind, digest, payload))
                if kind == "removed":
                    db.execute("UPDATE staged SET state=CASE WHEN state='superseded' THEN state ELSE 'removed' END WHERE connection_id=? AND transaction_id=?",
                               (connection_id, row["transaction_id"]))
                    continue
                pending_id = row.get("pending_transaction_id")
                if pending_id:
                    db.execute("UPDATE staged SET state='superseded' WHERE connection_id=? AND transaction_id=?",
                               (connection_id, pending_id))
                if not db.execute("SELECT 1 FROM accounts WHERE connection_id=? AND account_id=?", (connection_id, row["account_id"])).fetchone():
                    raise ValueError("unknown provider account")
                amount = -Decimal(str(row["amount"])) * 100
                if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount) > 9_000_000_000_000_000:
                    raise ValueError("invalid amount precision")
                db.execute("""INSERT INTO staged(connection_id,transaction_id,account_id,amount_cents,currency,date,name,state,pending_transaction_id)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id,transaction_id) DO UPDATE SET
                    account_id=excluded.account_id,amount_cents=excluded.amount_cents,currency=excluded.currency,
                    date=excluded.date,name=excluded.name,state=excluded.state,pending_transaction_id=excluded.pending_transaction_id""",
                    (connection_id, row["transaction_id"], row["account_id"], int(amount), row.get("iso_currency_code"),
                     row.get("date"), row.get("name"), "pending" if row.get("pending") else "posted", row.get("pending_transaction_id")))
                # A separate enrichment-only write bypasses the source guard,
                # atomically with staging, observations, cursor and audit.
                db.execute("UPDATE staged SET enrichment_json=? WHERE connection_id=? AND transaction_id=?",
                           (_enrichment_json(row), connection_id, row["transaction_id"]))
            db.execute("UPDATE connections SET cursor=?,last_local_sync=?,last_bank_update=?,status='connected',error=NULL WHERE id=?",
                       (cursor, datetime.now(timezone.utc).isoformat(), bank_update, connection_id))
            db.execute("INSERT INTO audit(event,actor,target) VALUES('sync_committed',?,?)", (actor, connection_id))

    def connection_error(self, connection_id, code, actor):
        with self.tx() as db:
            db.execute("UPDATE connections SET status='needs_repair',error=? WHERE id=?", (code, connection_id))
            db.execute("INSERT INTO audit(event,actor,target) VALUES('connection_error',?,?)", (actor, connection_id))

    def repaired(self, session_id, connection_id, actor):
        with self.tx() as db:
            db.execute("UPDATE link_sessions SET status='complete' WHERE id=?", (session_id,))
            db.execute("UPDATE connections SET status='connected',error=NULL WHERE id=?", (connection_id,))
            db.execute("INSERT INTO audit(event,actor,target) VALUES('item_repaired',?,?)", (actor, connection_id))

    def revocation_state(self, connection_id, actor, *, uncertain=False):
        with self.tx() as db:
            db.execute("UPDATE connections SET status=?,error=? WHERE id=?",
                       ("revocation_uncertain" if uncertain else "revoking", "revocation_uncertain" if uncertain else None, connection_id))
            db.execute("INSERT INTO audit(event,actor,target) VALUES(?,?,?)",
                       ("revocation_uncertain" if uncertain else "revocation_started", actor, connection_id))

    def disconnected(self, connection_id, actor, *, confirmed=True):
        with self.tx() as db:
            db.execute("UPDATE connections SET status=?,access_token=NULL,error=? WHERE id=?",
                       ("disconnected" if confirmed else "revocation_unconfirmed", None if confirmed else "token_invalid_verify_revocation_in_sandbox_dashboard", connection_id))
            db.execute("INSERT INTO audit(event,actor,target) VALUES(?,?,?)",
                       ("item_disconnected" if confirmed else "revoke_token_invalid", actor, connection_id))

    def browser(self, cookie=None):
        import time
        with self.tx() as db:
            row = db.execute("SELECT * FROM browser_sessions WHERE id=? AND created>?", (cookie, time.time() - 86400)).fetchone()
            return dict(row) if row else None

    def new_browser(self):
        import secrets
        import time
        session = {"id": secrets.token_urlsafe(32), "csrf": secrets.token_urlsafe(32), "created": time.time()}
        with self.tx() as db:
            db.execute("INSERT INTO browser_sessions VALUES(:id,:csrf,:created)", session)
        return session

    def status(self):
        with self.tx() as db:
            row = db.execute("SELECT * FROM config WHERE id=1").fetchone()
            configured = self.private_file_ready() if self.credentials_file else bool(row["client_id"] and row["secret"])
            result = {"configured": configured,
                      "enabled": bool(configured and row["enabled"] and row["consent"] == CONSENT), "consent_version": CONSENT}
            if self.credentials_file:
                result["credential_source"] = "private_file"
            return result

    def private_file_ready(self):
        path = self.credentials_file
        if path is None:
            return False
        try:
            if any(p.is_symlink() for p in (path, *path.parents)):
                return False
            parent = path.parent.stat()
            info = path.stat()
            return (stat.S_ISDIR(parent.st_mode) and stat.S_IMODE(parent.st_mode) == 0o700
                    and parent.st_uid == os.getuid() and stat.S_ISREG(info.st_mode)
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.getuid()
                    and info.st_nlink == 1 and 1 <= info.st_size <= 4096)
        except OSError:
            return False

    def enable_private_file(self, consent, actor):
        if consent != CONSENT or not self.private_file_ready():
            raise ValueError("private sandbox handoff unavailable")
        with self.operation(), self.tx() as db:
            db.execute("UPDATE config SET client_id=NULL,secret=NULL,consent=?,enabled=1 WHERE id=1", (CONSENT,))
            db.execute("INSERT INTO audit(event,actor) VALUES('sandbox_private_file_enabled',?)", (actor,))

    def _private_file_credentials(self):
        from .client import SandboxError
        if not self.private_file_ready():
            raise SandboxError("private_handoff_unavailable")
        parent_fd = fd = None
        try:
            parent_fd = os.open(self.credentials_file.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent = os.fstat(parent_fd)
            if stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.getuid():
                raise ValueError()
            fd = os.open(self.credentials_file.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError()
            raw = os.read(fd, 4097)
            if len(raw) > 4096:
                raise ValueError()
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            data = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(data, dict) or set(data) != {"environment", "client_id", "secret"} or data["environment"] != "sandbox":
                raise ValueError()
            if not all(isinstance(data[k], str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", data[k]) for k in ("client_id", "secret")):
                raise ValueError()
            return data["client_id"], data["secret"]
        except (OSError, ValueError, TypeError):
            raise SandboxError("private_handoff_invalid") from None
        finally:
            if fd is not None:
                os.close(fd)
            if parent_fd is not None:
                os.close(parent_fd)

    def credentials(self):
        with self.tx() as db:
            row = db.execute("SELECT * FROM config WHERE id=1").fetchone()
            if row["enabled"] and row["consent"] == CONSENT:
                if self.credentials_file:
                    return self._private_file_credentials()
                if row["client_id"] and row["secret"]:
                    return row["client_id"], row["secret"]
        return None

    def configure(self, client_id, secret, consent, actor="local-setup"):
        if self.credentials_file:
            raise ValueError("private file handoff required")
        if consent != CONSENT or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", v) for v in (client_id, secret)):
            raise ValueError("invalid sandbox setup")
        with self.tx() as db:
            db.execute("UPDATE config SET client_id=?, secret=?, consent=?, enabled=1 WHERE id=1", (client_id, secret, CONSENT))
            db.execute("INSERT INTO audit(event,actor) VALUES('sandbox_enabled',?)", (actor,))

    def disable(self, actor="local-setup"):
        with self.tx() as db:
            db.execute("UPDATE config SET enabled=0 WHERE id=1")
            db.execute("INSERT INTO audit(event,actor) VALUES('sandbox_disabled',?)", (actor,))
