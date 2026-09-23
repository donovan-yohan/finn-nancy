"""Allowlisted finance UI protocol over the native Hermes session gateway."""
from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import UUID

from starlette.websockets import WebSocketDisconnect

from .threads import ThreadStore
from .transport import Gateway, GatewayError, HISTORY_LIMIT


ERRORS = {
    "invalid_request": "That chat request was not accepted.",
    "gateway_unavailable": "Hermes is unavailable. Reconnect to check the conversation; your message was not retried.",
    "gateway_request_failed": "Hermes could not confirm the request. Reconnect before sending it again.",
    "gateway_disconnected": "Connection to Hermes lost. Reconnect to recover the conversation.",
    "thread_in_use": "This conversation is open in another tab. Close that tab or start a new chat.",
    "invalid_thread": "This conversation cannot be resumed here. Start a new chat.",
    "turn_running": "Wait for the current reply or stop it before sending another message.",
    "uncertain_send": "This message may already have reached Hermes. Check the conversation before sending again.",
    "not_configured": "Hermes chat has not been configured.",
    "request_expired": "That question is no longer waiting for an answer.",
    "thread_full": "Start a new chat to send more messages.",
}


def error_frame(code):
    return {"type": "error", "code": code, "message": ERRORS.get(code, "Hermes chat could not continue.")}


def text(value, limit=None):
    # The transport bounds frames. Never silently truncate a financial answer
    # (including its final caveats) or a command awaiting approval.
    if not isinstance(value, str):
        return ""
    if limit is not None and len(value) > limit:
        return value[:limit] + "\n[Preview truncated]"
    return value


def snapshot(result: dict) -> dict:
    messages = []
    for row in result.get("messages", []):
        if row.get("role") not in {"user", "assistant"} or row.get("display_kind"):
            continue
        content = text(row.get("content", row.get("text", "")))
        if content:
            messages.append({"role": row["role"], "content": content})
    inflight = result.get("inflight")
    visible = None
    if isinstance(inflight, dict) and not inflight.get("display_kind"):
        visible = {key: text(inflight.get(key)) for key in ("user", "assistant")}
        visible["error"] = ("Hermes could not finish this reply."
                            if inflight.get("error") or inflight.get("status") == "error" else "")
        visible["streaming"] = bool(inflight.get("streaming"))
    open_ids = [request["id"] for request in result.get("open_requests") or []
                if isinstance(request, dict) and isinstance(request.get("id"), str)
                and request.get("method") in {"approval", "clarify"}
                and request.get("params", {}).get("session_id") == result.get("session_id")]
    window = result.get("history_window")
    history_window = None
    if (isinstance(window, dict) and window.get("status") in {"recent", "unavailable"}
            and window.get("limit") == HISTORY_LIMIT and type(window.get("returned")) is int
            and 0 <= window["returned"] <= HISTORY_LIMIT):
        history_window = {key: window[key] for key in ("status", "limit", "returned")}
    return {"type": "snapshot", "messages": messages, "open_request_ids": open_ids,
            "history_window": history_window, "inflight": visible,
            "running": bool(result.get("running") or result.get("info", {}).get("running")
                            or (inflight or {}).get("streaming"))}


