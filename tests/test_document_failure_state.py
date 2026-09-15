"""A document whose extraction dies must reach a terminal, visible state.

Without this the document stays 'staged', the upload view polls it every two
seconds forever, and the user waits on work that already stopped.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import engine, repo_documents, repo_jobs


def _client(db_path: str, data_dir: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATA_DIR", data_dir)
    get_settings.cache_clear()
    from app.web.app import create_app

    return TestClient(create_app())


def _document(db_path: str) -> int:
    with engine.write_tx(db_path) as conn:
        return repo_documents.insert_source_document(
            conn, kind="receipt", original_name="r.jpg", storage_ref="blobs/r.jpg",
            sha256="a" * 64, mime_type="image/jpeg", status="staged", metadata={},
        )


def _kill_job(db_path: str, doc_id: int) -> None:
    with engine.write_tx(db_path) as conn:
        job_id = repo_jobs.enqueue(
            conn, "ingest_document", {"source_document_id": doc_id},
            source_document_id=doc_id,
        )
        conn.execute(
            "UPDATE jobs SET attempts=max_attempts WHERE id=?", (job_id,)
        )
        repo_jobs.mark_failed(conn, job_id, "vision endpoint unreachable")


def test_a_dead_job_parks_its_document_with_a_reason(empty_db):
    doc_id = _document(empty_db)
    _kill_job(empty_db, doc_id)

    with engine.read_conn(empty_db) as conn:
        doc = conn.execute(
            "SELECT * FROM source_documents WHERE id=?", (doc_id,)
        ).fetchone()
    assert doc["status"] != "staged"
    failure = repo_documents.extraction_failure(doc)
    assert failure is not None
    assert "unreachable" in failure["reason"]


def test_a_retrying_job_does_not_park_the_document(empty_db):
    doc_id = _document(empty_db)
    with engine.write_tx(empty_db) as conn:
        job_id = repo_jobs.enqueue(
            conn, "ingest_document", {"source_document_id": doc_id},
            source_document_id=doc_id,
        )
        repo_jobs.mark_failed(conn, job_id, "transient")

    with engine.read_conn(empty_db) as conn:
        doc = conn.execute(
            "SELECT * FROM source_documents WHERE id=?", (doc_id,)
        ).fetchone()
    assert doc["status"] == "staged"
    assert repo_documents.extraction_failure(doc) is None


def test_failed_document_stops_polling_and_offers_a_retry(
    empty_db, tmp_path, monkeypatch
):
    doc_id = _document(empty_db)
    _kill_job(empty_db, doc_id)
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)

    body = client.get(f"/documents/{doc_id}/status").text
    assert "hx-trigger" not in body
    assert "couldn't read this file" in body
    assert f"/documents/{doc_id}/retry" in body


def test_retry_requeues_the_document(empty_db, tmp_path, monkeypatch):
    doc_id = _document(empty_db)
    _kill_job(empty_db, doc_id)
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)

    response = client.post(f"/documents/{doc_id}/retry")
    assert response.status_code == 200
    assert "hx-trigger" in response.text

    with engine.read_conn(empty_db) as conn:
        doc = conn.execute(
            "SELECT * FROM source_documents WHERE id=?", (doc_id,)
        ).fetchone()
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE source_document_id=? AND status='pending'",
            (doc_id,),
        ).fetchone()
    assert doc["status"] == "staged"
    assert repo_documents.extraction_failure(doc) is None
    assert queued["n"] == 1


def test_retry_rejects_a_document_that_has_not_failed(empty_db, tmp_path, monkeypatch):
    doc_id = _document(empty_db)
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    assert client.post(f"/documents/{doc_id}/retry").status_code == 409
