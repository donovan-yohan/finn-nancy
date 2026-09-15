from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db import engine
from app.ingest.schemas import ExtractedReceipt


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    # Lifespan stays off so the background worker cannot race assertions.
    from app.web.app import create_app

    return TestClient(
        create_app(),
        raise_server_exceptions=raise_server_exceptions,
    )


def _post_capture(
    client: TestClient,
    *,
    capture_id: str,
    raw: bytes,
    filename: str = "receipt.jpg",
):
    return client.post(
        "/captures",
        data={
            "client_capture_id": capture_id,
            "source": "camera",
            "intent": "receipt",
        },
        files={"file": (filename, raw, "image/jpeg")},
    )


def _capture_blob_path(db_path: str, source_document_id: int):
    from app.ingest.storage import blob_abspath

    with engine.read_conn(db_path) as conn:
        row = conn.execute(
            "SELECT storage_ref FROM source_documents WHERE id=?",
            (source_document_id,),
        ).fetchone()
    assert row is not None
    return blob_abspath(row["storage_ref"])


def test_timeout_after_commit_retry_has_one_document_job_and_ledger_effect(
    app_env, make_jpeg, fake_llm
):
    """A lost HTTP response can be retried after processing without duplicating effects."""
    from app.ingest.pipeline import process_document

    client = _client()
    capture_id = str(uuid4())
    raw = make_jpeg()

    first = _post_capture(client, capture_id=capture_id, raw=raw)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["durable"] is True
    assert first_body["device_state"] == "saved"
    assert first_body["server_state"] == "queued"
    assert first_body["replayed"] is False

    receipt = ExtractedReceipt(
        merchant="Retry Cafe",
        purchased_on="2026-06-03",
        currency="CAD",
        subtotal_cents=1250,
        total_cents=1250,
        category_guess="Restaurants",
        confidence=0.95,
    )
    result = process_document(
        app_env,
        first_body["source_document_id"],
        fake_llm(receipt),
    )
    assert result["status"] == "inserted"

    # Simulate a client that never received the first response and retries the
    # exact same outbox record after the worker already filed it.
    replay = _post_capture(client, capture_id=capture_id, raw=raw)
    assert replay.status_code == 200
    replay_body = replay.json()
    assert replay_body["replayed"] is True
    assert replay_body["source_document_id"] == first_body["source_document_id"]
    assert replay_body["server_state"] == "logged"

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM source_documents WHERE sha256=?",
            (first_body["sha256"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='ingest_document'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source_document_id=?",
            (first_body["source_document_id"],),
        ).fetchone()[0] == 1


def test_stable_id_replay_repairs_missing_referenced_blob(app_env, make_jpeg):
    client = _client()
    capture_id = str(uuid4())
    raw = make_jpeg()
    first = _post_capture(client, capture_id=capture_id, raw=raw).json()
    blob_path = _capture_blob_path(app_env, first["source_document_id"])
    blob_path.unlink()

    replay = _post_capture(client, capture_id=capture_id, raw=raw)

    assert replay.status_code == 200
    assert replay.json()["durable"] is True
    assert replay.json()["replayed"] is True
    assert blob_path.read_bytes() == raw
    assert hashlib.sha256(blob_path.read_bytes()).hexdigest() == first["sha256"]


def test_stable_id_replay_repairs_corrupt_referenced_blob(app_env, make_jpeg):
    client = _client()
    capture_id = str(uuid4())
    raw = make_jpeg()
    first = _post_capture(client, capture_id=capture_id, raw=raw).json()
    blob_path = _capture_blob_path(app_env, first["source_document_id"])
    blob_path.write_bytes(b"corrupt")

    replay = _post_capture(client, capture_id=capture_id, raw=raw)

    assert replay.status_code == 200
    assert replay.json()["durable"] is True
    assert replay.json()["replayed"] is True
    assert blob_path.read_bytes() == raw


def test_content_dedup_repairs_existing_storage_ref_despite_new_extension(app_env):
    client = _client()
    raw = b"same opaque receipt bytes"
    first = _post_capture(
        client,
        capture_id=str(uuid4()),
        raw=raw,
        filename="receipt.original",
    ).json()
    blob_path = _capture_blob_path(app_env, first["source_document_id"])
    assert blob_path.suffix == ".original"
    blob_path.write_bytes(b"corrupt")
    alternate_path = blob_path.with_suffix(".retry")

    second = _post_capture(
        client,
        capture_id=str(uuid4()),
        raw=raw,
        filename="receipt.retry",
    )

    assert second.status_code == 200
    assert second.json()["durable"] is True
    assert second.json()["source_document_id"] == first["source_document_id"]
    assert blob_path.read_bytes() == raw
    assert not alternate_path.exists()
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1


def test_failed_content_dedup_repair_returns_no_ack_and_rolls_back_submission(
    app_env, make_jpeg, monkeypatch
):
    from app.ingest import storage

    client = _client(raise_server_exceptions=False)
    raw = make_jpeg()
    first_id = str(uuid4())
    first = _post_capture(client, capture_id=first_id, raw=raw).json()
    blob_path = _capture_blob_path(app_env, first["source_document_id"])
    blob_path.unlink()

    def fail_repair(*_args, **_kwargs):
        raise storage.BlobDurabilityError("synthetic repair failure")

    monkeypatch.setattr(storage, "_repair_blob_atomically", fail_repair)
    second_id = str(uuid4())
    failed = _post_capture(client, capture_id=second_id, raw=raw)

    assert failed.status_code == 503
    assert failed.json()["detail"] == "capture original is not durably stored"
    assert not blob_path.exists()
    assert client.get(f"/captures/{second_id}").status_code == 404
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 1


def test_capture_id_cannot_be_reused_for_different_bytes(
    app_env, make_jpeg
):
    client = _client()
    capture_id = str(uuid4())

    first = _post_capture(
        client,
        capture_id=capture_id,
        raw=make_jpeg(color=(20, 40, 60)),
    )
    assert first.status_code == 200

    conflict = _post_capture(
        client,
        capture_id=capture_id,
        raw=make_jpeg(color=(60, 40, 20)),
        filename="different.jpg",
    )
    assert conflict.status_code == 409
    assert "different content" in conflict.json()["detail"]

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='ingest_document'"
        ).fetchone()[0] == 1


