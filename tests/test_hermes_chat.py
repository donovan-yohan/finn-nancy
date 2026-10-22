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


@pytest.mark.parametrize("terminal", ["disconnect", "gateway_error", "cancel", "pending_submit"])
def test_bridge_teardown_drains_tasks_and_gateway_before_unlocking(tmp_path, monkeypatch, terminal):
    import anyio
    from anyio.lowlevel import checkpoint
    from types import SimpleNamespace
    from app.hermes_chat import bridge
    from app.hermes_chat.threads import ThreadStore
    from app.hermes_chat.transport import GatewayError

    async def run():
        config = SimpleNamespace(data_dir=tmp_path, hermes_chat_url="ws://127.0.0.1:19119/api/ws",
                                 hermes_chat_profile="synthetic-finance")
        submission_id = str(uuid4())
        thread_id = uuid4().hex
        store = ThreadStore(config, thread_id)
        store.create()
        ready = anyio.Event()
        teardown = anyio.Event()
        release = anyio.Event()
        owner: anyio.CancelScope | None = None
        calls = []
        closed = []
        tasks = []
        errors = []

        class Events(asyncio.Queue):
            async def get(self):
                tasks.append(asyncio.current_task())
                ready.set()
                try:
                    return await super().get()
                finally:
                    teardown.set()
                    # Model asynchronous transport cleanup, not a timing sleep.
                    with anyio.CancelScope(shield=True):
                        await release.wait()

        class FakeGateway:
            def __init__(self, settings):
                self.events = Events()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                # The mapping must remain locked until gateway teardown completes.
                with pytest.raises(GatewayError, match="^thread_in_use$"):
                    with store.locked():
                        pass
                await checkpoint()
                closed.append(True)

            async def call(self, method, params):
                calls.append(method)
                if method == "prompt.submit":
                    assert owner is not None
                    owner.cancel()
                    await anyio.sleep_forever()
                assert method == "session.create"
                return {"session_id": "runtime", "stored_session_id": "stored", "messages": []}

        class Browser:
            def __init__(self):
                self.frames = []
                self.closes = []

            async def accept(self):
                pass

            async def send_json(self, frame):
                self.frames.append(frame)

            async def close(self, code):
                self.closes.append(code)

            async def receive_text(self):
                tasks.append(asyncio.current_task())
                await ready.wait()
                if terminal == "pending_submit":
                    return json.dumps({"type": "submit", "id": submission_id, "message": "synthetic"})
                if terminal == "gateway_error":
                    raise GatewayError("gateway_disconnected")
                if terminal == "cancel":
                    assert owner is not None
                    owner.cancel()
                    await anyio.sleep_forever()
                raise WebSocketDisconnect(1000)

        monkeypatch.setattr(bridge, "Gateway", FakeGateway)
        browser = Browser()

        async def connect():
            nonlocal owner
            with anyio.CancelScope() as scope:
                owner = scope
                try:
                    await bridge.serve(browser, config, thread_id)
                except BaseException as exc:
                    errors.append(exc)
                    raise

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(connect)
                await teardown.wait()
                with pytest.raises(GatewayError, match="^thread_in_use$"):
                    with store.locked():
                        pass
                # Deliver a real outer cancellation while task draining is pending.
                # On gateway failure, retain that failure as the terminal UI result.
                if terminal != "gateway_error":
                    assert owner is not None
                    owner.cancel()
                release.set()

        assert closed == [True]
        assert len(tasks) == 2 and all(task.done() for task in tasks)
        assert not any(isinstance(exc, Exception) for exc in errors)
        if terminal in {"cancel", "pending_submit"}:
            assert len(errors) == 1 and isinstance(errors[0], asyncio.CancelledError)
        with store.locked() as state:
            assert state["stored_session_id"] == "stored"
            if terminal == "pending_submit":
                assert state["submissions"][submission_id]["status"] == "sending"
            else:
                assert state["submissions"] == {}
        expected_calls = ["session.create"] + (["prompt.submit"] if terminal == "pending_submit" else [])
        assert calls == expected_calls  # No implicit interrupt or resend.
        if terminal == "pending_submit":
            resumed = bridge.Bridge(browser, config, store, store.load(), FakeGateway(config))
            with pytest.raises(GatewayError, match="^uncertain_send$"):
                await resumed.submit({"type": "submit", "id": submission_id, "message": "synthetic"})
            assert calls == expected_calls
        if terminal == "gateway_error":
            assert browser.frames[-1]["code"] == "gateway_disconnected"
            assert browser.closes == [1011]
        else:
            assert [frame["type"] for frame in browser.frames] == ["snapshot"]
            assert browser.closes == []

    anyio.run(run)
