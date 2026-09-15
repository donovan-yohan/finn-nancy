from __future__ import annotations

import queue
import threading

from app.db import engine, repo_period_policy


def test_close_wins_queued_writer_race_and_writer_observes_lock(empty_db):
    """A second connection cannot guard against a stale pre-close snapshot."""
    close_started = threading.Event()
    writer_started = threading.Event()
    release_close = threading.Event()
    results: queue.Queue[object] = queue.Queue()

    def close_first() -> None:
        conn = engine.connect(empty_db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            close_started.set()
            assert release_close.wait(timeout=5)
            repo_period_policy.close_period(
                conn,
                "2026-07",
                snapshot={"month": "2026-07"},
                exceptions=[],
                actor="human:owner",
                reason="monthly sign-off",
                operation_key="race:close:2026-07",
            )
            conn.commit()
            results.put("closed")
        except BaseException as exc:  # pragma: no cover - asserted in parent
            conn.rollback()
            results.put(exc)
        finally:
            conn.close()

    def queued_writer() -> None:
        conn = engine.connect(empty_db)
        try:
            writer_started.set()
            conn.execute("BEGIN IMMEDIATE")
            try:
                repo_period_policy.guard_months(conn, ["2026-07"])
            except repo_period_policy.PeriodLockedError:
                results.put("writer-rejected")
            else:  # pragma: no cover - the assertion below reports this
                results.put("writer-bypassed-lock")
            conn.rollback()
        except BaseException as exc:  # pragma: no cover - asserted in parent
            results.put(exc)
        finally:
            conn.close()

    close_thread = threading.Thread(target=close_first)
    close_thread.start()
    assert close_started.wait(timeout=5)

    writer_thread = threading.Thread(target=queued_writer)
    writer_thread.start()
    assert writer_started.wait(timeout=5)
    release_close.set()

    close_thread.join(timeout=5)
    writer_thread.join(timeout=5)
    assert not close_thread.is_alive()
    assert not writer_thread.is_alive()
    observed = {results.get_nowait(), results.get_nowait()}
    assert observed == {"closed", "writer-rejected"}
