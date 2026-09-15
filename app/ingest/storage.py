"""Content-addressed capture: the single entrypoint every channel calls.

Files are stored by sha256 under DATA_DIR/originals/blobs/ab/cd/<sha>.<ext>. Re-uploading
the same bytes is a no-op (dedup). A successful capture enqueues an ingest_document job.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import filetype

from ..config import get_settings
from ..db import engine, repo_captures, repo_documents, repo_jobs
from .extract.pdf import pdf_doc_kind


class InvalidClientCaptureId(ValueError):
    """The caller did not supply a canonical UUID capture id."""


class ClientCaptureConflict(ValueError):
    """One client capture id was reused for different original bytes."""


class BlobDurabilityError(RuntimeError):
    """The database points at an original that cannot be verified or repaired."""


def blob_abspath(storage_ref: str) -> Path:
    return Path(get_settings().data_dir) / storage_ref


def _sniff(raw: bytes, original_name: str, declared_mime: str | None) -> tuple[str, str, str]:
    """Return (doc_kind, mime, ext) from magic bytes, falling back to the filename."""
    kind = filetype.guess(raw)
    if kind is not None:
        mime, ext = kind.mime, "." + kind.extension
    else:
        mime = declared_mime or "application/octet-stream"
        ext = Path(original_name).suffix or ".bin"

    if mime == "application/pdf":
        doc_kind = pdf_doc_kind(raw)   # 1-page non-statement PDF => receipt; else statement (M3)
    elif mime.startswith("image/"):
        doc_kind = "receipt"
    else:
        doc_kind = "upload"
    return doc_kind, mime, ext


def normalize_client_capture_id(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError) as exc:
        raise InvalidClientCaptureId("client_capture_id must be a UUID") from exc
    canonical = str(parsed)
    if candidate.lower() != canonical:
        raise InvalidClientCaptureId("client_capture_id must be a canonical UUID")
    return canonical


def _job_id_for_document(conn, source_document_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM jobs
        WHERE source_document_id=? AND type='ingest_document'
        ORDER BY id
        LIMIT 1
        """,
        (source_document_id,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _claim_deterministic_statement_source(
    conn: sqlite3.Connection,
    *,
    source_document_id: int,
    existing_kind: str,
    forced_kind: str | None,
    enqueue_ingest: bool,
) -> None:
    """Fail closed if generic ingest has ever owned deterministic-import bytes."""
    if forced_kind is None:
        return
    if not enqueue_ingest and forced_kind == "statement":
        conflicting = conn.execute(
            """SELECT status FROM jobs
               WHERE source_document_id=? AND type='ingest_document'
               ORDER BY id LIMIT 1""",
            (int(source_document_id),),
        ).fetchone()
        if conflicting is not None:
            raise ClientCaptureConflict(
                "captured bytes already have generic ingest work; "
                "use a new deterministic statement source"
            )
    if existing_kind == forced_kind:
        return
    if existing_kind != "upload":
        raise ClientCaptureConflict(
            "captured bytes already belong to a different document kind"
        )
    repo_documents.set_kind(conn, source_document_id, forced_kind)


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _blob_matches(path: Path, expected_sha256: str) -> bool:
    try:
        return path.is_file() and _file_sha256(path) == expected_sha256
    except OSError:
        return False


def _repair_blob_atomically(path: Path, raw: bytes, expected_sha256: str) -> None:
    """Replace one missing/corrupt blob without exposing partial retry bytes."""
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if _file_sha256(tmp) != expected_sha256:
            raise BlobDurabilityError("temporary capture blob failed verification")
        tmp.replace(path)
        if not _blob_matches(path, expected_sha256):
            raise BlobDurabilityError("repaired capture blob failed verification")
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, BlobDurabilityError) as exc:
        raise BlobDurabilityError("capture blob could not be repaired") from exc
    finally:
        tmp.unlink(missing_ok=True)


def _ensure_blob(path: Path, raw: bytes, expected_sha256: str) -> bool:
    if _blob_matches(path, expected_sha256):
        return False
    _repair_blob_atomically(path, raw, expected_sha256)
    return True


def _existing_result(
    conn,
    *,
    source_document_id: int,
    sha256: str,
    raw: bytes,
    client_capture_id: str | None,
    include_existing_job: bool,
    replayed: bool,
) -> dict:
    doc = repo_documents.get_document(conn, source_document_id)
    if doc is None or doc["sha256"] != sha256:
        raise BlobDurabilityError("capture document does not match submitted bytes")
    _ensure_blob(blob_abspath(doc["storage_ref"]), raw, sha256)
    return {
        "status": "duplicate",
        "source_document_id": source_document_id,
        "job_id": (
            _job_id_for_document(conn, source_document_id)
            if include_existing_job
            else None
        ),
        "sha256": sha256,
        "kind": doc["kind"],
        "client_capture_id": client_capture_id,
        "durable": True,
        "replayed": replayed,
    }


