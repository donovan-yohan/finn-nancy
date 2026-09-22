"""Chat page and SSE streaming endpoint."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from ...agents.chat import load_thread_messages, stream_chat
from ...config import get_settings
from ...db import engine
from ...llm.warm import warm
from ..templating import templates

router = APIRouter()

THREAD_COOKIE = "fn_chat_thread"


def _thread_id(request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(THREAD_COOKIE, "").strip()
    if existing:
        return existing, False
    return uuid4().hex, True


def _sse_frame(event: dict) -> str:
    event_type = str(event.get("type") or "message")
    data = json.dumps(event, separators=(",", ":"), default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def _why_seed_message(db_path: str, txn_id: int) -> str | None:
    with engine.read_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT
              t.id,
              t.posted_on,
              t.description,
              t.counterparty,
              t.amount_cents,
              GROUP_CONCAT(DISTINCT c.name) AS categories
            FROM transactions t
            LEFT JOIN transaction_splits s ON s.transaction_id = t.id
            LEFT JOIN categories c ON c.id = s.category_id
            WHERE t.id = ?
            GROUP BY t.id
            """,
            (txn_id,),
        ).fetchone()
    if row is None:
        return None
    merchant = row["counterparty"] or row["description"]
    categories = row["categories"] or "Uncategorized"
    return (
        f"Why was transaction {row['id']} ({merchant}, {row['posted_on']}, "
        f"{_money(row['amount_cents'])}) categorized as {categories}?"
    )


async def _with_keepalives(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def consume() -> None:
        try:
            async for event in events:
                await queue.put(event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(consume())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                break
            yield _sse_frame(event)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, why: int | None = None):
    settings = get_settings()
    seed_message = ""
    if why is not None:
        seed = _why_seed_message(str(settings.db_path), why)
        if seed is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        thread_id, is_new = uuid4().hex, True
        messages = []
        seed_message = seed
    else:
        thread_id, is_new = _thread_id(request)
        messages = await load_thread_messages(str(settings.db_path), thread_id)
    response = templates.TemplateResponse(
        request,
        "chat.html",
        {
            "active": "chat",
            "brand": "nancy",
            "thread_id": thread_id,
            "messages": messages,
            "seed_message": seed_message,
        },
    )
    if is_new:
        response.set_cookie(THREAD_COOKIE, thread_id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


@router.post("/chat/new")
def new_chat():
    response = RedirectResponse("/chat", status_code=303)
    response.set_cookie(THREAD_COOKIE, uuid4().hex, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


@router.post("/chat/stream")
async def chat_stream(request: Request, message: str = Form(...)):
    settings = get_settings()
    thread_id, is_new = _thread_id(request)
    events = stream_chat(str(settings.db_path), thread_id, message)
    response = StreamingResponse(
        _with_keepalives(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    if is_new:
        response.set_cookie(THREAD_COOKIE, thread_id, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return response


@router.post("/chat/ping")
async def chat_ping():
    ok = await warm()
    return JSONResponse({"ok": ok})
