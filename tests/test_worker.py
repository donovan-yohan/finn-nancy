from __future__ import annotations

import asyncio

from app.db import engine
from app.ingest.schemas import ExtractedReceipt


def test_worker_drains_ingest_job(app_env, make_jpeg, fake_llm):
    """End-to-end async path: capture -> jobs table -> worker -> promote."""
    from app.ingest.storage import capture
    from app.workers.runner import run_worker

    cap = capture(raw=make_jpeg(), original_name="cafe.jpg", channel="web")
    assert cap["status"] == "staged"

    receipt = ExtractedReceipt(
        merchant="Night Owl Cafe", currency="CAD", subtotal_cents=4850, total_cents=4850,
        category_guess="Restaurants", confidence=0.9,
    )

    async def _drive():
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(stop, llm=fake_llm(receipt), poll_seconds=0.05))
        txn = None
        for _ in range(100):  # up to ~5s
            await asyncio.sleep(0.05)
            with engine.read_conn(app_env) as conn:
                txn = conn.execute(
                    "SELECT * FROM transactions WHERE source_document_id=?",
                    (cap["source_document_id"],),
                ).fetchone()
            if txn is not None:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        return txn

    txn = asyncio.run(_drive())
    assert txn is not None
    assert txn["amount_cents"] == -4850

    with engine.read_conn(app_env) as conn:
        job = conn.execute("SELECT status FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        doc = conn.execute(
            "SELECT status FROM source_documents WHERE id=?", (cap["source_document_id"],)
        ).fetchone()
    assert job["status"] == "done"
    assert doc["status"] == "processed"
