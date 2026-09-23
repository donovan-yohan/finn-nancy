from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr
from websockets.asyncio.server import serve

from app.hermes_chat.bridge import Bridge, snapshot
from app.hermes_chat.threads import ThreadStore
from app.hermes_chat.transport import Gateway, GatewayError, gateway_uri


def settings(tmp_path, **overrides):
    return SimpleNamespace(data_dir=tmp_path, hermes_chat_url="ws://127.0.0.1:19119/api/ws",
                           hermes_chat_profile="synthetic-finance", hermes_chat_token=SecretStr("synthetic-token"),
                           hermes_chat_token_file=None, **overrides)


@pytest.mark.parametrize("url", [
    "wss://127.0.0.1:80/api/ws", "ws://localhost:80/api/ws", "ws://example.com:80/api/ws",
    "ws://127.1:80/api/ws", "ws://127.0.0.1/api/ws", "ws://127.0.0.1:0/api/ws",
    "ws://127.0.0.1:65536/api/ws", "ws://127.0.0.1:80/admin", "ws://127.0.0.1:80/api/ws?x=y",
    "ws://127.0.0.1:80/api/ws#fragment", "ws://" + "user@127.0.0.1:80/api/ws",
])
def test_gateway_rejects_remote_or_ambiguous_targets(tmp_path, url):
    config = settings(tmp_path)
    config.hermes_chat_url = url
    with pytest.raises(GatewayError, match="not_configured"):
        gateway_uri(config)


def test_gateway_uses_server_only_credential(tmp_path):
    config = settings(tmp_path)
    assert gateway_uri(config).endswith("?token=synthetic-token")
    config.hermes_chat_token = SecretStr("")
    with pytest.raises(GatewayError):
        gateway_uri(config)
    config.hermes_chat_token_file = tmp_path / "credential"
    config.hermes_chat_token_file.write_text("synthetic-file-token")
    assert gateway_uri(config).endswith("?token=synthetic-file-token")


