"""Private capability-to-session mappings, separate from the financial database.

Hermes owns transcript persistence. Only opaque IDs and bounded submission
receipts live here. A nonblocking file lock admits one frontend per thread.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .transport import GatewayError


def valid_thread(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", value))


class ThreadStore:
    def __init__(self, settings, thread_id: str):
        if not valid_thread(thread_id):
            raise GatewayError("invalid_thread")
        self.directory = settings.data_dir / "hermes-chat"
        self.path = self.directory / (thread_id + ".json")
        self.target = hashlib.sha256(json.dumps([
            settings.hermes_chat_url, settings.hermes_chat_profile,
        ]).encode()).hexdigest()

    def create(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.save({"version": 1, "target": self.target, "stored_session_id": None,
                   "submissions": {}})

    def load(self) -> dict:
        try:
            state = json.loads(self.path.read_text())
            if state.get("version") != 1 or state.get("target") != self.target:
                raise ValueError()
            if not isinstance(state.get("submissions"), dict):
                raise ValueError()
            return state
        except (OSError, ValueError, AttributeError):
            raise GatewayError("invalid_thread") from None

    def save(self, state: dict):
        temporary = self.directory / (uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def locked(self):
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GatewayError("thread_in_use") from None
            yield self.load()
        finally:
            os.close(fd)
