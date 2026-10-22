"""Local synthetic HTTP/WS peers; never access a Hermes profile or finance DB."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import SecretStr
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from app.hermes_chat.bridge import Bridge, snapshot
from app.hermes_chat.threads import ThreadStore
from app.hermes_chat.transport import Gateway, GatewayError


def test_history_disclosure_projects_only_safe_window_fields():
    window = {"status": "recent", "limit": 20, "returned": 3}
    frame = snapshot({"history_window": {**window, "token": "synthetic secret", "session_id": "private"}})
    assert frame["history_window"] == window


class Browser:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


class NativePeer:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []
        self.http = []
        self.live = {"running": False, "inflight": None, "open_requests": []}
        self.http_status = 200
        self.http_body = None
        self.http_delay = 0
        self.headers = {}
        self.omit_supported = True
        self.omit_marker = True
        self.resume_error = None
        self.after_history = None
        self.history_session_id = "stored"
        self.resume_session_key = "stored"

    async def request(self, connection, request):
        if urlsplit(request.path).path == "/api/ws":
            return None
        self.http.append(request)
        await asyncio.sleep(self.http_delay)
        query = parse_qs(urlsplit(request.path).query)
        limit = int(query.get("limit", [500])[0])
        messages = self.messages[-limit:]
        body = self.http_body if self.http_body is not None else json.dumps({
            "session_id": self.history_session_id, "profile": "synthetic-finance", "messages": messages,
            "pagination": {"limit": limit, "offset": 0, "order": "latest", "returned": len(messages)},
        }).encode()
        if self.after_history:
            self.after_history()
        return Response(self.http_status, "Synthetic", Headers({
            "Content-Type": "application/json", "Content-Length": str(len(body)), **self.headers,
        }), body)

    async def socket(self, socket):
        await socket.send(json.dumps({"method": "event", "params": {"type": "gateway.ready"}}))
        try:
            async for raw in socket:
                frame = json.loads(raw)
                self.calls.append(frame)
                result = {"ok": True}
                if frame.get("method") == "session.resume":
                    if self.resume_error:
                        await socket.send(json.dumps({"id": frame["id"], "error": self.resume_error}))
                        continue
                    omit = frame["params"].get("omit_messages") and self.omit_supported
                    result = {"session_id": "runtime", "session_key": self.resume_session_key,
                              "messages": [] if omit else self.messages,
                              "messages_omitted": bool(omit), "message_count": len(self.messages),
                              "info": {}, **self.live}
                    if not self.omit_marker:
                        result.pop("messages_omitted")
                await socket.send(json.dumps({"id": frame["id"], "result": result}))
        except ConnectionClosed:
            pass


@asynccontextmanager
async def connected(tmp_path, peer):
    async with serve(peer.socket, "127.0.0.1", 0, process_request=peer.request) as server:
        config = SimpleNamespace(
            data_dir=tmp_path, hermes_chat_profile="synthetic-finance",
            hermes_chat_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/ws",
            hermes_chat_token=SecretStr("synthetic-token"), hermes_chat_token_file=None,
        )
        store = ThreadStore(config, uuid4().hex)
        store.create()
        state = store.load()
        state["stored_session_id"] = "stored"
        store.save(state)
        async with Gateway(config) as gateway:
            yield Bridge(Browser(), config, store, state, gateway)


def test_resume_recovers_real_over_two_mib_conversation_with_bounded_history(tmp_path):
    async def run():
        rows = [{"role": role, "content": f"{turn}:" + "x" * 16000}
                for turn in range(70) for role in ("user", "assistant")]
        assert len(json.dumps(rows).encode()) > 2 * 1024 * 1024
        peer = NativePeer(rows)
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            frame = bridge.browser.frames[0]
            assert frame["messages"] == rows[-20:]
            assert frame["history_window"] == {"status": "recent", "limit": 20, "returned": 20}
            assert "stored" not in json.dumps(frame)
            assert peer.calls[-1]["params"]["omit_messages"] is True
            assert len(peer.http) == 1
            request = peer.http[0]
            assert urlsplit(request.path).path == "/api/sessions/stored/messages"
            assert parse_qs(urlsplit(request.path).query) == {
                "profile": ["synthetic-finance"], "limit": ["20"], "offset": ["0"], "order": ["latest"],
            }
            assert request.headers["X-Hermes-Session-Token"] == "synthetic-token"
            assert "token=" not in request.path
    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 404, 500])
def test_history_failure_disclosed_without_blocking_live_recovery(tmp_path, status):
    async def run():
        peer = NativePeer([])
        peer.http_status = status
        peer.live = {"running": True, "inflight": {"user": "latest", "assistant": "partial", "streaming": True},
                     "open_requests": [{"id": "fresh", "method": "clarify", "params": {
                         "session_id": "runtime", "question": "Choose?"}}]}
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            snapshot = bridge.browser.frames[0]
            assert snapshot["history_window"] == {"status": "unavailable", "limit": 20, "returned": 0}
            assert snapshot["messages"] == []
            assert snapshot["running"] is True
            assert snapshot["inflight"]["assistant"] == "partial"
            assert snapshot["open_request_ids"] == ["fresh"]
    asyncio.run(run())


@pytest.mark.parametrize("variant", ["foreign-profile", "foreign-session", "old-api", "too-many",
                                     "wrong-order", "wrong-count", "invalid-row", "invalid-json", "compressed"])
def test_untrusted_history_page_is_not_projected(tmp_path, variant):
    async def run():
        peer = NativePeer([])
        page = {"session_id": "stored", "profile": "synthetic-finance",
                "messages": [{"role": "assistant", "content": "must not be disclosed"}],
                "pagination": {"limit": 20, "offset": 0, "order": "latest", "returned": 1}}
        if variant == "foreign-profile":
            page["profile"] = "foreign"
        elif variant == "foreign-session":
            page["session_id"] = "foreign"
        elif variant == "old-api":
            page.pop("pagination")
        elif variant == "too-many":
            page["messages"] *= 21
            page["pagination"]["returned"] = 21
        elif variant == "wrong-order":
            page["pagination"]["order"] = "oldest"
        elif variant == "wrong-count":
            page["pagination"]["returned"] = 2
        elif variant == "invalid-row":
            page["messages"] = ["not an object"]
        elif variant == "compressed":
            peer.headers["Content-Encoding"] = "gzip"
        peer.http_body = b"not json" if variant == "invalid-json" else json.dumps(page).encode()
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            frame = bridge.browser.frames[0]
            assert frame["history_window"]["status"] == "unavailable"
            assert frame["messages"] == []
            assert "must not be disclosed" not in json.dumps(frame)
    asyncio.run(run())


@pytest.mark.parametrize("stored", ["../other", "x?profile=foreign", "x/../../api/admin", "https://other.invalid", "", None])
def test_history_target_must_be_exact_server_minted_id(tmp_path, stored):
    async def run():
        peer = NativePeer([])
        async with connected(tmp_path, peer) as bridge:
            with pytest.raises(GatewayError, match="invalid_thread"):
                await bridge.gateway.recent_history(stored)
            assert peer.http == []
    asyncio.run(run())


def test_old_resume_interface_is_refused_without_unbounded_fallback(tmp_path):
    async def run():
        peer = NativePeer([{"role": "assistant", "content": "small old transcript"}])
        peer.omit_supported = False
        async with connected(tmp_path, peer) as bridge:
            with pytest.raises(GatewayError, match="gateway_request_failed"):
                await bridge.attach()
            assert bridge.browser.frames == []
            resumes = [call for call in peer.calls if call["method"] == "session.resume"]
            assert len(resumes) == 1
            assert resumes[0]["params"]["omit_messages"] is True
    asyncio.run(run())


def test_rest_history_uses_native_display_projection_not_physical_wrapper(tmp_path):
    async def run():
        peer = NativePeer([
            {"role": "user", "content": "private wrapper", "display_content": "Visible question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "hidden seed", "display_kind": "hidden"},
            {"role": "tool", "content": "tool output"},
        ])
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            assert bridge.browser.frames[0]["messages"] == [
                {"role": "user", "content": "Visible question"}, {"role": "assistant", "content": "answer"},
            ]
            assert bridge.browser.frames[0]["history_window"]["returned"] == 4
    asyncio.run(run())


def test_missing_empty_session_reports_new_chat_without_recreating_or_replaying(tmp_path):
    async def run():
        peer = NativePeer([])
        peer.http_status = 404
        peer.resume_error = {"code": 4007, "message": "synthetic private session not found"}
        async with connected(tmp_path, peer) as bridge:
            before = bridge.store.load()
            with pytest.raises(GatewayError, match="^invalid_thread$"):
                await bridge.attach()
            assert bridge.browser.frames == []
            assert bridge.store.load() == before
            assert [call["method"] for call in peer.calls] == ["client.capabilities", "session.resume"]
    asyncio.run(run())


def test_history_compression_tip_must_match_fresh_native_resume(tmp_path):
    async def run():
        peer = NativePeer([{"role": "assistant", "content": "Compressed continuation"}])
        peer.history_session_id = peer.resume_session_key = "compression-tip"
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            frame = bridge.browser.frames[0]
            assert frame["history_window"]["status"] == "recent"
            assert frame["messages"] == peer.messages
            assert "compression-tip" not in json.dumps(frame)
            assert bridge.store.load()["stored_session_id"] == "stored"
            assert urlsplit(peer.http[0].path).path == "/api/sessions/stored/messages"
    asyncio.run(run())


def test_live_snapshot_and_requests_are_fresh_after_history_io(tmp_path):
    async def run():
        peer = NativePeer([])
        peer.live = {"running": True, "inflight": {"user": "old", "assistant": "old partial"},
                     "open_requests": [{"id": "expired", "method": "clarify", "params": {
                         "session_id": "runtime", "question": "expired question"}}]}

        def advance():
            peer.live = {"running": True, "inflight": {"user": "new", "assistant": "fresh", "streaming": True},
                         "open_requests": [{"id": "fresh", "method": "clarify", "params": {
                             "session_id": "runtime", "question": "current question"}}]}
        peer.after_history = advance
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            frame = bridge.browser.frames[0]
            assert frame["inflight"]["assistant"] == "fresh"
            assert frame["open_request_ids"] == ["fresh"]
            assert bridge.browser.frames[1]["id"] == "fresh"
            assert "expired" not in json.dumps(bridge.browser.frames)
    asyncio.run(run())


@pytest.mark.parametrize("redirect", [False, True])
def test_history_never_uses_proxy_environment_or_follows_redirects(tmp_path, monkeypatch, redirect):
    async def run():
        trap = NativePeer([])
        async with serve(trap.socket, "127.0.0.1", 0, process_request=trap.request) as server:
            trap_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                monkeypatch.setenv(name, trap_url)
            for name in ("NO_PROXY", "no_proxy"):
                monkeypatch.setenv(name, "")
            peer = NativePeer([])
            if redirect:
                peer.http_status = 302
                peer.headers["Location"] = trap_url + "/api/sessions/stored/messages"
            async with connected(tmp_path, peer) as bridge:
                await bridge.attach()
                assert bridge.browser.frames[0]["history_window"]["status"] == ("unavailable" if redirect else "recent")
                assert len(peer.http) == 1
                assert trap.http == []
                assert trap.calls == []
    asyncio.run(run())


def test_history_response_byte_limit_does_not_truncate_into_a_valid_answer(tmp_path):
    async def run():
        peer = NativePeer([{"role": "assistant", "content": "x" * (2 * 1024 * 1024)}])
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            frame = bridge.browser.frames[0]
            assert frame["messages"] == []
            assert frame["history_window"]["status"] == "unavailable"
            assert frame["running"] is False
    asyncio.run(run())


def test_browser_cannot_page_history_or_select_native_authority(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    async def run():
        peer = NativePeer([])
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            frames = iter([
                {"type": "history", "offset": 20},
                {"type": "history", "profile": "foreign", "session_id": "foreign", "url": "http://127.0.0.1:9/admin"},
                {"type": "stop", "session_id": "foreign"},
                {"type": "submit", "id": str(uuid4()), "message": "synthetic", "profile": "foreign"},
            ])

            async def receive_text():
                frame = next(frames, None)
                if frame is None:
                    raise WebSocketDisconnect()
                return json.dumps(frame)
            bridge.browser.receive_text = receive_text
            with pytest.raises(WebSocketDisconnect):
                await bridge.downstream()
            assert [frame["code"] for frame in bridge.browser.frames[1:]] == ["invalid_request"] * 4
            assert len(peer.http) == 1
            assert [call["method"] for call in peer.calls] == ["client.capabilities", "session.resume"]
    asyncio.run(run())


def test_history_deadline_degrades_to_live_state(tmp_path, monkeypatch):
    from app.hermes_chat import transport

    async def run():
        peer = NativePeer([])
        peer.http_delay = 0.15
        monkeypatch.setattr(transport, "HISTORY_TIMEOUT", 0.05)
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            assert bridge.browser.frames[0]["history_window"]["status"] == "unavailable"
            assert peer.calls[-1]["method"] == "session.resume"
    asyncio.run(run())


def test_native_unpersisted_empty_resume_without_omission_marker(tmp_path):
    async def run():
        peer = NativePeer([])
        peer.http_status = 404
        peer.omit_marker = False  # _resume_live_unpersisted does not return this marker.
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            assert bridge.browser.frames[0]["history_window"]["status"] == "unavailable"
            assert bridge.browser.frames[0]["messages"] == []
            assert bridge.session_id == "runtime"
    asyncio.run(run())


def test_empty_persisted_session_is_a_recent_empty_window_not_a_full_history_claim(tmp_path):
    async def run():
        peer = NativePeer([])
        async with connected(tmp_path, peer) as bridge:
            await bridge.attach()
            frame = bridge.browser.frames[0]
            assert frame["history_window"] == {"status": "recent", "limit": 20, "returned": 0}
            assert frame["messages"] == []
    asyncio.run(run())


def _history_settings(url):
    return SimpleNamespace(
        hermes_chat_profile="synthetic-finance", hermes_chat_url=url,
        hermes_chat_token=SecretStr("synthetic-token"), hermes_chat_token_file=None,
    )


@asynccontextmanager
async def _chunked_history_peer(body):
    peer = SimpleNamespace(requests=[], tasks=[], writers=[], release=asyncio.Event())
    peer.release.set()

    async def raw_peer(reader, writer):
        try:
            peer.requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            await peer.release.wait()
            for offset in range(0, len(body), 65536):
                chunk = body[offset:offset + 65536]
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            await reader.read()  # Keep ownership until the client closes.
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def accept(reader, writer):
        peer.writers.append(writer)
        peer.tasks.append(asyncio.create_task(raw_peer(reader, writer)))

    peer.server = await asyncio.start_server(accept, "127.0.0.1", 0)
    peer.url = f"ws://127.0.0.1:{peer.server.sockets[0].getsockname()[1]}/api/ws"
    try:
        yield peer
    finally:
        # Server.__aexit__ alone doesn't retire accepted handler tasks. Cleanup
        # must run even when a body-boundary assertion fails.
        peer.server.close()
        for writer in peer.writers:
            writer.close()
        for task in peer.tasks:
            task.cancel()
        await asyncio.gather(*peer.tasks, return_exceptions=True)
        await asyncio.gather(*(writer.wait_closed() for writer in peer.writers), return_exceptions=True)
        await peer.server.wait_closed()


@pytest.mark.parametrize("oversized", [False, True])
def test_chunked_history_is_bounded_without_content_length(monkeypatch, oversized):
    from app.hermes_chat import transport

    async def run():
        # Valid JSON would succeed if the streaming byte cap were removed;
        # malformed filler could fail at parsing and mask that regression.
        page = {
            "session_id": "stored", "profile": "synthetic-finance",
            "messages": [{"role": "assistant", "content": ""}],
            "pagination": {"limit": 20, "offset": 0, "order": "latest", "returned": 1},
        }
        content_size = (2 * transport.HISTORY_MAX_BYTES if oversized else
                        transport.HISTORY_MAX_BYTES - len(json.dumps(page).encode()))
        page["messages"][0]["content"] = "x" * content_size
        body = json.dumps(page).encode()
        assert (len(body) > transport.HISTORY_MAX_BYTES if oversized else
                len(body) == transport.HISTORY_MAX_BYTES)
        received, responses = [], []
        original_raw = transport.httpx.Response.aiter_raw

        async def observed_raw(response, *args, **kwargs):
            responses.append(response)
            async for chunk in original_raw(response, *args, **kwargs):
                received.append(len(chunk))
                yield chunk

        monkeypatch.setattr(transport.httpx.Response, "aiter_raw", observed_raw)
        async with _chunked_history_peer(body) as peer:
            gateway = Gateway(_history_settings(peer.url))
            if oversized:
                with pytest.raises(GatewayError, match="^history_unavailable$"):
                    await gateway.recent_history("stored")
                assert sum(received[:-1]) <= transport.HISTORY_MAX_BYTES < sum(received)
            else:
                result = await gateway.recent_history("stored")
                assert result["messages"][0]["content"] == page["messages"][0]["content"]
                assert result["history_window"] == {"status": "recent", "limit": 20, "returned": 1}
                assert sum(received) == transport.HISTORY_MAX_BYTES
            assert len(peer.requests) == len(responses) == 1
            assert responses[0].headers["transfer-encoding"] == "chunked"
            assert "content-length" not in responses[0].headers
            assert responses[0].is_closed
        assert all(task.done() for task in peer.tasks)
        assert all(writer.is_closing() for writer in peer.writers)
    asyncio.run(run())


def test_chunked_history_peer_closes_on_assertion_failure():
    async def run():
        with pytest.raises(AssertionError, match="synthetic assertion failure"):
            async with _chunked_history_peer(b"synthetic") as peer:
                peer.release.clear()  # Hold the handler at a known await.
                reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(peer.url).port)
                try:
                    writer.write(b"GET /synthetic HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    await writer.drain()
                    await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
                    assert len(peer.tasks) == 1 and not peer.tasks[0].done()
                    raise AssertionError("synthetic assertion failure")
                finally:
                    writer.close()
                    await writer.wait_closed()
        assert peer.tasks[0].cancelled()
        assert all(writer.is_closing() for writer in peer.writers)
        assert not peer.server.is_serving()
        assert not peer.server.sockets
    asyncio.run(run())


def test_history_total_deadline_cancels_progressing_body(monkeypatch):
    import httpx
    from app.hermes_chat import transport

    async def run():
        progressed, release = asyncio.Event(), asyncio.Event()
        deadlines, requests, responses, clients = [], [], [], []
        original_client = httpx.AsyncClient

        @asynccontextmanager
        async def controlled_deadline(delay):
            assert delay == 0.06
            # Use the real cancellation/TimeoutError context, but arm it only
            # at the body barrier. Host scheduling and synchronous TLS setup
            # are not an upper wall-clock contract of asyncio.timeout.
            async with asyncio.timeout(None) as deadline:
                deadlines.append(deadline)
                yield deadline

        class Trickle(httpx.AsyncByteStream):
            chunks = 0
            cancelled = closed = False

            async def __aiter__(self):
                try:
                    while not release.is_set():
                        self.chunks += 1
                        yield b" "
                        if self.chunks == 8:
                            progressed.set()
                        await asyncio.sleep(0)  # Progress never waits for a read timeout.
                    # A valid response makes a missing total deadline fail by
                    # success, rather than by an unrelated parse/read timeout.
                    yield json.dumps({
                        "session_id": "stored", "profile": "synthetic-finance", "messages": [],
                        "pagination": {"limit": 20, "offset": 0, "order": "latest", "returned": 0},
                    }).encode()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

            async def aclose(self):
                self.closed = True

        body = Trickle()

        async def respond(request):
            requests.append(request)
            response = httpx.Response(200, headers={"Transfer-Encoding": "chunked"}, stream=body)
            responses.append(response)
            return response

        def client(**kwargs):
            instance = original_client(**kwargs, transport=httpx.MockTransport(respond))
            clients.append(instance)
            return instance

        monkeypatch.setattr(transport, "HISTORY_TIMEOUT", 0.06)
        monkeypatch.setattr(transport, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": controlled_deadline}))
        monkeypatch.setattr(httpx, "AsyncClient", client)
        gateway = Gateway(_history_settings("ws://127.0.0.1:1/api/ws"))
        task = asyncio.create_task(gateway.recent_history("stored"))
        try:
            await asyncio.wait_for(progressed.wait(), 1)
            if deadlines:
                deadlines[0].reschedule(asyncio.get_running_loop().time())
            else:
                release.set()  # Missing-context mutation must finish, not hang.
            with pytest.raises(GatewayError, match="^history_unavailable$"):
                await asyncio.wait_for(task, 1)
            assert len(deadlines) == 1 and deadlines[0].expired()
            assert body.chunks >= 8 and body.cancelled and body.closed
            assert len(requests) == len(responses) == len(clients) == 1
            assert clients[0].is_closed and responses[0].is_closed
            assert requests[0].extensions["timeout"]["read"] == 0.06
            assert "content-length" not in responses[0].headers
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