def test_content_duplicate_with_two_capture_ids_retains_both_acknowledgements(
    app_env, make_jpeg
):
    client = _client()
    raw = make_jpeg()
    first = _post_capture(client, capture_id=str(uuid4()), raw=raw).json()
    second = _post_capture(client, capture_id=str(uuid4()), raw=raw).json()

    assert second["source_document_id"] == first["source_document_id"]
    assert second["durable"] is True
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='ingest_document'"
        ).fetchone()[0] == 1


def test_concurrent_same_id_retries_share_one_commit(app_env, make_jpeg):
    from app.ingest.storage import capture

    capture_id = str(uuid4())
    raw = make_jpeg()

    def submit():
        return capture(
            raw=raw,
            original_name="concurrent.jpg",
            channel="web",
            declared_mime="image/jpeg",
            client_capture_id=capture_id,
            source_metadata={"source": "camera", "intent": "receipt"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))

    assert {result["source_document_id"] for result in results} == {
        results[0]["source_document_id"]
    }
    assert sorted(result["replayed"] for result in results) == [False, True]
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='ingest_document'"
        ).fetchone()[0] == 1


def test_capture_retries_one_unique_constraint_race(
    app_env, make_jpeg, monkeypatch
):
    from app.db import repo_documents
    from app.ingest.storage import capture

    original_insert = repo_documents.insert_source_document
    attempts = 0

    def insert_with_one_race(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.IntegrityError("synthetic concurrent winner")
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(
        repo_documents,
        "insert_source_document",
        insert_with_one_race,
    )
    result = capture(
        raw=make_jpeg(),
        original_name="race.jpg",
        channel="web",
        declared_mime="image/jpeg",
        client_capture_id=str(uuid4()),
    )

    assert attempts == 2
    assert result["durable"] is True
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_submissions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='ingest_document'"
        ).fetchone()[0] == 1


def test_deleted_capture_id_is_a_tombstone_not_a_resurrection(
    app_env, make_jpeg
):
    client = _client()
    capture_id = str(uuid4())
    raw = make_jpeg()
    first = _post_capture(client, capture_id=capture_id, raw=raw).json()

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "DELETE FROM source_documents WHERE id=?",
            (first["source_document_id"],),
        )

    replay = _post_capture(client, capture_id=capture_id, raw=raw)
    assert replay.status_code == 409
    assert "removed capture" in replay.json()["detail"]
    with engine.read_conn(app_env) as conn:
        row = conn.execute(
            "SELECT source_document_id FROM capture_submissions WHERE client_capture_id=?",
            (capture_id,),
        ).fetchone()
        assert row is not None and row["source_document_id"] is None


def test_capture_rejects_non_uuid_id(app_env, make_jpeg):
    response = _post_capture(
        _client(),
        capture_id="not-a-uuid",
        raw=make_jpeg(),
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "client_capture_id must be a UUID"


def test_capture_status_maps_worker_and_document_states(app_env, make_jpeg):
    client = _client()
    capture_id = str(uuid4())
    body = _post_capture(client, capture_id=capture_id, raw=make_jpeg()).json()
    doc_id = body["source_document_id"]

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE jobs SET status='running' WHERE source_document_id=?",
            (doc_id,),
        )
    assert client.get(f"/captures/{capture_id}").json()["server_state"] == "processing"

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE source_documents SET status='needs_review' WHERE id=?",
            (doc_id,),
        )
    assert client.get(f"/captures/{capture_id}").json()["server_state"] == "needs_review"

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE source_documents SET status='processed' WHERE id=?",
            (doc_id,),
        )
        conn.execute(
            "UPDATE jobs SET status='done' WHERE source_document_id=?",
            (doc_id,),
        )
    # A processed flag without the actual receipt ledger effect must never
    # become false terminal Filed/Logged success.
    assert client.get(f"/captures/{capture_id}").json()["server_state"] == "failed"


def test_processed_statement_is_not_logged_before_reconciliation(
    app_env, make_jpeg
):
    from app.db import repo_jobs

    client = _client()
    capture_id = str(uuid4())
    body = _post_capture(client, capture_id=capture_id, raw=make_jpeg()).json()
    doc_id = body["source_document_id"]

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE source_documents SET kind='statement', status='processed' WHERE id=?",
            (doc_id,),
        )
        conn.execute(
            "UPDATE jobs SET status='done' WHERE source_document_id=?",
            (doc_id,),
        )
        repo_jobs.enqueue(
            conn,
            "reconcile_document",
            {"source_document_id": doc_id},
            source_document_id=doc_id,
        )
    assert client.get(f"/captures/{capture_id}").json()["server_state"] == "processing"

    with engine.write_tx(app_env) as conn:
        conn.execute(
            "UPDATE jobs SET status='done' WHERE source_document_id=?",
            (doc_id,),
        )
    assert client.get(f"/captures/{capture_id}").json()["server_state"] == "needs_review"
