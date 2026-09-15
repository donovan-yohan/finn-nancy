"""Job dispatch: job.type -> handler function."""
from __future__ import annotations

import json
import sqlite3

from ..backlog.batch import run_batch as run_backlog_batch
from ..db import repo_embeddings
from ..ingest.pipeline import process_document
from ..reconcile.engine import reconcile_document
from .insight_prose import handle_monthly_insight


def handle_job(db_path, job: sqlite3.Row, llm) -> dict:
    payload = json.loads(job["payload_json"] or "{}")
    if job["type"] == "ingest_document":
        return process_document(db_path, int(payload["source_document_id"]), llm)
    if job["type"] == "reconcile_document":
        return reconcile_document(db_path, int(payload["source_document_id"]), llm)
    if job["type"] == "monthly_insight":
        return handle_monthly_insight(db_path, payload, llm)
    if job["type"] == "embed_transactions":
        return repo_embeddings.embed_missing_transactions(db_path)
    if job["type"] == "backlog_suggest":
        return run_backlog_batch(
            db_path,
            batch=str(payload.get("batch") or job["id"]),
            limit=int(payload.get("limit") or 25),
            llm=llm,
            include_closed=bool(payload.get("include_closed")),
        )
    raise ValueError(f"unknown job type: {job['type']}")