class Bridge:
    def __init__(self, browser, settings, store, state, gateway):
        self.browser, self.settings, self.store = browser, settings, store
        self.state, self.gateway = state, gateway
        self.session_id = ""
        self.running = False
        self.requests: dict[str, dict] = {}
        self.serial = asyncio.Lock()

    def params(self, **values):
        return {"session_id": self.session_id, "profile": self.settings.hermes_chat_profile, **values}

    async def attach(self):
        stored = self.state["stored_session_id"]
        if stored:
            # Read history before attaching: inflight and open requests must come
            # from the fresh native resume, not a snapshot held during HTTP I/O.
            try:
                history = await self.gateway.recent_history(stored)
            except GatewayError as exc:
                if str(exc) != "history_unavailable":
                    raise
                history = {"messages": [], "history_window": {
                    "status": "unavailable", "limit": HISTORY_LIMIT, "returned": 0,
                }}
            result = await self.gateway.call("session.resume", {
                "session_id": stored, "profile": self.settings.hermes_chat_profile,
                "source": "finn-nancy", "close_on_disconnect": False, "omit_messages": True,
            })
            if result.get("messages_omitted") is False or result.get("messages") != []:
                # Live unpersisted sessions omit the marker, but still return [];
                # never accept a transcript or retry without omit_messages.
                raise GatewayError("gateway_request_failed")
            # Both native interfaces follow compression descendants. Only project
            # the page if the fresh profile-scoped resume confirms the same tip.
            if history.pop("history_session_id", None) != result.get("session_key", stored):
                history = {"messages": [], "history_window": {
                    "status": "unavailable", "limit": HISTORY_LIMIT, "returned": 0,
                }}
            result.update(history)
        else:
            result = await self.gateway.call("session.create", {
                "profile": self.settings.hermes_chat_profile, "source": "finn-nancy",
                "title": "Finn Nancy", "close_on_disconnect": False,
                "follow_profile_config": True,
            })
            self.state["stored_session_id"] = result["stored_session_id"]
            self.store.save(self.state)
        self.session_id = result["session_id"]
        projected = snapshot(result)
        self.running = projected["running"]
        await self.browser.send_json(projected)
        for request in result.get("open_requests") or []:
            await self.server_request(request)

    async def server_request(self, frame):
        request_id, kind, params = frame.get("id"), frame.get("method"), frame.get("params", {})
        if not isinstance(request_id, str) or params.get("session_id") != self.session_id:
            return  # Never acknowledge or disclose another session's requests.
        if kind not in {"approval", "clarify"}:
            await self.gateway.answer(request_id, None)
            return
        if kind == "approval":
            allowed = params.get("choices", [])
            projected = {"command": text(params.get("command")),
                         "description": text(params.get("description")),
                         "choices": [choice for choice in ("once", "deny") if choice in allowed]}
        else:
            projected = {key: params[key] for key in (
                "question", "choices", "multi_select", "questions", "answers") if key in params}
        self.requests[request_id] = {"kind": kind, "params": projected}
        await self.browser.send_json({"type": "request", "id": request_id,
                                      "kind": kind, "params": projected})

    async def upstream(self):
        while True:
            frame = await self.gateway.events.get()
            if frame is None:
                raise GatewayError("gateway_disconnected")
            async with self.serial:
                if frame.get("method") != "event":
                    await self.server_request(frame)
                    continue
                event = frame.get("params", {})
                if event.get("session_id") != self.session_id:
                    continue
                kind, payload = event.get("type"), event.get("payload") or {}
                output = None
                if kind == "message.start":
                    self.running = True
                elif kind == "message.delta":
                    output = {"type": "token", "text": text(payload.get("text"))}
                elif kind == "message.interim":
                    output = {"type": "interim", "text": text(payload.get("text")),
                              "already_streamed": bool(payload.get("already_streamed"))}
                elif kind == "message.complete":
                    self.running = False
                    status = payload.get("status")
                    if status not in {"complete", "error", "interrupted"}:
                        status = "error"  # Missing/unknown terminal status is not success.
                    if payload.get("partial") or payload.get("error"):
                        status = "error"
                    # Native failed-turn text can be provider-detail fallback
                    # copy, not model output. Only explicit partial answers are
                    # safe to retain; the UI already has any streamed partial.
                    answer = text(payload.get("text"))
                    if status == "error" and payload.get("partial") is not True:
                        answer = ""
                    output = {"type": "done", "message": answer, "status": status}
                elif kind in {"tool.start", "tool.complete"}:
                    output = {"type": "step_start" if kind == "tool.start" else "step_end",
                              "id": text(payload.get("tool_id"), 200),
                              "tool": text(payload.get("name"), 200)}
                    if kind == "tool.start":
                        output["input"] = text(json.dumps(payload.get("args", {})), 8192)
                    else:
                        output["output"] = text(payload.get("summary") or payload.get("result_text")
                                                or json.dumps(payload.get("result")), 8192)
                elif kind == "request.cancel":
                    request_id = payload.get("id")
                    if request_id in self.requests:
                        self.requests.pop(request_id)
                        output = {"type": "request_cancel", "id": request_id}
                elif kind == "error":
                    self.running = False
                    output = {**error_frame("gateway_request_failed"), "terminal": True}
                if output:
                    await self.browser.send_json(output)

    async def submit(self, frame):
        if set(frame) != {"type", "id", "message"}:
            raise GatewayError("invalid_request")
        request_id, message = frame["id"], frame["message"]
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise GatewayError("invalid_request") from None
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise GatewayError("invalid_request")
        digest = hashlib.sha256(message.encode()).hexdigest()
        receipt = self.state["submissions"].get(request_id)
        if receipt:
            if receipt["digest"] != digest:
                raise GatewayError("invalid_request")
            if receipt["status"] != "accepted":
                raise GatewayError("uncertain_send")
            await self.browser.send_json({"type": "accepted", "id": request_id})
            return
        if self.running:
            raise GatewayError("turn_running")
        if len(self.state["submissions"]) >= 500:
            raise GatewayError("thread_full")
        # Record uncertainty before crossing the wire. Never retry an ambiguous RPC.
        self.state["submissions"][request_id] = {"digest": digest, "status": "sending"}
        self.store.save(self.state)
        self.running = True
        await self.gateway.call("prompt.submit", self.params(text=message))
        self.state["submissions"][request_id]["status"] = "accepted"
        self.store.save(self.state)
        await self.browser.send_json({"type": "accepted", "id": request_id})

    async def answer(self, frame):
        if set(frame) != {"type", "id", "answer"} or not isinstance(frame["id"], str):
            raise GatewayError("invalid_request")
        request_id, answer = frame["id"], frame["answer"]
        request = self.requests.get(request_id)
        if not request:
            raise GatewayError("request_expired")
        if not isinstance(answer, dict):
            raise GatewayError("invalid_request")
        if request["kind"] == "approval":
            if (set(answer) != {"choice"} or answer["choice"] not in {"once", "deny"}
                    or answer["choice"] not in request["params"]["choices"]):
                raise GatewayError("invalid_request")
        else:
            questions = request["params"].get("questions")
            if questions:
                values = answer.get("answers")
                qids = {q["qid"] for q in questions}
                if (set(answer) != {"answers"} or not isinstance(values, dict)
                        or not set(values) <= qids
                        or any(not isinstance(v, str) or len(v) > 4000 for v in values.values())):
                    raise GatewayError("invalid_request")
            elif (set(answer) != {"answer"} or not isinstance(answer["answer"], str)
                  or len(answer["answer"]) > 4000):
                raise GatewayError("invalid_request")
        await self.gateway.answer(request_id, answer)
        self.requests.pop(request_id)
        await self.browser.send_json({"type": "request_cancel", "id": request_id})

    async def downstream(self):
        while True:
            raw = await self.browser.receive_text()
            if len(raw) > 65536:
                raise GatewayError("invalid_request")
            try:
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise GatewayError("invalid_request")
                async with self.serial:
                    kind = frame.get("type")
                    if kind == "submit":
                        await self.submit(frame)
                    elif kind == "answer":
                        await self.answer(frame)
                    elif kind == "stop" and set(frame) == {"type"}:
                        await self.gateway.call("session.interrupt", self.params())
                    else:
                        raise GatewayError("invalid_request")
            except json.JSONDecodeError:
                await self.browser.send_json(error_frame("invalid_request"))
            except GatewayError as exc:
                await self.browser.send_json(error_frame(str(exc)))
                if str(exc) == "gateway_request_failed":
                    raise

    async def run(self):
        await self.attach()
        tasks = [asyncio.create_task(self.upstream()), asyncio.create_task(self.downstream())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve(browser, settings, thread_id):
    await browser.accept()
    try:
        store = ThreadStore(settings, thread_id)
        with store.locked() as state:
            async with Gateway(settings) as gateway:
                await Bridge(browser, settings, store, state, gateway).run()
    except WebSocketDisconnect:
        pass  # Disconnect detaches; the Hermes turn continues and can be resumed.
    except Exception as exc:
        code = str(exc) if isinstance(exc, GatewayError) else "gateway_unavailable"
        try:
            await browser.send_json(error_frame(code))
            await browser.close(code=4409 if code == "thread_in_use" else 1011)
        except (RuntimeError, WebSocketDisconnect):
            pass
