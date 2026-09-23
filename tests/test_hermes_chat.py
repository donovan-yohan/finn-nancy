"""Synthetic native-gateway contract tests; no operator profile or database."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from app.web.app import create_app


def test_chat_socket_requires_same_origin_and_server_minted_cookie(app_env):
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    client.get("/chat")
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("ws://127.0.0.1/chat/socket", headers={"origin": "https://other.example"}):
            pass
    assert exc.value.code == 4403


def test_bridge_creates_and_resumes_hermes_not_langgraph(app_env, monkeypatch):
    from app.hermes_chat import bridge
    from app.config import get_settings

    monkeypatch.setenv("HERMES_CHAT_URL", "ws://127.0.0.1:19119/api/ws")
    get_settings.cache_clear()
    calls = []

    class FakeGateway:
        def __init__(self, settings):
            self.events = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def recent_history(self, stored_session_id):
            assert stored_session_id == "stored"
            return {"history_session_id": "stored", "messages": [{"role": "user", "content": "hello"},
                                 {"role": "assistant", "content": "synthetic answer"}],
                    "history_window": {"status": "recent", "limit": 20, "returned": 2}}

        async def call(self, method, params):
            calls.append((method, params))
            if method in {"session.create", "session.resume"}:
                return {"session_id": "runtime", "stored_session_id": "stored",
                        "messages": [], "messages_omitted": method == "session.resume",
                        "info": {"running": False}}
            if method == "prompt.submit":
                for kind, payload in [("message.delta", {"text": "synthetic answer"}),
                                      ("message.complete", {"text": "synthetic answer", "status": "complete"})]:
                    await self.events.put({"method": "event", "params": {
                        "type": kind, "session_id": "runtime", "payload": payload}})
                return {"status": "streaming"}
            return {}

    monkeypatch.setattr(bridge, "Gateway", FakeGateway)
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    client.get("/chat")
    with client.websocket_connect("ws://127.0.0.1/chat/socket", headers={"origin": "http://127.0.0.1"}) as socket:
        assert socket.receive_json()["type"] == "snapshot"
        socket.send_json({"type": "submit", "id": str(uuid4()), "message": "hello"})
        received = [socket.receive_json() for _ in range(3)]
        assert {row["type"] for row in received} == {"accepted", "token", "done"}
        assert next(row for row in received if row["type"] == "done")["message"] == "synthetic answer"
    with client.websocket_connect("ws://127.0.0.1/chat/socket", headers={"origin": "http://127.0.0.1"}) as socket:
        snapshot = socket.receive_json()
        assert snapshot["messages"][-1]["content"] == "synthetic answer"
    assert [method for method, _ in calls] == ["session.create", "prompt.submit", "session.resume"]
    assert calls[-1][1]["session_id"] == "stored"
    assert all(params.get("profile") == "namako-finance" for _, params in calls)
    assert "runtime" not in json.dumps(received)
