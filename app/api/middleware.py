"""ASGI guards for the programmatic API surface."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from starlette.types import Message, Receive, Scope, Send

from ..config import get_settings
from .auth import check_token

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestBodyTooLarge(Exception):
    pass


def _json_body(detail: str) -> bytes:
    return json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")


async def _send_json(send: Send, status: int, detail: str) -> None:
    body = _json_body(detail)
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _too_large_detail(cap: int) -> str:
    return f"request body too large (max {cap} bytes)"


class ApiGuardMiddleware:
    """Reject disabled/unauthorized/oversized API requests before body parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api"):
            await self.app(scope, receive, send)
            return

        auth_status = check_token(_header(scope, b"authorization"))
        if auth_status == "disabled":
            await _send_json(send, 503, "API disabled: set API_TOKEN to enable programmatic access")
            return
        if auth_status == "unauthorized":
            await _send_json(send, 401, "invalid or missing bearer token")
            return

        cap = int(get_settings().api_max_body_bytes)
        content_length = _header(scope, b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > cap:
                    await _send_json(send, 413, _too_large_detail(cap))
                    return
            except ValueError:
                pass

        total = 0
        rejected = False
        response_started = False

        async def app_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        async def capped_receive() -> Message:
            nonlocal rejected, total
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                total += len(body)
                if total > cap:
                    if not rejected:
                        rejected = True
                        if not response_started:
                            await _send_json(send, 413, _too_large_detail(cap))
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, capped_receive, app_send)
        except RequestBodyTooLarge:
            return
