"""WAL-safe backup manifests used only by delivery and release tooling."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.db.migrate import apply_migrations, read_database_identity
from app.diagnostics import (
    EXPECTED_MIGRATION_SEQUENCE,
    applied_migration_sequence,
    migration_sequence_digest,
    migration_status,
    sqlite_schema_digest,
)

FORMAT = "finn-nancy-backup-manifest-v1"
_HEX_DIGEST_LENGTH = 64
RELEASE_MANIFEST_MAX_AGE = dt.timedelta(hours=24)
_MANIFEST_FUTURE_SKEW = dt.timedelta(minutes=5)


class ManifestError(ValueError):
    """A backup or manifest does not satisfy the release evidence contract."""


def _open_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_state(path: Path) -> dict[str, Any]:
    conn = _open_read_only(path)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ManifestError(f"SQLite integrity_check failed: {integrity}")
        identity = read_database_identity(conn)
        if identity is None:
            raise ManifestError(
                "database UUID is missing or invalid; run the current migration/init first"
            )
        sequence = applied_migration_sequence(conn)
        return {
            "database_identity": identity,
            "schema_digest": sqlite_schema_digest(conn),
            "migration_sequence": list(sequence),
            "migration_digest": migration_sequence_digest(sequence),
        }
    finally:
        conn.close()


def _candidate_state(backup_path: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="finn-nancy-backup-proof.") as tmp:
        candidate_path = Path(tmp) / "candidate.sqlite"
        shutil.copy2(backup_path, candidate_path)
        conn = sqlite3.connect(candidate_path)
        conn.row_factory = sqlite3.Row
        try:
            apply_migrations(conn)
            sequence = applied_migration_sequence(conn)
            status, matches = migration_status(EXPECTED_MIGRATION_SEQUENCE, sequence)
            if not matches:
                raise ManifestError(
                    "candidate migration sequence does not match this release "
                    f"(status={status})"
                )
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise ManifestError(
                    f"candidate SQLite integrity_check failed: {integrity}"
                )
            identity = read_database_identity(conn)
            if identity is None:
                raise ManifestError("candidate database UUID is missing or invalid")
            return {
                "database_identity": identity,
                "schema_digest": sqlite_schema_digest(conn),
                "migration_sequence": list(sequence),
                "migration_digest": migration_sequence_digest(sequence),
            }
        finally:
            conn.close()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_manifest_exclusive(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            tmp_path = Path(handle.name)
            os.chmod(tmp_path, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, path)
        except FileExistsError as exc:
            raise ManifestError(f"refusing to overwrite manifest: {path}") from exc
        try:
            _fsync_directory(path.parent)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def create_manifest(
    source_db: str | Path,
    backup_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Create a consistent online backup and a current-release proof manifest."""
    source = Path(source_db).expanduser().resolve(strict=True)
    backup = Path(backup_path).expanduser().resolve(strict=False)
    manifest = Path(manifest_path).expanduser().resolve(strict=False)

    if not source.is_file():
        raise ManifestError(f"source database is not a regular file: {source}")
    if backup == source or manifest == source or manifest == backup:
        raise ManifestError("source, backup, and manifest paths must be distinct")
    if backup.parent != manifest.parent:
        raise ManifestError("backup and manifest must share a directory")
    if backup.exists() or backup.is_symlink():
        raise ManifestError(f"refusing to overwrite backup: {backup}")
    if manifest.exists() or manifest.is_symlink():
        raise ManifestError(f"refusing to overwrite manifest: {manifest}")
    if not backup.parent.is_dir():
        raise ManifestError(f"backup directory does not exist: {backup.parent}")

    created_backup = False
    try:
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        created_backup = True

        source_conn = _open_read_only(source)
        try:
            backup_conn = sqlite3.connect(backup)
            try:
                source_conn.backup(backup_conn)
            finally:
                backup_conn.close()
        finally:
            source_conn.close()

        os.chmod(backup, 0o600)
        _fsync_file(backup)
        _fsync_directory(backup.parent)

        source_snapshot = _database_state(backup)
        candidate = _candidate_state(backup)
        if candidate["database_identity"] != source_snapshot["database_identity"]:
            raise ManifestError("migrated proof copy changed the database UUID")
        payload = {
            "format": FORMAT,
            "created_at": (
                dt.datetime.now(dt.UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "source": source_snapshot,
            "backup": {
                "filename": backup.name,
                "sha256": _sha256_file(backup),
            },
            "candidate": candidate,
        }
        _write_manifest_exclusive(manifest, payload)
        return payload
    except Exception:
        if created_backup:
            backup.unlink(missing_ok=True)
        raise


def _expect_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != _HEX_DIGEST_LENGTH:
        raise ManifestError(f"{label} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ManifestError(f"{label} must be a SHA-256 digest") from exc
    return value


def _expect_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ManifestError(f"{label} must be a canonical UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ManifestError(f"{label} must be a canonical UUID")
    return canonical


def _expect_sequence(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ManifestError(f"{label} must be a migration filename list")
    return value


def verify_manifest(
    manifest_path: str | Path,
    *,
    max_age: dt.timedelta | None = None,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    """Verify manifest structure, snapshot bytes, and migrated proof state."""
    requested_manifest = Path(manifest_path).expanduser()
    if requested_manifest.is_symlink() or not requested_manifest.is_file():
        raise ManifestError(
            f"manifest must be a regular non-symlink file: {requested_manifest}"
        )
    manifest = requested_manifest.resolve(strict=True)
    try:
        payload = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid backup manifest JSON: {manifest}") from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ManifestError(f"unsupported backup manifest format: {payload.get('format')!r}")

    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        raise ManifestError("created_at must be an ISO-8601 UTC timestamp")
    try:
        parsed_timestamp = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("created_at must be an ISO-8601 UTC timestamp") from exc
    if parsed_timestamp.tzinfo is None:
        raise ManifestError("created_at must include a timezone")
    created_utc = parsed_timestamp.astimezone(dt.UTC)
    now_utc = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    if created_utc > now_utc + _MANIFEST_FUTURE_SKEW:
        raise ManifestError("created_at is unreasonably far in the future")
    if max_age is not None:
        if max_age <= dt.timedelta(0):
            raise ManifestError("manifest maximum age must be positive")
        age = now_utc - created_utc
        if age > max_age:
            raise ManifestError(
                "backup manifest is stale for release audit "
                f"(age={age}, maximum={max_age}); create a fresh backup"
            )

    source = payload.get("source")
    backup_info = payload.get("backup")
    candidate = payload.get("candidate")
    if not all(isinstance(item, dict) for item in (source, backup_info, candidate)):
        raise ManifestError("manifest source, backup, and candidate objects are required")

    source_identity = _expect_uuid(
        source.get("database_identity"), "source.database_identity"
    )
    source_schema = _expect_digest(source.get("schema_digest"), "source.schema_digest")
    source_migrations = _expect_digest(
        source.get("migration_digest"), "source.migration_digest"
    )
    source_sequence = _expect_sequence(
        source.get("migration_sequence"), "source.migration_sequence"
    )
    if migration_sequence_digest(source_sequence) != source_migrations:
        raise ManifestError("source migration sequence digest mismatch")

    candidate_schema = _expect_digest(
        candidate.get("schema_digest"), "candidate.schema_digest"
    )
    candidate_identity = _expect_uuid(
        candidate.get("database_identity"), "candidate.database_identity"
    )
    if candidate_identity != source_identity:
        raise ManifestError("candidate database UUID does not match manifest source")
    candidate_migrations = _expect_digest(
        candidate.get("migration_digest"), "candidate.migration_digest"
    )
    candidate_sequence = _expect_sequence(
        candidate.get("migration_sequence"), "candidate.migration_sequence"
    )
    if migration_sequence_digest(candidate_sequence) != candidate_migrations:
        raise ManifestError("candidate migration sequence digest mismatch")

    filename = backup_info.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or Path(filename).name != filename
    ):
        raise ManifestError("backup.filename must be one sibling filename")
    expected_sha = _expect_digest(backup_info.get("sha256"), "backup.sha256")
    backup = manifest.parent / filename
    if backup.is_symlink() or not backup.is_file():
        raise ManifestError(f"backup must be a regular non-symlink file: {backup}")
    actual_sha = _sha256_file(backup)
    if actual_sha != expected_sha:
        raise ManifestError(
            f"backup SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
        )

    actual_source = _database_state(backup)
    if actual_source["database_identity"] != source_identity:
        raise ManifestError("backup database UUID does not match manifest source")
    if actual_source["schema_digest"] != source_schema:
        raise ManifestError("backup schema digest does not match manifest source")
    if actual_source["migration_digest"] != source_migrations:
        raise ManifestError("backup migration digest does not match manifest source")
    if actual_source["migration_sequence"] != source_sequence:
        raise ManifestError("backup migration sequence does not match manifest source")

    actual_candidate = _candidate_state(backup)
    if actual_candidate["database_identity"] != candidate_identity:
        raise ManifestError("migrated proof database UUID does not match manifest candidate")
    if actual_candidate["schema_digest"] != candidate_schema:
        raise ManifestError("migrated proof schema digest does not match manifest candidate")
    if actual_candidate["migration_digest"] != candidate_migrations:
        raise ManifestError(
            "migrated proof migration digest does not match manifest candidate"
        )
    if actual_candidate["migration_sequence"] != candidate_sequence:
        raise ManifestError(
            "migrated proof migration sequence does not match manifest candidate"
        )

    return payload, backup
