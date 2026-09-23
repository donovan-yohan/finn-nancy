"""Finance-scoped frontend for native Hermes chat sessions."""
from __future__ import annotations

from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import get_settings
from ...db import engine
from ...hermes_chat.bridge import serve
from ...hermes_chat.threads import ThreadStore, valid_thread
from ..templating import templates

router = APIRouter()

THREAD_COOKIE = "fn_chat_thread"


def _thread_id(request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(THREAD_COOKIE, "").strip()
    if valid_thread(existing) and ThreadStore(get_settings(), existing).path.is_file():
        return existing, False
    return uuid4().hex, True


def _cookie(response, request, thread_id):
    response.set_cookie(THREAD_COOKIE, thread_id, max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")
    response.headers["Cache-Control"] = "no-store"


def _same_origin(request, *, required=False):
    origin = request.headers.get("origin")
    if not origin:
        return not required and request.headers.get("sec-fetch-site") != "cross-site"
    expected_scheme = {"ws": "http", "wss": "https"}.get(request.url.scheme, request.url.scheme)
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme == expected_scheme and parsed.netloc == request.url.netloc
                and not parsed.path and not parsed.query and not parsed.fragment
                and parsed.username is None)
    except ValueError:
        return False


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


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, why: int | None = None):
    settings = get_settings()
    seed_message = ""
    if why is not None:
        seed = _why_seed_message(str(settings.db_path), why)
        if seed is None:
            raise HTTPException(status_code=404, detail="transaction not found")
        thread_id, is_new = uuid4().hex, True
        seed_message = seed
    else:
        thread_id, is_new = _thread_id(request)
    if is_new and settings.hermes_chat_url:
        ThreadStore(settings, thread_id).create()
    response = templates.TemplateResponse(
        request,
        "chat.html",
        {
            "active": "chat",
            "brand": "nancy",
            "thread_id": thread_id,
            "messages": [],
            "chat_enabled": bool(settings.hermes_chat_url),
            "seed_message": seed_message,
        },
    )
    if is_new:
        _cookie(response, request, thread_id)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/chat/new")
def new_chat(request: Request):
    if not _same_origin(request):
        raise HTTPException(403, "cross-origin chat request")
    thread_id = uuid4().hex
    settings = get_settings()
    if settings.hermes_chat_url:
        ThreadStore(settings, thread_id).create()
    response = RedirectResponse("/chat", status_code=303)
    _cookie(response, request, thread_id)
    return response


@router.websocket("/chat/socket")
async def chat_socket(socket: WebSocket):
    if not _same_origin(socket, required=True):
        await socket.close(code=4403)
        return
    settings = get_settings()
    thread_id = socket.cookies.get(THREAD_COOKIE, "")
    if not valid_thread(thread_id) or not ThreadStore(settings, thread_id).path.is_file():
        await socket.close(code=4403)
        return
    if not settings.hermes_chat_url:
        await socket.close(code=4404)
        return
    await serve(socket, settings, thread_id)
