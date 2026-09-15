from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import engine, migrate, repo_captures, repo_jobs
from app.ingest.schemas import ExtractedReceipt


def _client() -> TestClient:
    from app.web.app import create_app

    return TestClient(create_app())


def test_capture_telemetry_schema_is_append_only_and_copy_safe(app_env, tmp_path):
    with engine.read_conn(app_env) as conn:
        assert {
            "capture_provenance",
            "capture_events",
            "capture_transport_consents",
        } <= {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    copied = tmp_path / "copied.sqlite"
    shutil.copy2(app_env, copied)
    assert migrate.init_db(copied) == []
    with engine.read_conn(copied) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_capture_events_and_provenance_cannot_be_rewritten(app_env, make_jpeg):
    from app.ingest.storage import capture

    result = capture(
        raw=make_jpeg(),
        original_name="synthetic.jpg",
        channel="web",
        client_capture_id=str(uuid4()),
        source_metadata={"source": "camera"},
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with engine.write_tx(app_env) as conn:
            conn.execute(
                "UPDATE capture_events SET reason_code='changed' WHERE capture_id=?",
                (result["client_capture_id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with engine.write_tx(app_env) as conn:
            conn.execute(
                "DELETE FROM capture_provenance WHERE capture_id=?",
                (result["client_capture_id"],),
            )


def test_four_channel_content_dedup_has_one_promotion_and_all_provenance(
    app_env, make_jpeg, fake_llm
):
    from app.ingest.storage import capture
    from app.workers.runner import run_worker

    raw = make_jpeg()
    origins = (
        ("web", "camera"),
        ("web", "share"),
        ("inbox", "inbox"),
        ("telegram", "telegram"),
    )
    results = [
        capture(
            raw=raw,
            original_name="synthetic.jpg",
            channel=channel,
            client_capture_id=str(uuid4()),
            source_metadata={"source": source},
        )
        for channel, source in origins
    ]
    assert len({result["source_document_id"] for result in results}) == 1
    document_id = int(results[0]["source_document_id"])

    receipt = ExtractedReceipt(
        merchant="Synthetic Cafe",
        currency="CAD",
        subtotal_cents=4850,
        total_cents=4850,
        category_guess="Restaurants",
        confidence=0.9,
    )

    async def _drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(stop, llm=fake_llm(receipt), poll_seconds=0.01)
        )
        for _ in range(300):
            await asyncio.sleep(0.01)
            with engine.read_conn(app_env) as conn:
                row = conn.execute(
                    "SELECT status FROM jobs WHERE source_document_id=?",
                    (document_id,),
                ).fetchone()
            if row is not None and row["status"] == "done":
                break
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_drive())

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='ingest_document'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source_document_id=?",
            (document_id,),
        ).fetchone()[0] == 1
        provenance = repo_captures.provenance_for_document(conn, document_id)
        assert {row["channel"] for row in provenance} == {
            "web",
            "share",
            "inbox",
            "telegram",
        }
        assert all("capture_id" not in row for row in provenance)
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM capture_events
            WHERE source_document_id=? AND event_kind='processed'
            """,
            (document_id,),
        ).fetchone()[0] == 4


def test_replay_is_one_origin_with_retry_event_and_conflict_is_rejected(
    app_env, make_jpeg
):
    from app.ingest.storage import ClientCaptureConflict, capture

    capture_id = str(uuid4())
    raw = make_jpeg()
    first = capture(
        raw=raw,
        original_name="synthetic.jpg",
        channel="web",
        client_capture_id=capture_id,
        source_metadata={"source": "camera"},
    )
    replay = capture(
        raw=raw,
        original_name="synthetic.jpg",
        channel="web",
        client_capture_id=capture_id,
        source_metadata={"source": "camera"},
    )
    assert first["replayed"] is False
    assert replay["replayed"] is True

    with pytest.raises(ClientCaptureConflict):
        capture(
            raw=make_jpeg(color=(20, 40, 60)),
            original_name="different.jpg",
            channel="web",
            client_capture_id=capture_id,
            source_metadata={"source": "camera"},
        )

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM capture_provenance").fetchone()[0] == 1
        assert conn.execute(
            """
            SELECT COUNT(*) FROM capture_events
            WHERE event_kind='deduplicated' AND reason_code='id_replay'
            """
        ).fetchone()[0] == 1


def test_metrics_are_content_free_and_include_client_ack(app_env, make_jpeg):
    marker = "PRIVATE-MERCHANT-ACCOUNT-4242"
    response = _client().post(
        "/captures",
        data={
            "client_capture_id": str(uuid4()),
            "source": "share",
            "intent": "receipt",
            "shared_text": marker,
            "accepted_at": "2026-07-26T10:00:00.000Z",
            "client_attempts": "2",
        },
        files={"file": ("private-name.jpg", make_jpeg(), "image/jpeg")},
    )
    assert response.status_code == 200
    capture_id = response.json()["client_capture_id"]
    event = _client().post(
        f"/captures/{capture_id}/client-event",
        data={
            "event": "online_durable_ack",
            "duration_ms": "9000",
            "sequence_no": "2",
        },
    )
    assert event.status_code == 200

    metrics_response = _client().get("/capture/metrics")
    assert metrics_response.status_code == 200
    metrics = metrics_response.json()
    assert metrics["accepted"] == 1
    assert metrics["durable"] == 1
    assert metrics["retry_count"] == 1
    assert metrics["online_durable_ack"]["p90_ms"] == 9000
    assert marker not in metrics_response.text
    assert "private-name.jpg" not in metrics_response.text

    with engine.read_conn(app_env) as conn:
        telemetry_dump = "\n".join(
            "|".join("" if value is None else str(value) for value in row)
            for table in ("capture_provenance", "capture_events")
            for row in conn.execute(f"SELECT * FROM {table}").fetchall()
        )
    assert marker not in telemetry_dump
    assert "private-name.jpg" not in telemetry_dump


def test_transport_consent_is_versioned_append_only_and_strict_local_wins(app_env):
    with engine.write_tx(app_env) as conn:
        assert (
            repo_captures.transport_allowed(
                conn, "telegram", strict_local_mode=True
            )
            is False
        )
        repo_captures.record_transport_decision(
            conn, transport="telegram", decision="consented"
        )
        assert (
            repo_captures.transport_allowed(
                conn, "telegram", strict_local_mode=False
            )
            is True
        )
        assert (
            repo_captures.transport_allowed(
                conn, "telegram", strict_local_mode=True
            )
            is False
        )
        repo_captures.record_transport_decision(
            conn, transport="telegram", decision="revoked"
        )
        assert (
            repo_captures.transport_allowed(
                conn, "telegram", strict_local_mode=False
            )
            is False
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with engine.write_tx(app_env) as conn:
            conn.execute(
                "UPDATE capture_transport_consents SET decision='consented'"
            )


def test_online_ack_percentile_is_scoped_and_excludes_offline_dwell(
    app_env, make_jpeg
):
    from app.ingest.storage import capture

    proof_run_id = str(uuid4())
    device_cohort_id = str(uuid4())
    capture_ids = []
    for index, online_ms in enumerate((1_000, 30_000)):
        result = capture(
            raw=make_jpeg(color=(20 + index, 40, 60)),
            original_name="synthetic.jpg",
            channel="web",
            client_capture_id=str(uuid4()),
            source_metadata={
                "source": "camera",
                "proof_run_id": proof_run_id,
                "device_cohort_id": device_cohort_id,
            },
        )
        capture_ids.append(result["client_capture_id"])
        with engine.write_tx(app_env) as conn:
            repo_captures.record_client_event(
                conn,
                capture_id=result["client_capture_id"],
                client_event="online_durable_ack",
                duration_ms=online_ms,
                sequence_no=1,
            )

    with engine.write_tx(app_env) as conn:
        repo_captures.record_client_event(
            conn,
            capture_id=capture_ids[0],
            client_event="offline_recovered",
            duration_ms=600_000,
            sequence_no=1,
        )
    outside = capture(
        raw=make_jpeg(color=(99, 98, 97)),
        original_name="outside-cohort.jpg",
        channel="web",
        client_capture_id=str(uuid4()),
        source_metadata={"source": "camera"},
    )
    with engine.write_tx(app_env) as conn:
        repo_captures.record_client_event(
            conn,
            capture_id=outside["client_capture_id"],
            client_event="online_durable_ack",
            duration_ms=7,
            sequence_no=1,
        )
    with engine.read_conn(app_env) as conn:
        scoped = repo_captures.capture_metrics(
            conn,
            proof_run_id=proof_run_id,
            device_cohort_id=device_cohort_id,
        )
        unscoped = repo_captures.capture_metrics(conn)

    # Nearest-rank p90 for two online samples is the slower sample. The
    # ten-minute offline dwell remains a separate recovery metric.
    assert scoped["online_durable_ack"] == {
        "count": 2,
        "p50_ms": 1_000,
        "p90_ms": 30_000,
        "p99_ms": 30_000,
        "max_ms": 30_000,
    }
    assert scoped["offline_recovery"]["p90_ms"] == 600_000
    assert scoped["accepted"] == 2
    assert unscoped["accepted"] == 3


def test_client_retry_events_are_lifetime_deltas_and_manual_retry_is_not_reset(
    app_env, make_jpeg
):
    capture_id = str(uuid4())
    raw = make_jpeg()
    client = _client()
    first = client.post(
        "/captures",
        data={
            "client_capture_id": capture_id,
            "source": "camera",
            "intent": "receipt",
            "client_attempts": "2",
        },
        files={"file": ("synthetic.jpg", raw, "image/jpeg")},
    )
    assert first.status_code == 200
    for lifetime_attempts in ("4", "4"):
        replay = client.post(
            "/captures",
            data={
                "client_capture_id": capture_id,
                "source": "camera",
                "intent": "receipt",
                "client_attempts": lifetime_attempts,
            },
            files={"file": ("synthetic.jpg", raw, "image/jpeg")},
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

    with engine.read_conn(app_env) as conn:
        retries = conn.execute(
            """
            SELECT attempt_no
            FROM capture_events
            WHERE capture_id=?
              AND event_kind='retry'
              AND stage_key='client_delivery'
              AND reason_code='client_retry'
            ORDER BY attempt_no
            """,
            (capture_id,),
        ).fetchall()
    assert [row["attempt_no"] for row in retries] == [1, 2, 3]

    source = (
        Path(__file__).resolve().parents[1] / "app/web/static/capture-outbox.js"
    ).read_text()
    manual_retry = source[source.index("async function manualRetry") :]
    manual_retry = manual_retry[: manual_retry.index("async function removeCapture")]
    assert "attempts: 0" not in manual_retry


def test_distinct_jobs_do_not_collapse_same_attempt_retry_events(app_env, make_jpeg):
    from app.ingest.storage import capture

    result = capture(
        raw=make_jpeg(),
        original_name="synthetic.jpg",
        channel="web",
        client_capture_id=str(uuid4()),
        source_metadata={"source": "camera"},
    )
    doc_id = int(result["source_document_id"])
    with engine.write_tx(app_env) as conn:
        first_job = int(
            conn.execute(
                """
                SELECT id FROM jobs
                WHERE source_document_id=? AND type='ingest_document'
                """,
                (doc_id,),
            ).fetchone()["id"]
        )
        conn.execute("UPDATE jobs SET attempts=1 WHERE id=?", (first_job,))
        repo_jobs.mark_failed(conn, first_job, "synthetic first failure")
        second_job = repo_jobs.enqueue(
            conn,
            "ingest_document",
            {"source_document_id": doc_id},
            source_document_id=doc_id,
        )
        conn.execute("UPDATE jobs SET attempts=1 WHERE id=?", (second_job,))
        repo_jobs.mark_failed(conn, second_job, "synthetic second failure")

    with engine.read_conn(app_env) as conn:
        rows = conn.execute(
            """
            SELECT stage_key, attempt_no
            FROM capture_events
            WHERE capture_id=? AND event_kind='retry' AND reason_code='job_error'
            ORDER BY stage_key
            """,
            (result["client_capture_id"],),
        ).fetchall()
    assert [(row["stage_key"], row["attempt_no"]) for row in rows] == [
        (f"job_{first_job}", 1),
        (f"job_{second_job}", 1),
    ]


def test_new_dedup_origin_uses_latest_reprocess_job_state(app_env, make_jpeg):
    from app.ingest.storage import capture

    raw = make_jpeg()
    first = capture(
        raw=raw,
        original_name="synthetic.jpg",
        channel="web",
        client_capture_id=str(uuid4()),
        source_metadata={"source": "camera"},
    )
    doc_id = int(first["source_document_id"])
    with engine.write_tx(app_env) as conn:
        old_job = int(
            conn.execute(
                "SELECT id FROM jobs WHERE source_document_id=? ORDER BY id",
                (doc_id,),
            ).fetchone()["id"]
        )
        conn.execute(
            "UPDATE jobs SET attempts=max_attempts WHERE id=?", (old_job,)
        )
        repo_jobs.mark_failed(conn, old_job, "synthetic terminal failure")
        new_job = repo_jobs.enqueue(
            conn,
            "ingest_document",
            {"source_document_id": doc_id},
            source_document_id=doc_id,
        )
        conn.execute("UPDATE jobs SET attempts=1 WHERE id=?", (new_job,))
        repo_jobs.mark_done(conn, new_job)

    second = capture(
        raw=raw,
        original_name="synthetic-copy.jpg",
        channel="inbox",
        client_capture_id=str(uuid4()),
        source_metadata={"source": "inbox"},
    )
    with engine.read_conn(app_env) as conn:
        state_events = conn.execute(
            """
            SELECT event_kind, stage_key
            FROM capture_events
            WHERE capture_id=?
              AND event_kind IN ('processed', 'terminal_failure')
            """,
            (second["client_capture_id"],),
        ).fetchall()
        metrics = repo_captures.capture_metrics(conn)
    assert [(row["event_kind"], row["stage_key"]) for row in state_events] == [
        ("processed", f"job_{new_job}")
    ]
    assert metrics["terminal_failure_events"] == 1
    assert metrics["terminal_failures"] == 0


def test_capture_registry_is_exhaustive_and_rejects_unknown_channel_or_source(
    app_env, make_jpeg
):
    from app.ingest.storage import capture

    with pytest.raises(ValueError, match="channel is not registered"):
        capture(
            raw=make_jpeg(),
            original_name="unknown.jpg",
            channel="carrier_pigeon",
        )
    with pytest.raises(ValueError, match="source is not registered"):
        capture(
            raw=make_jpeg(color=(2, 3, 4)),
            original_name="unknown-source.jpg",
            channel="web",
            source_metadata={"source": "carrier_pigeon"},
        )

    api = capture(
        raw=make_jpeg(color=(5, 6, 7)),
        original_name="api.jpg",
        channel="api",
    )
    mcp = capture(
        raw=make_jpeg(color=(8, 9, 10)),
        original_name="mcp.jpg",
        channel="mcp",
    )
    with engine.read_conn(app_env) as conn:
        classes = {
            row["capture_id"]: row["transport_class"]
            for row in conn.execute(
                "SELECT capture_id, transport_class FROM capture_provenance"
            )
        }
        registry = repo_captures.transport_settings(
            conn, strict_local_mode=True
        )
    assert classes[api["client_capture_id"]] == "direct_network"
    assert classes[mcp["client_capture_id"]] == "local_only"
    assert {item["channel"] for item in registry} == {
        "web",
        "share",
        "inbox",
        "api",
        "mcp",
        "telegram",
    }
