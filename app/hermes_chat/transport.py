"""Bounded native Hermes JSON-RPC transport; never expose its credential or admin API."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from websockets.asyncio.client import connect


HISTORY_LIMIT = 20
HISTORY_MAX_BYTES = 1024 * 1024
HISTORY_TIMEOUT = 5


class GatewayError(Exception):
    """Content-free errors safe to project to the finance UI."""


class NoRedirectConnect(connect):
    def process_redirect(self, exc):
        # A redirect must not carry the gateway credential to another origin.
        return exc


def gateway_uri(settings) -> str:
    url = settings.hermes_chat_url
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "ws" and parsed.hostname in {"127.0.0.1", "::1"}
                 and parsed.port is not None and 0 < parsed.port < 65536
                 and parsed.path == "/api/ws" and not parsed.query and not parsed.fragment
                 and parsed.username is None and parsed.password is None
                 and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", settings.hermes_chat_profile))
        if not valid:
            raise ValueError()
        token = settings.hermes_chat_token.get_secret_value() if settings.hermes_chat_token else ""
        if settings.hermes_chat_token_file is not None:
            if token:
                raise ValueError()
            token = settings.hermes_chat_token_file.read_text().strip()
        if not token or len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError()
    except (OSError, ValueError):
        raise GatewayError("not_configured") from None
    return url + "?" + urlencode({"token": token})


class Gateway:
    def __init__(self, settings):
        self.settings = settings
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.ready = asyncio.Event()
        self.socket = None
        self.reader = None
        self.sequence = 0

    async def __aenter__(self):
        try:
            self.socket = await NoRedirectConnect(
                gateway_uri(self.settings), proxy=None, open_timeout=10,
                close_timeout=3, max_size=2 * 1024 * 1024, max_queue=32,
            )
            self.reader = asyncio.create_task(self._read())
            await asyncio.wait_for(self.ready.wait(), 15)
            await self.call("client.capabilities", {"server_requests": True})
        except (Exception, asyncio.CancelledError):
            await self.__aexit__(None, None, None)
            raise GatewayError("gateway_unavailable") from None
        return self

    async def __aexit__(self, *_):
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.socket:
            await self.socket.close()

    async def _read(self):
        try:
            async for raw in self.socket:
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise GatewayError("invalid_gateway_frame")
                if "method" not in frame and frame.get("id") in self.pending:
                    future = self.pending.pop(frame["id"])
                    if not future.done():
                        if "error" in frame:
                            code = "invalid_thread" if frame["error"].get("code") == 4007 else "gateway_request_failed"
                            future.set_exception(GatewayError(code))
                        else:
                            future.set_result(frame.get("result", {}))
                elif frame.get("method") == "event":
                    params = frame.get("params", {})
                    if params.get("type") == "gateway.ready":
                        self.ready.set()
                    else:
                        self.events.put_nowait(frame)
                elif "id" in frame and "method" in frame:
                    self.events.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Provider frames/errors can contain secrets; never log them.
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(GatewayError("gateway_disconnected"))
            self.pending.clear()
            # If the queue overflowed, discard buffered deltas and require snapshot recovery.
            if self.events.full():
                while not self.events.empty():
                    self.events.get_nowait()
            self.events.put_nowait(None)

    async def call(self, method: str, params: dict):
        self.sequence += 1
        request_id = f"finance-{self.sequence}"
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.socket.send(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                               "method": method, "params": params}))
            return await asyncio.wait_for(future, 30)
        except (asyncio.CancelledError, GatewayError):
            raise
        except Exception:
            raise GatewayError("gateway_request_failed") from None
        finally:
            self.pending.pop(request_id, None)

    async def recent_history(self, stored_session_id: str):
        """One server-selected page; never accept a URL/profile/offset from the UI."""
        if (not isinstance(stored_session_id, str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", stored_session_id)):
            raise GatewayError("invalid_thread")
        uri = urlsplit(gateway_uri(self.settings))
        url = f"http://{uri.netloc}/api/sessions/{stored_session_id}/messages"
        headers = {"X-Hermes-Session-Token": parse_qs(uri.query)["token"][0],
                   "Accept-Encoding": "identity"}
        params = {"profile": self.settings.hermes_chat_profile, "limit": HISTORY_LIMIT,
                  "offset": 0, "order": "latest"}
        try:
            async with asyncio.timeout(HISTORY_TIMEOUT):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                             timeout=HISTORY_TIMEOUT) as client:
                    async with client.stream("GET", url, params=params, headers=headers) as response:
                        if (response.status_code != 200
                                or response.headers.get("content-encoding", "identity") != "identity"):
                            raise ValueError()
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > HISTORY_MAX_BYTES:
                                raise ValueError()
                            body.extend(chunk)
            page = json.loads(body)
            rows, pagination = page["messages"], page["pagination"]
            if (page["profile"] != self.settings.hermes_chat_profile
                    or not isinstance(page["session_id"], str)
                    or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", page["session_id"])
                    or not isinstance(rows, list) or len(rows) > HISTORY_LIMIT
                    or any(not isinstance(row, dict) or not isinstance(row.get("role"), str) for row in rows)
                    or pagination != {"limit": HISTORY_LIMIT, "offset": 0, "order": "latest", "returned": len(rows)}):
                raise ValueError()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, RecursionError):
            raise GatewayError("history_unavailable") from None
        # REST retains physical content for exports; only display_content is
        # intended for a chat when the native projection supplies it.
        messages = [{"role": row["role"], "display_kind": row.get("display_kind"),
                     "content": row.get("display_content", row.get("content", ""))} for row in rows]
        return {"history_session_id": page["session_id"], "messages": messages, "history_window": {
            "status": "recent", "limit": HISTORY_LIMIT, "returned": len(rows),
        }}

    async def answer(self, request_id: str, result: dict | None):
        frame = {"jsonrpc": "2.0", "id": request_id}
        if result is None:
            frame["error"] = {"code": -32601, "message": "Unsupported in the finance client"}
        else:
            frame["result"] = result
        await self.socket.send(json.dumps(frame))
