"""SQLite connection management.

Single-writer discipline: every mutation goes through ``write_tx()``, which holds
one process-wide lock for the duration of the transaction. Reads open their own
short-lived connection (SQLite handles concurrent readers under WAL). This keeps
the "one writer, many readers" invariant the whole app relies on.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# One writer for the whole process. WAL lets readers proceed concurrently, but we
# still serialize writers ourselves so a sloppy handler can't open a second one.
_write_lock = threading.Lock()


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a tuned SQLite connection. Rows come back as ``sqlite3.Row`` (dict-like)."""
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if read_only:
        # Belt-and-suspenders: refuse writes even if a handler gets sloppy.
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def write_tx(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Serialized cross-process write transaction.

    ``BEGIN IMMEDIATE`` acquires SQLite's reserved writer lock before any policy
    read.  A close decision and every guarded mutation therefore linearize in
    one transaction even when a queued worker runs in another process.
    """
    with _write_lock:
        conn = connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def read_conn(path: str | Path, *, read_only: bool = True) -> Iterator[sqlite3.Connection]:
    """Short-lived read connection."""
    conn = connect(path, read_only=read_only)
    try:
        yield conn
    finally:
        conn.close()
