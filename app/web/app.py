"""FastAPI application factory. Runs the portal and, in-process, the background worker."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..api.middleware import ApiGuardMiddleware
from ..api.routes import router as api_router
from ..channels import inbox, telegram
from ..config import get_settings
from ..db import engine, repo_embeddings
from ..llm.warm import warm
from ..workers.runner import run_worker
from .routes.actions import router as actions_router
from .routes.backlog import router as backlog_router
from .routes.chat import router as chat_router
from .routes.close import router as close_router
from .routes.dashboard import router as dashboard_router
from .routes.documents import router as documents_router
from .routes.goals import router as goals_router
from .routes.ledger import router as ledger_router
from .routes.manage import router as manage_router
from .routes.pwa import router as pwa_router
from .routes.recon import router as recon_router
from .routes.review import router as review_router
from .routes.structured_imports import router as structured_imports_router
from .routes.import_runs import router as import_runs_router
from .routes.statement_source import router as statement_source_router
from .routes.statements import router as statements_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    stop = asyncio.Event()
    if settings.embeddings_enabled:
        with engine.write_tx(settings.db_path) as conn:
            repo_embeddings.enqueue_embed_transactions(conn)
    worker_task = asyncio.create_task(run_worker(stop))
    telegram_stop = asyncio.Event()
    telegram_task = None
    telegram_runtime = telegram.TelegramRuntimeState(
        configured=telegram.telegram_credentials_configured(settings)
    )
    app.state.telegram_runtime = telegram_runtime
    app.state.telegram_task = None
    inbox_watcher = None
    inbox_scan_task = None
    if telegram_runtime.configured:
        telegram_task = asyncio.create_task(
            telegram.run_poller(
                telegram_stop,
                runtime_state=telegram_runtime,
            )
        )
        app.state.telegram_stop = telegram_stop
        app.state.telegram_task = telegram_task
    if settings.inbox_dir:
        inbox_watcher = inbox.start_watcher(settings.inbox_dir)
        inbox_scan_task = asyncio.create_task(asyncio.to_thread(inbox.safe_scan_existing, settings.inbox_dir))
        app.state.inbox_watcher = inbox_watcher
    asyncio.create_task(warm())  # fire-and-forget; swallows errors if endpoint is down
    app.state.worker_stop = stop
    try:
        yield
    finally:
        if telegram_task is not None:
            telegram_stop.set()
            try:
                await asyncio.wait_for(telegram_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                telegram_task.cancel()
        if inbox_watcher is not None:
            await asyncio.to_thread(inbox_watcher.stop)
        if inbox_scan_task is not None:
            try:
                await asyncio.wait_for(inbox_scan_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                inbox_scan_task.cancel()
        stop.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            worker_task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="finn-nancy", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(ApiGuardMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(actions_router)
    app.include_router(backlog_router)
    app.include_router(api_router)
    app.include_router(chat_router)
    app.include_router(close_router)
    app.include_router(dashboard_router)
    app.include_router(documents_router)
    app.include_router(goals_router)
    app.include_router(import_runs_router)
    app.include_router(ledger_router)
    app.include_router(manage_router)
    app.include_router(pwa_router)
    app.include_router(recon_router)
    app.include_router(review_router)
    app.include_router(statement_source_router)
    app.include_router(statements_router)
    app.include_router(structured_imports_router)
    return app


# Module-level app for `uvicorn app.web.app:app`.
app = create_app()