def test_gateway_real_websocket_rpc_and_requests(tmp_path):
    async def run():
        calls = []

        async def peer(socket):
            await socket.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "gateway.ready", "payload": {}, "session_id": None}}))
            async for raw in socket:
                frame = json.loads(raw)
                calls.append(frame)
                if frame.get("method"):
                    await socket.send(json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": {"ok": True}}))
                    if frame["method"] == "session.create":
                        await socket.send(json.dumps({"jsonrpc": "2.0", "id": "srq-1", "method": "clarify",
                                                      "params": {"session_id": "runtime", "question": "Which month?"}}))
        async with serve(peer, "127.0.0.1", 0) as server:
            config = settings(tmp_path)
            config.hermes_chat_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/ws"
            async with Gateway(config) as gateway:
                assert await gateway.call("session.create", {}) == {"ok": True}
                assert (await gateway.events.get())["method"] == "clarify"
                await gateway.answer("srq-1", {"answer": "2026-01"})
        assert calls[0]["method"] == "client.capabilities"
        assert calls[0]["params"] == {"server_requests": True}
        assert calls[-1]["result"] == {"answer": "2026-01"}
    asyncio.run(run())


def test_thread_mapping_is_private_locked_and_target_bound(tmp_path):
    config = settings(tmp_path)
    store = ThreadStore(config, uuid4().hex)
    store.create()
    assert store.path.stat().st_mode & 0o777 == 0o600
    with store.locked():
        with pytest.raises(GatewayError, match="thread_in_use"):
            with store.locked():
                pass
    config.hermes_chat_profile = "another-profile"
    with pytest.raises(GatewayError, match="invalid_thread"):
        ThreadStore(config, store.path.stem).load()
    with pytest.raises(GatewayError, match="invalid_thread"):
        ThreadStore(config, "../../state")


class Browser:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


class Peer:
    def __init__(self):
        self.calls = []
        self.answers = []
        self.events = asyncio.Queue()
        self.fail = False

    async def call(self, method, params):
        self.calls.append((method, params))
        if self.fail:
            raise GatewayError("gateway_request_failed")
        return {"status": "streaming"}

    async def answer(self, request_id, result):
        self.answers.append((request_id, result))


def make_bridge(tmp_path):
    config = settings(tmp_path)
    store = ThreadStore(config, uuid4().hex)
    store.create()
    bridge = Bridge(Browser(), config, store, store.load(), Peer())
    bridge.session_id = "owned-runtime"
    return bridge


def test_submit_replay_is_idempotent_and_binds_exact_message(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        frame = {"type": "submit", "id": str(uuid4()), "message": "Synthetic question"}
        await bridge.submit(frame)
        await bridge.submit(frame)
        assert len(bridge.gateway.calls) == 1
        assert bridge.store.load()["submissions"][frame["id"]]["status"] == "accepted"
        assert "Synthetic question" not in bridge.store.path.read_text()
        with pytest.raises(GatewayError, match="invalid_request"):
            await bridge.submit({**frame, "message": "different"})
        with pytest.raises(GatewayError, match="invalid_request"):
            await bridge.submit({**frame, "session_id": "foreign"})
    asyncio.run(run())


def test_uncertain_submission_is_not_replayed_after_restart(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        bridge.gateway.fail = True
        frame = {"type": "submit", "id": str(uuid4()), "message": "Synthetic question"}
        with pytest.raises(GatewayError, match="gateway_request_failed"):
            await bridge.submit(frame)
        restarted = Bridge(Browser(), bridge.settings, bridge.store, bridge.store.load(), Peer())
        with pytest.raises(GatewayError, match="uncertain_send"):
            await restarted.submit(frame)
        assert restarted.gateway.calls == []
    asyncio.run(run())


def test_approval_requires_matching_request_and_cannot_persist_authority(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        await bridge.server_request({"id": "srq-1", "method": "approval", "params": {
            "session_id": "owned-runtime", "command": "synthetic action", "choices": ["once", "always", "session", "deny"]}})
        assert bridge.browser.frames[-1]["params"]["choices"] == ["once", "deny"]
        for choice in ("always", "session"):
            with pytest.raises(GatewayError, match="invalid_request"):
                await bridge.answer({"type": "answer", "id": "srq-1", "answer": {"choice": choice}})
        with pytest.raises(GatewayError, match="request_expired"):
            await bridge.answer({"type": "answer", "id": "foreign", "answer": {"choice": "once"}})
        await bridge.answer({"type": "answer", "id": "srq-1", "answer": {"choice": "once"}})
        assert bridge.gateway.answers == [("srq-1", {"choice": "once"})]
        with pytest.raises(GatewayError, match="request_expired"):
            await bridge.answer({"type": "answer", "id": "srq-1", "answer": {"choice": "once"}})
    asyncio.run(run())


@pytest.mark.parametrize("choices", [None, [], ["once"], ["always", "session"]],
                         ids=["omitted", "empty", "once-only", "permanent-only"])
def test_approval_always_allows_deny_without_broadening_grants(tmp_path, choices):
    async def run():
        bridge = make_bridge(tmp_path)
        params = {"session_id": "owned-runtime", "command": "synthetic action"}
        if choices is not None:
            params["choices"] = choices
        await bridge.server_request({"id": "srq-deny", "method": "approval", "params": params})
        expected = ["once", "deny"] if choices == ["once"] else ["deny"]
        assert bridge.browser.frames[-1]["params"]["choices"] == expected
        for choice in {"once", "always", "session"} - set(expected):
            with pytest.raises(GatewayError, match="invalid_request"):
                await bridge.answer({"type": "answer", "id": "srq-deny", "answer": {"choice": choice}})
        with pytest.raises(GatewayError, match="invalid_request"):
            await bridge.answer({"type": "answer", "id": "srq-deny",
                                 "answer": {"choice": "deny", "extra": True}})
        assert bridge.gateway.answers == []
        await bridge.answer({"type": "answer", "id": "srq-deny", "answer": {"choice": "deny"}})
        assert bridge.gateway.answers == [("srq-deny", {"choice": "deny"})]
        assert bridge.browser.frames[-1] == {"type": "request_cancel", "id": "srq-deny"}
        with pytest.raises(GatewayError, match="request_expired"):
            await bridge.answer({"type": "answer", "id": "srq-deny", "answer": {"choice": "deny"}})
        if "once" in expected:
            await bridge.server_request({"id": "srq-once", "method": "approval", "params": params})
            await bridge.answer({"type": "answer", "id": "srq-once", "answer": {"choice": "once"}})
            assert bridge.gateway.answers[-1] == ("srq-once", {"choice": "once"})
    asyncio.run(run())


def test_unsupported_secret_request_fails_fast_without_disclosure(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        await bridge.server_request({"id": "srq-1", "method": "secret", "params": {
            "session_id": "owned-runtime", "prompt": "private synthetic secret prompt"}})
        await bridge.server_request({"id": "srq-2", "method": "clarify", "params": {
            "session_id": "foreign", "question": "private foreign question"}})
        assert bridge.gateway.answers == [("srq-1", None)]
        assert bridge.browser.frames == []
    asyncio.run(run())


def test_snapshot_omits_hidden_and_tool_rows():
    output = snapshot({"messages": [{"role": "user", "content": "hello"},
                                     {"role": "user", "content": "internal", "display_kind": "hidden"},
                                     {"role": "tool", "content": "raw output"}],
                       "inflight": {"user": "question", "assistant": "partial", "streaming": True}})
    assert output["messages"] == [{"role": "user", "content": "hello"}]
    assert output["inflight"]["assistant"] == "partial"
    assert output["running"] is True


def test_snapshot_expires_absent_requests_without_disclosing_foreign_prompts():
    output = snapshot({"session_id": "owned", "open_requests": [
        {"id": "current", "method": "clarify", "params": {"session_id": "owned"}},
        {"id": "foreign", "method": "clarify", "params": {"session_id": "other"}},
        {"id": "secret", "method": "secret", "params": {"session_id": "owned"}},
    ]})
    assert output["open_request_ids"] == ["current"]
    assert snapshot({})["open_request_ids"] == []


def test_native_error_settles_turn_and_marks_browser_terminal(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        bridge.running = True
        await bridge.gateway.events.put({"method": "event", "params": {
            "session_id": "owned-runtime", "type": "error",
            "payload": {"message": "private upstream body"}}})
        await bridge.gateway.events.put(None)
        with pytest.raises(GatewayError):
            await bridge.upstream()
        assert bridge.running is False
        assert bridge.browser.frames[-1]["terminal"] is True
        assert "private upstream body" not in json.dumps(bridge.browser.frames)
    asyncio.run(run())


@pytest.mark.parametrize("status,partial", [("error", False), ("complete", False), (None, False), ("error", True)])
def test_failed_completion_never_projects_native_error_copy(tmp_path, status, partial):
    async def run():
        bridge = make_bridge(tmp_path)
        private = "synthetic-private-provider-detail"
        answer = "Genuine partial assistant answer." if partial else "Details: " + private
        await bridge.gateway.events.put({"method": "event", "params": {
            "session_id": "owned-runtime", "type": "message.complete",
            "payload": {"status": status, "text": answer, "error": private,
                        "partial": partial, "rendered": private, "error_surface": {"provider": private}}}})
        await bridge.gateway.events.put(None)
        with pytest.raises(GatewayError):
            await bridge.upstream()
        assert private not in json.dumps(bridge.browser.frames)
        output = bridge.browser.frames[-1]
        assert output["status"] == "error"
        assert output["message"] == (answer if partial else "")
    asyncio.run(run())


def test_failed_reconnect_keeps_partial_answer_not_native_error_details():
    private = "synthetic-private-provider-detail"
    result = snapshot({"inflight": {"user": "Synthetic question", "assistant": "Partial answer",
                                   "streaming": False, "status": "error", "error": private,
                                   "error_surface": {"provider": private}}})
    assert private not in json.dumps(result)
    assert result["inflight"]["assistant"] == "Partial answer"
    assert result["inflight"]["error"] == "Hermes could not finish this reply."
    assert result["running"] is False


def test_complete_reply_never_silently_drops_final_caveat(tmp_path):
    async def run():
        bridge = make_bridge(tmp_path)
        answer = 'x' * 32768 + '\nImportant final accounting caveat.'
        await bridge.gateway.events.put({'method': 'event', 'params': {
            'type': 'message.complete', 'session_id': 'owned-runtime',
            'payload': {'status': 'complete', 'text': answer}}})
        await bridge.gateway.events.put(None)
        with pytest.raises(GatewayError):
            await bridge.upstream()
        assert bridge.browser.frames[-1]['message'] == answer
        assert snapshot({'messages': [{'role': 'assistant', 'text': answer}]})['messages'][0]['content'] == answer
    asyncio.run(run())
