"""Watched drop-dir capture channel.

Policy: eligible PDFs/images dropped into INBOX_DIR are captured through the
shared ingest entrypoint, then moved to INBOX_DIR/processed/. Duplicate content
is still moved there after capture reports a dedupe, so the inbox drains cleanly.
Files that raise during ingest three times are moved to INBOX_DIR/failed/ so one
poison file cannot wedge startup scans or the live watcher.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from ..db import repo_captures
from ..ingest.storage import capture

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff"}
DOCUMENT_SUFFIXES = {".pdf"}
TEMP_SUFFIXES = {".tmp", ".part", ".crdownload", ".download", ".swp"}
PROCESSED_DIRNAME = "processed"
FAILED_DIRNAME = "failed"
_QUARANTINE_FAILURES = 3
_FAILURE_COUNTS: dict[Path, int] = {}
_FAILURE_LOCK = threading.Lock()


def _inbox_root(path: Path) -> Path:
    parent = path.parent
    if parent.name == PROCESSED_DIRNAME:
        return parent.parent
    return parent


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def is_candidate(path: Path, inbox_dir: Path | None = None) -> bool:
    if path.is_symlink():
        return False
    if path.name.startswith(".") or path.name.endswith("~"):
        return False
    if path.suffix.lower() in TEMP_SUFFIXES:
        return False
    root = inbox_dir or _inbox_root(path)
    if _is_under(path, root / PROCESSED_DIRNAME):
        return False
    if _is_under(path, root / FAILED_DIRNAME):
        return False
    return path.suffix.lower() in IMAGE_SUFFIXES | DOCUMENT_SUFFIXES


def size_is_stable(
    path: Path,
    *,
    checks: int = 2,
    interval: float = 0.2,
    sleep_func: Callable[[float], None] = time.sleep,
) -> bool:
    if checks < 2:
        checks = 2
    sizes: list[int] = []
    for idx in range(checks):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size <= 0:
            return False
        sizes.append(size)
        if idx < checks - 1:
            sleep_func(interval)
    return len(set(sizes)) == 1


def _processed_destination(processed_dir: Path, name: str, sha256: str) -> Path:
    candidate = processed_dir / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    candidate = processed_dir / f"{stem}-{sha256[:8]}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = processed_dir / f"{stem}-{sha256[:8]}-{counter}{suffix}"
        counter += 1
    return candidate


def _failed_destination(failed_dir: Path, name: str) -> Path:
    candidate = failed_dir / name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = failed_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _failure_key(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def _clear_failure(path: Path) -> None:
    key = _failure_key(path)
    with _FAILURE_LOCK:
        _FAILURE_COUNTS.pop(key, None)


def _record_ingest_failure(path: Path, inbox_dir: Path, exc: Exception) -> dict[str, Any]:
    logger.warning("inbox ingest failed for %s: %s", path, exc)
    key = _failure_key(path)
    with _FAILURE_LOCK:
        count = _FAILURE_COUNTS.get(key, 0) + 1
        _FAILURE_COUNTS[key] = count

    result: dict[str, Any] = {"status": "error", "path": str(path), "error": str(exc)}
    if count < _QUARANTINE_FAILURES:
        return result

    failed_dir = inbox_dir / FAILED_DIRNAME
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        dest = _failed_destination(failed_dir, path.name)
        shutil.move(str(path), str(dest))
    except Exception as move_exc:  # noqa: BLE001 - quarantine must not wedge scans
        logger.warning("inbox quarantine failed for %s after %d failures: %s", path, count, move_exc)
        return result

    logger.warning("inbox quarantined %s after %d failures to %s", path, count, dest)
    result["quarantined_to"] = str(dest)
    return result


def _ingest_path_result(
    path: Path,
    inbox_dir: Path,
    *,
    capture_func: Callable[..., dict[str, Any]] = capture,
) -> dict[str, Any]:
    try:
        result = ingest_path(path, inbox_dir=inbox_dir, capture_func=capture_func)
    except Exception as exc:  # noqa: BLE001 - one file must not wedge the inbox
        return _record_ingest_failure(path, inbox_dir, exc)
    _clear_failure(path)
    return {"path": str(path), **result}


def ingest_path(
    path: str | Path,
    *,
    inbox_dir: str | Path | None = None,
    stable_checks: int = 2,
    stable_interval: float = 0.2,
    sleep_func: Callable[[float], None] = time.sleep,
    capture_func: Callable[..., dict[str, Any]] = capture,
) -> dict[str, Any]:
    src = Path(path)
    root = Path(inbox_dir) if inbox_dir is not None else _inbox_root(src)
    try:
        mode = src.lstat().st_mode
    except FileNotFoundError:
        return {"status": "ignored", "reason": "missing"}
    if src.is_symlink():
        return {"status": "ignored", "reason": "symlink"}
    if not stat.S_ISREG(mode):
        return {"status": "ignored", "reason": "not_regular_file"}
    if not is_candidate(src, root):
        return {"status": "ignored", "reason": "not_candidate"}
    if not size_is_stable(src, checks=stable_checks, interval=stable_interval, sleep_func=sleep_func):
        return {"status": "pending", "reason": "unstable"}

    try:
        mode = src.lstat().st_mode
    except FileNotFoundError:
        return {"status": "ignored", "reason": "missing"}
    if src.is_symlink():
        return {"status": "ignored", "reason": "symlink"}
    if not stat.S_ISREG(mode):
        return {"status": "ignored", "reason": "not_regular_file"}
    raw = src.read_bytes()
    if not raw:
        return {"status": "ignored", "reason": "empty"}
    sha256 = hashlib.sha256(raw).hexdigest()
    observed = src.stat(follow_symlinks=False)
    occurrence_key = (
        f"{observed.st_dev}:{observed.st_ino}:{observed.st_ctime_ns}:"
        f"{observed.st_size}:{sha256}"
    )
    captured = capture_func(
        raw=raw,
        original_name=src.name,
        channel="inbox",
        client_capture_id=repo_captures.stable_origin_id(
            "inbox", occurrence_key
        ),
        source_metadata={"source": "inbox"},
    )

    processed_dir = root / PROCESSED_DIRNAME
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = _processed_destination(processed_dir, src.name, str(captured.get("sha256") or sha256))
    shutil.move(str(src), str(dest))
    return {**captured, "moved_to": str(dest)}


def scan_existing(
    inbox_dir: str | Path,
    *,
    capture_func: Callable[..., dict[str, Any]] = capture,
) -> list[dict[str, Any]]:
    root = Path(inbox_dir)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if is_candidate(path, root):
            results.append(_ingest_path_result(path, root, capture_func=capture_func))
    return results


def safe_scan_existing(inbox_dir: str | Path) -> list[dict[str, Any]]:
    try:
        return scan_existing(inbox_dir)
    except Exception as exc:  # noqa: BLE001 - startup should not wedge the app
        logger.warning("inbox startup scan failed: %s", exc)
        return []


class InboxEventHandler(FileSystemEventHandler):
    def __init__(self, inbox_dir: Path, executor: concurrent.futures.Executor) -> None:
        self.inbox_dir = inbox_dir
        self.executor = executor

    def _submit(self, path: str) -> None:
        try:
            candidate = Path(path)
            if is_candidate(candidate, self.inbox_dir):
                self.executor.submit(_safe_ingest_path, candidate, self.inbox_dir)
        except Exception as exc:  # noqa: BLE001 - watchdog dispatch must not die
            logger.warning("inbox watcher dispatch failed for %s: %s", path, exc)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.dest_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.src_path)

    def on_closed(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._submit(event.src_path)


def _safe_ingest_path(path: Path, inbox_dir: Path) -> None:
    _ingest_path_result(path, inbox_dir)


@dataclass
class InboxWatcher:
    observer: Observer
    executor: concurrent.futures.ThreadPoolExecutor

    def stop(self, timeout: float = 5.0) -> None:
        self.observer.stop()
        self.observer.join(timeout=timeout)
        self.executor.shutdown(wait=False, cancel_futures=True)


def start_watcher(inbox_dir: str | Path) -> InboxWatcher:
    root = Path(inbox_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / PROCESSED_DIRNAME).mkdir(parents=True, exist_ok=True)
    (root / FAILED_DIRNAME).mkdir(parents=True, exist_ok=True)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="inbox-capture")
    handler = InboxEventHandler(root, executor)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=False)
    observer.start()
    return InboxWatcher(observer=observer, executor=executor)
