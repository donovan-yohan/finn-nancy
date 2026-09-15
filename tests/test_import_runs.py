"""Batch import runs.

A run is what makes "drop five statements and come back later" possible: a
durable URL, one poller, and a rollup that leads with what still needs a
person rather than what already succeeded.
"""
from __future__ import annotations

from itertools import count

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import engine, repo_documents, repo_import_runs, repo_jobs


def _client(db_path: str, data_dir: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATA_DIR", data_dir)
    get_settings.cache_clear()
    from app.web.app import create_app

    return TestClient(create_app())


_unique = count(1)


def _run_with(db_path: str, statuses: list[str]) -> tuple[int, list[int]]:
    ids = []
    with engine.write_tx(db_path) as conn:
        run_id = repo_import_runs.create(conn)
        for status in statuses:
            index = next(_unique)
            doc_id = repo_documents.insert_source_document(
                conn, kind="statement", original_name=f"s{index}.pdf",
                storage_ref=f"blobs/s{index}.pdf", sha256=f"{index:064d}",
                mime_type="application/pdf", status="staged", metadata={},
            )
            conn.execute(
                "UPDATE source_documents SET import_run_id=?, status=? WHERE id=?",
                (run_id, status, doc_id),
            )
            ids.append(doc_id)
    return run_id, ids


def test_rollup_reports_partial_failure_as_the_normal_case(empty_db):
    run_id, _ = _run_with(empty_db, ["processed", "processed", "processed",
                                     "needs_review", "needs_review"])
    with engine.read_conn(empty_db) as conn:
        rollup = repo_import_runs.rollup(conn, run_id)

    assert rollup["total"] == 5
    assert rollup["settled"] == 3
    assert rollup["needs_you"] == 2
    assert rollup["working"] == 0
    # The obstacle outranks the reward.
    assert rollup["state"] == "needs_you"


def test_a_run_still_working_outranks_its_attention_items(empty_db):
    run_id, _ = _run_with(empty_db, ["staged", "needs_review"])
    with engine.read_conn(empty_db) as conn:
        assert repo_import_runs.rollup(conn, run_id)["state"] == "working"


def test_polling_stops_once_nothing_is_in_flight(empty_db):
    run_id, _ = _run_with(empty_db, ["processed", "needs_review"])
    with engine.read_conn(empty_db) as conn:
        rollup = repo_import_runs.rollup(conn, run_id)
    assert rollup["poll_seconds"] == 0

    working_id, _ = _run_with(empty_db, ["staged"])
    with engine.read_conn(empty_db) as conn:
        assert repo_import_runs.rollup(conn, working_id)["poll_seconds"] > 0


def test_terminal_run_page_carries_no_poll_trigger(empty_db, tmp_path, monkeypatch):
    run_id, _ = _run_with(empty_db, ["processed", "needs_review"])
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)

    body = client.get(f"/imports/runs/{run_id}").text
    assert "hx-trigger" not in body
    assert "1</strong> need you" in body


def test_working_run_page_polls_the_run_not_each_document(
    empty_db, tmp_path, monkeypatch
):
    run_id, ids = _run_with(empty_db, ["staged", "staged"])
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)

    body = client.get(f"/imports/runs/{run_id}").text
    assert f"/imports/runs/{run_id}/status" in body
    assert body.count("hx-trigger") == 1
    for doc_id in ids:
        assert f"/documents/{doc_id}/status" not in body


def test_failed_document_is_explained_and_retryable_from_the_run(
    empty_db, tmp_path, monkeypatch
):
    run_id, ids = _run_with(empty_db, ["staged"])
    with engine.write_tx(empty_db) as conn:
        job_id = repo_jobs.enqueue(
            conn, "ingest_document", {"source_document_id": ids[0]},
            source_document_id=ids[0],
        )
        conn.execute("UPDATE jobs SET attempts=max_attempts WHERE id=?", (job_id,))
        repo_jobs.mark_failed(conn, job_id, "unreadable pdf")

    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    body = client.get(f"/imports/runs/{run_id}").text
    assert "Couldn't read this file" in body
    assert "Nothing from it has been recorded" in body
    assert f"/documents/{ids[0]}/retry" in body


def test_upload_creates_a_run_the_user_can_return_to(empty_db, tmp_path, monkeypatch):
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    response = client.post(
        "/upload",
        files=[
            ("files", ("a.txt", b"hello world alpha", "text/plain")),
            ("files", ("b.txt", b"hello world beta", "text/plain")),
        ],
    )
    assert response.status_code == 200
    assert "/imports/runs/" in response.text

    with engine.read_conn(empty_db) as conn:
        runs = repo_import_runs.open_runs(conn)
    assert runs and runs[0]["total"] == 2


def test_missing_run_is_not_found(empty_db, tmp_path, monkeypatch):
    client = _client(empty_db, str(tmp_path / "data"), monkeypatch)
    assert client.get("/imports/runs/4242").status_code == 404
