"""jobs table access. Callers pass a connection; claim/finish run inside write_tx()."""
from __future__ import annotations

import json
import sqlite3

from . import repo_captures, repo_documents


def enqueue(conn: sqlite3.Connection, job_type: str, payload: dict | None = None,
            source_document_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO jobs(type, payload_json, source_document_id) VALUES (?,?,?)",
        (job_type, json.dumps(payload or {}), source_document_id),
    )
    return int(cur.lastrowid)


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically take the oldest available pending job and mark it running."""
    row = conn.execute(
        """SELECT * FROM jobs
           WHERE status='pending' AND available_at <= CURRENT_TIMESTAMP
           ORDER BY id LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status='running', attempts=attempts+1, started_at=CURRENT_TIMESTAMP WHERE id=?",
        (row["id"],),
    )
    repo_captures.record_document_event(
        conn,
        source_document_id=row["source_document_id"],
        event_kind="processing",
        stage_key=f"job_{int(row['id'])}",
        attempt_no=int(row["attempts"] or 0) + 1,
        reason_code="job_claimed",
    )
    return row


def mark_done(conn: sqlite3.Connection, job_id: int) -> None:
    row = conn.execute(
        "SELECT source_document_id, attempts FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    conn.execute("UPDATE jobs SET status='done', finished_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    if row is not None:
        repo_captures.record_document_event(
            conn,
            source_document_id=row["source_document_id"],
            event_kind="processed",
            stage_key=f"job_{job_id}",
            attempt_no=int(row["attempts"] or 0),
            reason_code="job_done",
        )


def mark_failed(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    """Retry with a short backoff until max_attempts, then park as 'dead'."""
    row = conn.execute(
        "SELECT attempts, max_attempts, source_document_id FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    dead = row is not None and row["attempts"] >= row["max_attempts"]
    conn.execute(
        """UPDATE jobs
           SET status=?, last_error=?, finished_at=CURRENT_TIMESTAMP,
               available_at=datetime(CURRENT_TIMESTAMP, '+30 seconds')
           WHERE id=?""",
        ("dead" if dead else "pending", (error or "")[:2000], job_id),
    )
    if row is not None:
        repo_captures.record_document_event(
            conn,
            source_document_id=row["source_document_id"],
            event_kind="terminal_failure" if dead else "retry",
            stage_key=f"job_{job_id}",
            attempt_no=int(row["attempts"] or 0),
            reason_code="job_dead" if dead else "job_error",
        )
        if dead and row["source_document_id"] is not None:
            repo_documents.record_extraction_failure(
                conn,
                int(row["source_document_id"]),
                reason=error or "extraction failed",
                attempts=int(row["attempts"] or 0),
            )


def recover_orphans(conn: sqlite3.Connection) -> int:
    """On boot, requeue jobs left 'running' by a crash."""
    rows = conn.execute(
        "SELECT id, source_document_id, attempts FROM jobs WHERE status='running'"
    ).fetchall()
    cur = conn.execute("UPDATE jobs SET status='pending' WHERE status='running'")
    for row in rows:
        repo_captures.record_document_event(
            conn,
            source_document_id=row["source_document_id"],
            event_kind="retry",
            stage_key=f"job_{int(row['id'])}",
            attempt_no=int(row["attempts"] or 0),
            reason_code="worker_recovered",
        )
    return cur.rowcount
