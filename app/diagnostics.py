"""Non-sensitive build and schema diagnostics for exact-head release proof."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .db.migrate import MIGRATIONS_DIR, read_database_identity

ROOT = Path(__file__).resolve().parents[1]
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _package_version() -> str:
    try:
        return version("finn-nancy")
    except PackageNotFoundError:  # pragma: no cover - editable install is normal
        return "unknown"


def _resolve_build_sha() -> str:
    configured = os.getenv("FINN_NANCY_BUILD_SHA", "").strip().lower()
    if configured:
        return configured if _FULL_SHA.fullmatch(configured) else "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    candidate = result.stdout.strip().lower()
    return candidate if result.returncode == 0 and _FULL_SHA.fullmatch(candidate) else "unknown"


def _resolve_build_tree_sha() -> str:
    configured = os.getenv("FINN_NANCY_BUILD_TREE_SHA", "").strip().lower()
    if configured:
        return configured if _FULL_SHA.fullmatch(configured) else "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    candidate = result.stdout.strip().lower()
    return candidate if result.returncode == 0 and _FULL_SHA.fullmatch(candidate) else "unknown"


def migration_sequence_digest(sequence: Sequence[str]) -> str:
    """Digest a complete ordered migration ledger without exposing row data."""
    encoded = json.dumps(list(sequence), ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"finn-nancy-migrations-v1\0" + encoded).hexdigest()


def sqlite_schema_digest(conn: sqlite3.Connection) -> str:
    """Digest SQLite's ordered, non-system schema definitions."""
    rows = conn.execute(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    schema = [tuple(row) for row in rows]
    encoded = json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"finn-nancy-schema-v1\0" + encoded).hexdigest()


def applied_migration_sequence(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return migrations in ledger insertion order, including unknown entries."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if table is None:
        return ()
    return tuple(
        str(row[0])
        for row in conn.execute("SELECT filename FROM schema_migrations ORDER BY rowid")
    )


def migration_status(
    expected: Sequence[str], applied: Sequence[str]
) -> tuple[str, bool]:
    """Classify complete-sequence drift, not merely a matching final filename."""
    expected_tuple = tuple(expected)
    applied_tuple = tuple(applied)
    if applied_tuple == expected_tuple:
        return "ok", True

    expected_set = set(expected_tuple)
    applied_set = set(applied_tuple)
    problems: list[str] = []
    if any(name not in applied_set for name in expected_tuple):
        problems.append("missing")
    if any(name not in expected_set for name in applied_tuple):
        problems.append("unknown")
    known_applied = tuple(name for name in applied_tuple if name in expected_set)
    expected_known = tuple(name for name in expected_tuple if name in applied_set)
    if known_applied != expected_known:
        problems.append("out_of_order")
    if not problems:
        problems.append("sequence_mismatch")
    return "+".join(problems), False


PACKAGE_VERSION = _package_version()
BUILD_SHA = _resolve_build_sha()
BUILD_TREE_SHA = _resolve_build_tree_sha()
EXPECTED_MIGRATION_SEQUENCE = tuple(path.name for path in sorted(MIGRATIONS_DIR.glob("*.sql")))
EXPECTED_MIGRATION_HEAD = (
    EXPECTED_MIGRATION_SEQUENCE[-1] if EXPECTED_MIGRATION_SEQUENCE else None
)
EXPECTED_MIGRATION_DIGEST = migration_sequence_digest(EXPECTED_MIGRATION_SEQUENCE)


def version_payload(conn: sqlite3.Connection) -> dict[str, str | bool | None]:
    """Return revision metadata without paths, environment values, or row data."""
    applied = applied_migration_sequence(conn)
    status, matches = migration_status(EXPECTED_MIGRATION_SEQUENCE, applied)
    return {
        "version": PACKAGE_VERSION,
        "build_sha": BUILD_SHA,
        "build_tree_sha": BUILD_TREE_SHA,
        "database_identity": read_database_identity(conn),
        "schema_digest": sqlite_schema_digest(conn),
        "expected_migration_head": EXPECTED_MIGRATION_HEAD,
        "applied_migration_head": applied[-1] if applied else None,
        "expected_migration_digest": EXPECTED_MIGRATION_DIGEST,
        "applied_migration_digest": migration_sequence_digest(applied),
        "migration_status": status,
        "migration_sequence_matches": matches,
    }
