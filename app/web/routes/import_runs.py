"""Batch import runs: the destination for "upload these and check back later".

One page per run, one poller for the whole run, and a rollup that leads with
what still needs a person. The server owns the polling interval and returns
zero once nothing is in flight, so the page stops asking rather than spinning
against work that has already finished or died.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...config import get_settings
from ...db import engine, repo_documents, repo_import_runs
from ..templating import templates

router = APIRouter()


def _context(run_id: int) -> dict:
    settings = get_settings()
    with engine.read_conn(settings.db_path) as conn:
        run = repo_import_runs.get(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="import run not found")
        rollup = repo_import_runs.rollup(conn, run_id)

    documents = []
    for doc in rollup["documents"]:
        failure = repo_documents.extraction_failure(doc)
        documents.append({
            "id": int(doc["id"]),
            "original_name": doc["original_name"],
            "kind": doc["kind"],
            "status": doc["status"],
            "mime_type": doc["mime_type"],
            "failure": failure,
            "is_working": doc["status"] in repo_import_runs.WORKING,
            "needs_you": doc["status"] in repo_import_runs.ATTENTION,
            "is_pdf": str(doc["mime_type"] or "") == "application/pdf",
        })
    rollup["documents"] = documents
    rollup["attention_documents"] = [d for d in documents if d["needs_you"]]
    return {"run": dict(run), "rollup": rollup}


@router.get("/imports/runs/{run_id}", response_class=HTMLResponse)
def import_run(request: Request, run_id: int):
    context = _context(run_id)
    context.update({"active": "more", "brand": "finn"})
    return templates.TemplateResponse(request, "import_run.html", context)


@router.get("/imports/runs/{run_id}/status", response_class=HTMLResponse)
def import_run_status(request: Request, run_id: int):
    return templates.TemplateResponse(
        request, "_import_run_status.html", _context(run_id)
    )
