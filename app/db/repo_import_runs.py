"""Batch upload runs.

A run is the object a user comes back to. Without it, "drop five statements and
check later" has no destination: per-file results are transient, and a document
that stalls is invisible.

The rollup deliberately reports what still needs a person before what
succeeded, and treats a run as unfinished until every document is terminal.
"""
from __future__ import annotations

import sqlite3

# Documents still moving under their own steam.
WORKING = ("staged",)
# Documents that stopped and want a person.
ATTENTION = ("needs_review",)


def create(conn: sqlite3.Connection, *, channel: str = "web", label: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO import_runs(channel, label) VALUES (?,?)",
        (channel, label[:200]),
    )
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM import_runs WHERE id=?", (int(run_id),)
    ).fetchone()


def documents(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM source_documents
           WHERE import_run_id=? ORDER BY id""",
        (int(run_id),),
    ).fetchall()


def rollup(conn: sqlite3.Connection, run_id: int) -> dict:
    rows = documents(conn, run_id)
    working = [r for r in rows if r["status"] in WORKING]
    attention = [r for r in rows if r["status"] in ATTENTION]
    settled = [r for r in rows if r not in working and r not in attention]

    if working:
        state = "working"
    elif attention:
        # The obstacle outranks the reward: a run with anything unresolved
        # reports that, not its successes.
        state = "needs_you"
    else:
        state = "done"

    return {
        "run_id": int(run_id),
        "state": state,
        "total": len(rows),
        "working": len(working),
        "needs_you": len(attention),
        "settled": len(settled),
        "documents": rows,
        "attention_documents": attention,
        "poll_seconds": poll_interval(len(working)),
    }


def poll_interval(working: int) -> int:
    """Seconds until the next status check, or 0 when the run is terminal.

    The server owns backoff so the page never polls forever: a run with nothing
    in flight returns zero and the client stops asking.
    """
    if working <= 0:
        return 0
    return 2 if working > 3 else 5


def open_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT id FROM import_runs ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [rollup(conn, int(row["id"])) for row in rows]


def pending_attention_count(conn: sqlite3.Connection) -> int:
    """Documents across all runs that stopped and need a person."""
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM source_documents
           WHERE import_run_id IS NOT NULL AND status IN ('needs_review')"""
    ).fetchone()
    return int(row["n"] or 0)
