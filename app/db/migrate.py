"""Numbered .sql migration runner.

Each ``migrations/NNN_*.sql`` is applied exactly once, in filename order, tracked
in a ``schema_migrations`` ledger. Deliberately tiny — no Alembic. The canonical
``001_tables.sql`` / ``002_views.sql`` are copied verbatim from the original Go app
and must never be edited; later changes are additive files.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from pathlib import Path

from .engine import connect

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT / "migrations"
FIXTURES_SQL = ROOT / "fixtures" / "sample.sql"


_BASELINE = ("001_tables.sql", "002_views.sql")
DATABASE_IDENTITY_SETTING_KEY = "__finn_nancy_database_uuid"
_ATOMIC_MIGRATION_FORBIDDEN = {
    "ATTACH",
    "BEGIN",
    "COMMIT",
    "DETACH",
    "END",
    "PRAGMA",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
    "VACUUM",
}


def _top_level_statements(sql: str):
    """Yield complete top-level SQLite statements.

    ``sqlite3.complete_statement`` understands trigger bodies, so their internal
    ``BEGIN ... END`` block remains part of the outer ``CREATE TRIGGER`` statement
    instead of being mistaken for transaction control.
    """
    buffer: list[str] = []
    for char in sql:
        buffer.append(char)
        if char == ";" and sqlite3.complete_statement("".join(buffer)):
            yield "".join(buffer)
            buffer = []
    tail = "".join(buffer)
    if tail.strip():
        yield tail


def _without_leading_comments(statement: str) -> str:
    remaining = statement.lstrip()
    while remaining:
        if remaining.startswith("--"):
            _, separator, remaining = remaining.partition("\n")
            if not separator:
                return ""
            remaining = remaining.lstrip()
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            if end < 0:
                return ""
            remaining = remaining[end + 2 :].lstrip()
            continue
        break
    return remaining


def _assert_atomic_migration_compatible(path: Path, sql: str) -> None:
    """Reject commands that can escape or conflict with the runner transaction."""
    for statement in _top_level_statements(sql):
        code = _without_leading_comments(statement)
        match = re.match(r"([A-Za-z_]+)", code)
        keyword = match.group(1).upper() if match else ""
        if keyword in _ATOMIC_MIGRATION_FORBIDDEN:
            raise RuntimeError(
                f"{path.name}: top-level {keyword} is incompatible with atomic migrations"
            )


def _ensure_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
             filename   TEXT PRIMARY KEY,
             applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.commit()


def _baseline_if_legacy(conn: sqlite3.Connection) -> None:
    """Adopt a pre-existing canonical DB that has no migration ledger.

    A legacy finn-nancy DB (e.g. from the earlier namako workflow) already has the
    001/002 schema but no schema_migrations rows. Stamp those baseline files as applied
    so we only run the additive migrations (003+) on top, instead of re-CREATE-ing
    tables that already exist.
    """
    has_core = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
    ).fetchone()
    if not has_core:
        return
    for name in _BASELINE:
        conn.execute("INSERT OR IGNORE INTO schema_migrations(filename) VALUES (?)", (name,))
    conn.commit()


def _validated_database_identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    canonical = str(parsed)
    return canonical if value.lower() == canonical else None


def read_database_identity(conn: sqlite3.Connection) -> str | None:
    """Read the persistent database UUID without creating or changing it."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (DATABASE_IDENTITY_SETTING_KEY,),
    ).fetchone()
    return _validated_database_identity(row[0]) if row is not None else None


def _ensure_database_identity(conn: sqlite3.Connection) -> str | None:
    """Initialize the reserved UUID once, after migration 006 creates settings."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'"
    ).fetchone()
    if table is None:
        return None

    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (DATABASE_IDENTITY_SETTING_KEY,),
    ).fetchone()
    if row is not None:
        existing = _validated_database_identity(row[0])
        if existing is None:
            raise RuntimeError("stored finn-nancy database UUID is invalid")
        return existing

    generated = str(uuid.uuid4())
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO app_settings(key, value) VALUES (?, ?)",
            (DATABASE_IDENTITY_SETTING_KEY, generated),
        )
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (DATABASE_IDENTITY_SETTING_KEY,),
        ).fetchone()
        identity = _validated_database_identity(row[0]) if row is not None else None
        if identity is None:
            raise RuntimeError("failed to initialize finn-nancy database UUID")
        conn.commit()
        return identity
    except Exception:
        conn.rollback()
        raise


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Apply any not-yet-applied migrations. Returns the filenames applied."""
    _ensure_ledger(conn)
    _baseline_if_legacy(conn)
    already = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in already:
            continue
        sql = path.read_text()
        _assert_atomic_migration_compatible(path, sql)
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + sql)
            conn.execute("INSERT INTO schema_migrations(filename) VALUES (?)", (path.name,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(path.name)
    _ensure_database_identity(conn)
    return applied


def init_db(path: str | Path) -> list[str]:
    """Create/upgrade the database at ``path``. Idempotent."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        return apply_migrations(conn)
    finally:
        conn.close()


def seed_sample(path: str | Path) -> None:
    """Rebuild a fake sample database from scratch (schema + fixtures)."""
    p = Path(path)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        apply_migrations(conn)
        conn.executescript(FIXTURES_SQL.read_text())
        conn.commit()
    finally:
        conn.close()