def capture(*, raw: bytes, original_name: str, channel: str,
            declared_mime: str | None = None,
            client_capture_id: str | None = None,
            source_metadata: dict | None = None,
            enqueue_ingest: bool = True,
            forced_kind: str | None = None,
            import_run_id: int | None = None) -> dict:
    settings = get_settings()
    if forced_kind not in {None, "receipt", "statement", "upload"}:
        raise ValueError("forced capture kind is unsupported")
    # Channels with a native replay key supply it. Legacy/API callers still
    # receive an opaque origin id so every accepted occurrence has provenance.
    supplied_client_capture_id = normalize_client_capture_id(client_capture_id)
    client_capture_id = supplied_client_capture_id or str(uuid4())
    sha = hashlib.sha256(raw).hexdigest()
    doc_kind, mime, ext = _sniff(raw, original_name, declared_mime)
    if forced_kind is not None:
        doc_kind = forced_kind

    rel = f"originals/blobs/{sha[:2]}/{sha[2:4]}/{sha}{ext}"
    dest = blob_abspath(rel)
    created_blob = False
    doc_id: int | None = None
    job_id: int | None = None
    try:
        # SQLite serializes the normal one-process service through write_tx.
        # Retry once after a unique-constraint race so a second process (for
        # example during an accidental overlap) resolves to the winning durable
        # row instead of turning a harmless identical upload into HTTP 500.
        for race_attempt in range(2):
            try:
                with engine.write_tx(settings.db_path) as conn:
                    if client_capture_id is not None:
                        submission = repo_captures.get_submission(conn, client_capture_id)
                        if submission is not None:
                            if submission["sha256"] != sha:
                                raise ClientCaptureConflict(
                                    "client_capture_id already belongs to different content"
                                )
                            if submission["source_document_id"] is None:
                                raise ClientCaptureConflict(
                                    "client_capture_id belongs to a removed capture"
                                )
                            submission_doc = repo_documents.get_document(
                                conn, int(submission["source_document_id"])
                            )
                            if submission_doc is None:
                                raise BlobDurabilityError(
                                    "capture document is missing"
                                )
                            _claim_deterministic_statement_source(
                                conn,
                                source_document_id=int(
                                    submission["source_document_id"]
                                ),
                                existing_kind=str(submission_doc["kind"]),
                                forced_kind=forced_kind,
                                enqueue_ingest=enqueue_ingest,
                            )
                            result = _existing_result(
                                conn,
                                source_document_id=int(submission["source_document_id"]),
                                sha256=sha,
                                raw=raw,
                                client_capture_id=client_capture_id,
                                include_existing_job=(
                                    supplied_client_capture_id is not None
                                ),
                                replayed=True,
                            )
                            repo_captures.record_replay(
                                conn,
                                capture_id=client_capture_id,
                                source_document_id=int(submission["source_document_id"]),
                            )
                            if (source_metadata or {}).get("client_attempts") is not None:
                                repo_captures.record_client_attempts(
                                    conn,
                                    capture_id=client_capture_id,
                                    source_document_id=int(
                                        submission["source_document_id"]
                                    ),
                                    lifetime_attempts=int(
                                        source_metadata["client_attempts"]
                                    ),
                                )
                            return result

                    existing = repo_documents.find_by_sha(conn, sha)
                    if existing is not None:
                        source_document_id = int(existing["id"])
                        _claim_deterministic_statement_source(
                            conn,
                            source_document_id=source_document_id,
                            existing_kind=str(existing["kind"]),
                            forced_kind=forced_kind,
                            enqueue_ingest=enqueue_ingest,
                        )
                        if client_capture_id is not None:
                            repo_captures.insert_submission(
                                conn,
                                client_capture_id=client_capture_id,
                                source_document_id=source_document_id,
                                sha256=sha,
                                channel=channel,
                                source_metadata=source_metadata,
                                dedup_kind="content",
                            )
                        return _existing_result(
                            conn,
                            source_document_id=source_document_id,
                            sha256=sha,
                            raw=raw,
                            client_capture_id=client_capture_id,
                            include_existing_job=(
                                supplied_client_capture_id is not None
                            ),
                            replayed=False,
                        )

                    created_blob = _ensure_blob(dest, raw, sha) or created_blob

                    metadata = {"channel": channel}
                    if client_capture_id is not None:
                        metadata["client_capture_id"] = client_capture_id
                    if source_metadata:
                        metadata["capture"] = source_metadata
                    doc_id = repo_documents.insert_source_document(
                        conn, kind=doc_kind, original_name=original_name, storage_ref=rel,
                        sha256=sha, mime_type=mime, status="staged",
                        metadata=metadata,
                    )
                    if import_run_id is not None:
                        conn.execute(
                            "UPDATE source_documents SET import_run_id=? WHERE id=?",
                            (int(import_run_id), doc_id),
                        )
                    if enqueue_ingest:
                        job_id = repo_jobs.enqueue(
                            conn,
                            "ingest_document",
                            {"source_document_id": doc_id},
                            source_document_id=doc_id,
                        )
                    if client_capture_id is not None:
                        repo_captures.insert_submission(
                            conn,
                            client_capture_id=client_capture_id,
                            source_document_id=doc_id,
                            sha256=sha,
                            channel=channel,
                            source_metadata=source_metadata,
                            dedup_kind="new",
                        )
                break
            except sqlite3.IntegrityError:
                if race_attempt == 0:
                    continue
                raise
    except Exception:
        if created_blob and dest.exists():
            with engine.read_conn(settings.db_path) as conn:
                referenced = conn.execute(
                    "SELECT 1 FROM source_documents WHERE storage_ref=?",
                    (rel,),
                ).fetchone()
            if referenced is None:
                dest.unlink(missing_ok=True)
        raise

    if doc_id is None or (enqueue_ingest and job_id is None):
        # pragma: no cover - loop exhausts by raise
        raise RuntimeError("capture transaction did not produce a durable document")
    return {"status": "staged", "source_document_id": doc_id, "job_id": job_id,
            "sha256": sha, "kind": doc_kind,
            "client_capture_id": client_capture_id, "durable": True, "replayed": False}
