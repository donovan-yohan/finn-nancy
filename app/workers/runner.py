"""Single async consumer loop.

Claims one job at a time (matching the backend's low concurrency), runs the handler
in a thread behind the shared LLM gate, and records the outcome. Cold starts are
absorbed by the LLM timeout + job retry/backoff, invisible to the user.
"""
from __future__ import annotations

import asyncio

from ..config import get_settings
from ..db import engine, repo_jobs
from ..llm.client import make_llm
from ..llm.gate import llm_gate
from .handlers import handle_job


def _claim(db_path):
    with engine.write_tx(db_path) as conn:
        return repo_jobs.claim_next(conn)


def _finish(db_path, job_id: int, error: str | None) -> None:
    with engine.write_tx(db_path) as conn:
        if error is None:
            repo_jobs.mark_done(conn, job_id)
        else:
            repo_jobs.mark_failed(conn, job_id, error)


async def run_worker(stop: asyncio.Event, llm=None, poll_seconds: float = 2.0) -> None:
    settings = get_settings()
    if llm is None:
        llm = make_llm()

    with engine.write_tx(settings.db_path) as conn:
        repo_jobs.recover_orphans(conn)

    while not stop.is_set():
        job = await asyncio.to_thread(_claim, settings.db_path)
        if job is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            async with llm_gate():
                await asyncio.to_thread(handle_job, settings.db_path, job, llm)
            await asyncio.to_thread(_finish, settings.db_path, int(job["id"]), None)
        except Exception as exc:  # noqa: BLE001 - record and move on
            await asyncio.to_thread(_finish, settings.db_path, int(job["id"]), repr(exc))
