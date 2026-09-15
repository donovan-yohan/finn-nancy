"""Shared Jinja2 templates instance (filters registered once)."""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from .filters import money

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["money"] = money


# One guarded, request-memoized query; acceptable to run inline even on the async /chat render for a local single-user app.
@pass_context
def _pending_action_count(context) -> int:
    request = context.get("request")
    if request is not None:
        cached = getattr(request.state, "_pending_action_count", None)
        if cached is not None:
            return cached
    try:
        from ..config import get_settings
        from ..db import engine, repo_actions

        settings = get_settings()
        with engine.read_conn(settings.db_path, read_only=settings.read_only) as conn:
            value = len(repo_actions.list_proposals(conn))
    except Exception:
        value = 0
    if request is not None:
        request.state._pending_action_count = value
    return value


templates.env.globals["pending_action_count"] = _pending_action_count
