"""source_documents + ingest_extractions access."""
from __future__ import annotations

import json
import sqlite3


def find_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_documents WHERE sha256=?", (sha256,)).fetchone()


def get_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM source_documents WHERE id=?", (doc_id,)).fetchone()


def insert_source_document(conn: sqlite3.Connection, *, kind: str, original_name: str,
                           storage_ref: str, sha256: str, mime_type: str,
                           status: str = "staged", metadata: dict | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO source_documents(kind, original_name, storage_ref, sha256, mime_type, status, metadata_json)
           VALUES (?,?,?,?,?,?,?)""",
        (kind, original_name, storage_ref, sha256, mime_type, status, json.dumps(metadata or {})),
    )
    return int(cur.lastrowid)


def set_status(conn: sqlite3.Connection, doc_id: int, status: str) -> None:
    conn.execute("UPDATE source_documents SET status=? WHERE id=?", (status, doc_id))


def set_kind(conn: sqlite3.Connection, doc_id: int, kind: str) -> None:
    conn.execute("UPDATE source_documents SET kind=? WHERE id=?", (kind, doc_id))


def record_triage(conn: sqlite3.Connection, doc_id: int, *, from_kind: str,
                  to_kind: str, confidence: float) -> None:
    """Persist a triage reclassification note into the document's metadata_json.

    Merged (not overwritten) so the existing channel/etc. keys survive. Only called
    when triage actually overrode the sniffed kind, so its presence *is* the signal.
    """
    row = conn.execute(
        "SELECT metadata_json FROM source_documents WHERE id=?", (doc_id,)
    ).fetchone()
    meta = json.loads(row["metadata_json"]) if row and row["metadata_json"] else {}
    meta["triage"] = {"from": from_kind, "to": to_kind, "confidence": float(confidence)}
    conn.execute(
        "UPDATE source_documents SET metadata_json=? WHERE id=?", (json.dumps(meta), doc_id)
    )


def record_extraction_failure(
    conn: sqlite3.Connection, doc_id: int, *, reason: str, attempts: int
) -> None:
    """Park a document whose extraction can no longer be retried.

    Without this the document stays 'staged' forever after its job dies, and
    the upload view polls it indefinitely while showing no error -- the user
    waits on work that has already stopped.

    'needs_review' is reused rather than adding a status value, because a
    document that could not be read genuinely does need a human. The reason is
    recorded separately so the UI can say what happened instead of implying the
    extraction merely came back uncertain.
    """
    row = conn.execute(
        "SELECT metadata_json FROM source_documents WHERE id=?", (doc_id,)
    ).fetchone()
    if row is None:
        return
    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    meta["extraction_failure"] = {"reason": str(reason)[:500], "attempts": int(attempts)}
    conn.execute(
        """UPDATE source_documents
           SET metadata_json=?, status='needs_review'
           WHERE id=? AND status='staged'""",
        (json.dumps(meta), doc_id),
    )


def clear_extraction_failure(conn: sqlite3.Connection, doc_id: int) -> None:
    """Drop the failure note and return the document to the queue for a retry."""
    row = conn.execute(
        "SELECT metadata_json FROM source_documents WHERE id=?", (doc_id,)
    ).fetchone()
    if row is None:
        return
    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    meta.pop("extraction_failure", None)
    conn.execute(
        "UPDATE source_documents SET metadata_json=?, status='staged' WHERE id=?",
        (json.dumps(meta), doc_id),
    )


def extraction_failure(doc: sqlite3.Row) -> dict | None:
    """Return the {'reason','attempts'} failure note for a document, or None."""
    try:
        meta = json.loads(doc["metadata_json"] or "{}")
    except (ValueError, TypeError, IndexError):
        return None
    note = meta.get("extraction_failure")
    return note if isinstance(note, dict) else None


def triage_note(doc: sqlite3.Row) -> dict | None:
    """Return the {'from','to','confidence'} triage note for a document, or None."""
    try:
        meta = json.loads(doc["metadata_json"] or "{}")
    except (ValueError, TypeError, IndexError):
        return None
    note = meta.get("triage")
    return note if isinstance(note, dict) else None


def insert_extraction(conn: sqlite3.Connection, *, source_document_id: int, doc_kind: str,
                      extracted_json: str, confidence: float, external_id: str,
                      proposed_account_id: int | None, proposed_category_id: int | None,
                      review_status: str) -> int:
    cur = conn.execute(
        """INSERT INTO ingest_extractions(
             source_document_id, doc_kind, extracted_json, confidence, external_id,
             proposed_account_id, proposed_category_id, review_status)
           VALUES (?,?,?,?,?,?,?,?)""",
        (source_document_id, doc_kind, extracted_json, confidence, external_id,
         proposed_account_id, proposed_category_id, review_status),
    )
    return int(cur.lastrowid)


def link_extraction_txn(conn: sqlite3.Connection, extraction_id: int, txn_id: int,
                        review_status: str = "auto") -> None:
    conn.execute(
        "UPDATE ingest_extractions SET transaction_id=?, review_status=? WHERE id=?",
        (txn_id, review_status, extraction_id),
    )
