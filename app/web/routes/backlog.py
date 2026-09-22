"""Uncategorized expense backlog review and suggestion enqueue routes."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...backlog.batch import active_backlog_proposals_by_transaction
from ...backlog.suggest import list_uncategorized_expense_backlog
from ...config import get_settings
from ...db import engine, repo_jobs
from ...evals.metrics import expense_uncategorized_metrics
from ..templating import templates

router = APIRouter()

DEFAULT_BACKLOG_LIST_LIMIT = 100
SUGGEST_ALL_LIMIT = 100_000


def _parse_limit(raw: str) -> int:
    value = (raw or "").strip().lower()
    if value in {"", "all"}:
        return SUGGEST_ALL_LIMIT
    try:
        parsed = int(value)
    except ValueError:
        raise HTTPException(400, "limit must be a positive integer or all") from None
    if parsed <= 0:
        raise HTTPException(400, "limit must be positive")
    return min(parsed, SUGGEST_ALL_LIMIT)


def _latest_jobs(conn) -> list:
    return conn.execute(
        """
        SELECT *
        FROM jobs
        WHERE type='backlog_suggest'
        ORDER BY created_at DESC, id DESC
        LIMIT 5
        """
    ).fetchall()


@router.get("/backlog", response_class=HTMLResponse)
def backlog_page(request: Request):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        metrics = expense_uncategorized_metrics(conn)
        rows = list_uncategorized_expense_backlog(conn, limit=DEFAULT_BACKLOG_LIST_LIMIT)
        proposals = active_backlog_proposals_by_transaction(
            conn,
            [
                int(row["id"])
                for row in rows
                if int(row["mutation_supported"]) == 1
            ],
        )
        jobs = _latest_jobs(conn)
    return templates.TemplateResponse(
        request,
        "backlog.html",
        {
            "metrics": metrics,
            "rows": rows,
            "proposals": proposals,
            "jobs": jobs,
            "active": "more",
            "brand": "finn",
            "default_limit": 25,
            "list_limit": DEFAULT_BACKLOG_LIST_LIMIT,
        },
    )


@router.post("/backlog/suggest", response_class=HTMLResponse)
def suggest_backlog(request: Request, limit: str = Form("25"),
                    include_closed: str | None = Form(None)):
    settings = get_settings()
    if settings.read_only:
        raise HTTPException(400, "backlog suggestions are disabled in read-only mode")
    parsed_limit = _parse_limit(limit)
    include_closed_flag = include_closed == "1"
    batch = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    with engine.write_tx(settings.db_path) as conn:
        job_id = repo_jobs.enqueue(
            conn,
            "backlog_suggest",
            {"batch": batch, "limit": parsed_limit, "include_closed": include_closed_flag},
        )
        jobs = _latest_jobs(conn)
    return templates.TemplateResponse(
        request,
        "_backlog_job.html",
        {"job_id": job_id, "batch": batch, "limit": parsed_limit, "jobs": jobs},
    )


@router.get("/backlog/status", response_class=HTMLResponse)
def backlog_status(request: Request):
    settings = get_settings()
    with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
        jobs = _latest_jobs(conn)
    return templates.TemplateResponse(
        request,
        "_backlog_job.html",
        {"job_id": None, "batch": "", "limit": None, "jobs": jobs},
    )
