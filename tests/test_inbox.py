from __future__ import annotations

import hashlib
from concurrent.futures import Future
from pathlib import Path

from app.db import engine


def test_ingest_path_captures_and_moves_to_processed(app_env, tmp_path, make_jpeg):
    from app.channels.inbox import ingest_path

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    dropped = inbox / "receipt.jpg"
    dropped.write_bytes(make_jpeg())

    result = ingest_path(dropped, inbox_dir=inbox, stable_interval=0)
    assert result["status"] == "staged"
    moved_to = Path(result["moved_to"])
    assert moved_to.parent == inbox / "processed"
    assert moved_to.exists()
    assert not dropped.exists()

    with engine.read_conn(app_env) as conn:
        doc = conn.execute("SELECT * FROM source_documents").fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE type='ingest_document'").fetchone()
    assert doc is not None
    assert doc["original_name"] == "receipt.jpg"
    assert job is not None
    assert job["source_document_id"] == doc["id"]


def test_size_stability_debounce_waits_for_stable_file(app_env, tmp_path, make_jpeg):
    from app.channels.inbox import ingest_path

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    dropped = inbox / "growing.jpg"
    dropped.write_bytes(make_jpeg())
    grew = {"done": False}

    def grow_once(_interval: float) -> None:
        if not grew["done"]:
            with dropped.open("ab") as fh:
                fh.write(b"tail")
            grew["done"] = True

    result = ingest_path(dropped, inbox_dir=inbox, stable_interval=0, sleep_func=grow_once)
    assert result == {"status": "pending", "reason": "unstable"}
    assert dropped.exists()
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 0

    result = ingest_path(dropped, inbox_dir=inbox, stable_interval=0)
    assert result["status"] == "staged"
    assert Path(result["moved_to"]).exists()


def test_duplicate_redrop_moves_both_files_and_keeps_one_document(app_env, tmp_path, make_jpeg):
    from app.channels.inbox import ingest_path

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    raw = make_jpeg()
    first = inbox / "receipt.jpg"
    first.write_bytes(raw)
    first_result = ingest_path(first, inbox_dir=inbox, stable_interval=0)
    assert first_result["status"] == "staged"

    second = inbox / "receipt.jpg"
    second.write_bytes(raw)
    second_result = ingest_path(second, inbox_dir=inbox, stable_interval=0)
    assert second_result["status"] == "duplicate"

    processed = sorted((inbox / "processed").iterdir())
    assert len(processed) == 2
    assert all(path.exists() for path in processed)
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='ingest_document'").fetchone()[0] == 1


def test_scan_existing_continues_past_failing_file(app_env, tmp_path, make_jpeg):
    from app.channels.inbox import scan_existing
    from app.ingest.storage import capture

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for idx, name in enumerate(("a.jpg", "b.jpg", "c.jpg")):
        (inbox / name).write_bytes(make_jpeg(color=(180, 40 + idx, 40)))

    def capture_with_one_failure(**kwargs):
        if kwargs["original_name"] == "b.jpg":
            raise RuntimeError("capture exploded")
        return capture(**kwargs)

    results = scan_existing(inbox, capture_func=capture_with_one_failure)

    assert {Path(row["path"]).name for row in results} == {"a.jpg", "b.jpg", "c.jpg"}
    error = next(row for row in results if row["status"] == "error")
    assert Path(error["path"]).name == "b.jpg"
    assert "capture exploded" in error["error"]
    assert (inbox / "b.jpg").exists()
    assert sorted(path.name for path in (inbox / "processed").iterdir()) == ["a.jpg", "c.jpg"]
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 2


def test_repeated_scan_failure_moves_file_to_failed(app_env, tmp_path, make_jpeg):
    from app.channels.inbox import scan_existing

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    dropped = inbox / "bad.jpg"
    dropped.write_bytes(make_jpeg())

    def failing_capture(**_kwargs):
        raise RuntimeError("sqlite locked")

    for _ in range(3):
        results = scan_existing(inbox, capture_func=failing_capture)
        assert len(results) == 1
        assert results[0]["status"] == "error"

    failed_files = list((inbox / "failed").iterdir())
    assert [path.name for path in failed_files] == ["bad.jpg"]
    assert not dropped.exists()
    assert scan_existing(inbox, capture_func=failing_capture) == []


def test_symlink_candidate_is_rejected_without_ingesting_target(app_env, tmp_path):
    from app.channels.inbox import ingest_path, is_candidate

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    secret = b"do not ingest this secret"
    target = tmp_path / "secret.env"
    target.write_bytes(secret)
    link = inbox / "receipt.pdf"
    link.symlink_to(target)

    assert is_candidate(link, inbox) is False
    assert ingest_path(link, inbox_dir=inbox) == {"status": "ignored", "reason": "symlink"}

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 0
    data_dir = tmp_path / "data"
    digest = hashlib.sha256(secret).hexdigest()
    assert not data_dir.exists() or not list(data_dir.rglob(f"{digest}*"))


def test_is_under_returns_false_for_symlink_loop(tmp_path):
    from app.channels.inbox import _is_under

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.symlink_to(second)
    second.symlink_to(first)

    assert _is_under(first, tmp_path) is False


def test_on_modified_resubmits_stable_file(app_env, tmp_path, make_jpeg):
    from app.channels.inbox import InboxEventHandler

    class ImmediateExecutor:
        def __init__(self):
            self.futures: list[Future] = []

        def submit(self, fn, *args):
            future: Future = Future()
            try:
                future.set_result(fn(*args))
            except Exception as exc:  # pragma: no cover - makes failures visible to the assertion
                future.set_exception(exc)
            self.futures.append(future)
            return future

    class Event:
        is_directory = False

        def __init__(self, src_path: Path):
            self.src_path = str(src_path)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    dropped = inbox / "slow.jpg"
    dropped.write_bytes(make_jpeg())
    executor = ImmediateExecutor()
    handler = InboxEventHandler(inbox, executor)

    handler.on_modified(Event(dropped))

    assert len(executor.futures) == 1
    assert executor.futures[0].exception() is None
    assert not dropped.exists()
    assert (inbox / "processed" / "slow.jpg").exists()
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
