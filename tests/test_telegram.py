from __future__ import annotations

import asyncio
import logging

import pytest

from app.db import engine, repo_captures


class FakeTelegramTransport:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.replies: list[tuple[str, str]] = []
        self.downloads = 0

    async def get_file(self, file_id: str):
        return {"file_path": f"photos/{file_id}.jpg"}

    async def download(self, file_path: str) -> bytes:
        self.downloads += 1
        return self.raw

    async def send_message(self, chat_id, text: str) -> None:
        self.replies.append((str(chat_id), text))


def _photo_update(chat_id: str, file_id: str = "file-1", update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "photo": [
                {
                    "file_id": file_id,
                    "file_unique_id": f"uniq-{file_id}",
                    "width": 200,
                    "height": 200,
                    "file_size": 4000,
                }
            ],
        },
    }


def test_strict_local_blocks_poll_get_file_download_and_send_before_egress(
    app_env, monkeypatch
):
    from app.channels.telegram import (
        TelegramTransport,
        ThirdPartyTransportBlocked,
        run_poller,
    )
    from app.config import get_settings

    monkeypatch.setenv("STRICT_LOCAL_MODE", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    get_settings.cache_clear()

    class TrapClient:
        calls = 0

        async def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("network get must not run")

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("network post must not run")

    class TrapPoller:
        calls = 0

        async def get_updates(self, **_kwargs):
            self.calls += 1
            raise AssertionError("polling must not run")

    trap_poller = TrapPoller()
    asyncio.run(run_poller(asyncio.Event(), transport=trap_poller, poll_timeout=0))
    assert trap_poller.calls == 0

    client = TrapClient()
    transport = TelegramTransport("synthetic-token", client=client)
    calls = (
        transport.get_updates(offset=None, timeout=0),
        transport.get_file("synthetic-file"),
        transport.download("synthetic/path"),
        transport.send_message("synthetic-chat", "synthetic reply"),
    )
    for call in calls:
        with pytest.raises(ThirdPartyTransportBlocked):
            asyncio.run(call)
    assert client.calls == 0


def test_poller_begins_after_consent_is_recorded_post_start(app_env, monkeypatch):
    from app.channels.telegram import TelegramRuntimeState, run_poller
    from app.config import get_settings

    monkeypatch.setenv("STRICT_LOCAL_MODE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    get_settings.cache_clear()

    class ConsentAwareTransport:
        calls = 0

        async def get_updates(self, **_kwargs):
            self.calls += 1
            stop.set()
            return []

    async def drive():
        state = TelegramRuntimeState()
        transport = ConsentAwareTransport()
        task = asyncio.create_task(
            run_poller(
                stop,
                transport=transport,
                poll_timeout=0,
                policy_poll_seconds=0.01,
                runtime_state=state,
            )
        )
        await asyncio.sleep(0.04)
        assert transport.calls == 0
        assert state.snapshot() == {
            "configured": True,
            "allowed": False,
            "running": False,
        }
        with engine.write_tx(app_env) as conn:
            repo_captures.record_transport_decision(
                conn, transport="telegram", decision="consented"
            )
        await asyncio.wait_for(task, timeout=1)
        return state, transport

    stop = asyncio.Event()
    state, transport = asyncio.run(drive())
    assert transport.calls == 1
    assert state.configured is True
    assert state.allowed is True
    assert state.running is False


def test_revoke_after_poll_blocks_update_before_next_egress(app_env, monkeypatch):
    from app.channels.telegram import TelegramRuntimeState, run_poller
    from app.config import get_settings

    monkeypatch.setenv("STRICT_LOCAL_MODE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    get_settings.cache_clear()
    with engine.write_tx(app_env) as conn:
        repo_captures.record_transport_decision(
            conn, transport="telegram", decision="consented"
        )

    class RevokingTransport:
        poll_calls = 0
        file_calls = 0

        async def get_updates(self, **_kwargs):
            self.poll_calls += 1
            with engine.write_tx(app_env) as conn:
                repo_captures.record_transport_decision(
                    conn, transport="telegram", decision="revoked"
                )
            return [_photo_update("synthetic-chat")]

        async def get_file(self, _file_id):
            self.file_calls += 1
            raise AssertionError("revoked transport must not look up a file")

    async def drive():
        state = TelegramRuntimeState()
        transport = RevokingTransport()
        task = asyncio.create_task(
            run_poller(
                stop,
                transport=transport,
                poll_timeout=0,
                policy_poll_seconds=0.01,
                runtime_state=state,
            )
        )
        await asyncio.sleep(0.08)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        return state, transport

    stop = asyncio.Event()
    state, transport = asyncio.run(drive())
    assert transport.poll_calls == 1
    assert transport.file_calls == 0
    assert state.allowed is False
    assert state.running is False


def test_missing_telegram_credentials_never_starts_policy_runtime(
    app_env, monkeypatch
):
    from app.channels.telegram import TelegramRuntimeState, run_poller
    from app.config import get_settings

    monkeypatch.setenv("STRICT_LOCAL_MODE", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    get_settings.cache_clear()

    class TrapTransport:
        calls = 0

        async def get_updates(self, **_kwargs):
            self.calls += 1
            raise AssertionError("incomplete credentials must not poll")

    state = TelegramRuntimeState()
    transport = TrapTransport()
    asyncio.run(
        run_poller(
            asyncio.Event(),
            transport=transport,
            poll_timeout=0,
            policy_poll_seconds=0.01,
            runtime_state=state,
        )
    )
    assert transport.calls == 0
    assert state.snapshot() == {
        "configured": False,
        "allowed": False,
        "running": False,
    }


def test_chat_id_filtering_drops_unauthorized_and_empty_allowed(app_env, make_jpeg, monkeypatch):
    from app.channels.telegram import handle_update
    from app.config import get_settings

    transport = FakeTelegramTransport(make_jpeg())
    allowed_chat = str(id(transport))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", allowed_chat)
    get_settings.cache_clear()

    result = asyncio.run(
        handle_update(
            _photo_update(chat_id=str(id(transport) + 1)),
            transport,
            db_path=app_env,
            wait_seconds=0,
        )
    )
    assert result["status"] == "ignored"
    assert transport.replies == []
    assert transport.downloads == 0

    result = asyncio.run(
        handle_update(
            _photo_update(chat_id=allowed_chat),
            transport,
            db_path=app_env,
            allowed_chat_id="",
            wait_seconds=0,
        )
    )
    assert result["status"] == "ignored"
    assert transport.replies == []
    assert transport.downloads == 0

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 0


def test_allowed_photo_captures_and_replies(app_env, make_jpeg, monkeypatch):
    from app.channels.telegram import handle_update
    from app.config import get_settings

    transport = FakeTelegramTransport(make_jpeg())
    allowed_chat = str(id(transport))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", allowed_chat)
    get_settings.cache_clear()

    result = asyncio.run(
        handle_update(_photo_update(chat_id=allowed_chat), transport, db_path=app_env, wait_seconds=0)
    )
    assert result["status"] == "staged"
    assert transport.replies == [(allowed_chat, "still processing, check the app")]

    with engine.read_conn(app_env) as conn:
        doc = conn.execute("SELECT * FROM source_documents").fetchone()
        job = conn.execute("SELECT * FROM jobs WHERE type='ingest_document'").fetchone()
        provenance = conn.execute("SELECT * FROM capture_provenance").fetchone()
    assert doc is not None
    assert doc["kind"] == "receipt"
    assert job is not None
    assert job["source_document_id"] == doc["id"]
    assert provenance["channel"] == "telegram"
    assert provenance["transport_class"] == "third_party"


def test_resending_same_bytes_gets_duplicate_reply(app_env, make_jpeg, monkeypatch):
    from app.channels.telegram import handle_update
    from app.config import get_settings

    transport = FakeTelegramTransport(make_jpeg())
    allowed_chat = str(id(transport))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", allowed_chat)
    get_settings.cache_clear()

    first = asyncio.run(
        handle_update(
            _photo_update(chat_id=allowed_chat, file_id="first"),
            transport,
            db_path=app_env,
            wait_seconds=0,
        )
    )
    second = asyncio.run(
        handle_update(
            _photo_update(chat_id=allowed_chat, file_id="second"),
            transport,
            db_path=app_env,
            wait_seconds=0,
        )
    )
    assert first["status"] == "staged"
    assert second["status"] == "duplicate"
    assert transport.replies[-1] == (allowed_chat, "duplicate upload already captured")

    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='ingest_document'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM capture_provenance").fetchone()[0] == 2


def test_poll_loop_retries_update_after_transient_handler_failure(app_env, make_jpeg, monkeypatch):
    from app.channels.telegram import _poll_loop
    from app.config import get_settings

    allowed_chat = "4242"
    monkeypatch.setenv("TELEGRAM_CHAT_ID", allowed_chat)
    get_settings.cache_clear()
    stop = asyncio.Event()

    async def quick_reply(*_args, **_kwargs):
        return "still processing, check the app"

    monkeypatch.setattr("app.channels.telegram._wait_for_reply_text", quick_reply)

    class RetryOnceTransport(FakeTelegramTransport):
        def __init__(self, raw: bytes):
            super().__init__(raw)
            self.offsets: list[int | None] = []
            self.get_file_calls = 0
            self.update = _photo_update(allowed_chat, file_id="retry", update_id=1)

        async def get_updates(self, *, offset: int | None, timeout: int = 25):
            self.offsets.append(offset)
            if offset == 2:
                stop.set()
                return []
            return [self.update]

        async def get_file(self, file_id: str):
            self.get_file_calls += 1
            if self.get_file_calls == 1:
                raise RuntimeError("temporary getFile failure")
            return await super().get_file(file_id)

    transport = RetryOnceTransport(make_jpeg())

    asyncio.run(
        _poll_loop(
            stop,
            transport,
            offset=None,
            poll_timeout=0,
            backoff_seconds=0,
            enforce_policy=False,
        )
    )

    assert transport.offsets == [None, None, 2]
    assert transport.get_file_calls == 2
    assert transport.replies == [(allowed_chat, "still processing, check the app")]
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1


def test_poll_loop_dead_letters_poison_update_and_continues(
    app_env,
    make_jpeg,
    monkeypatch,
    caplog,
):
    from app.channels.telegram import _poll_loop
    from app.config import get_settings

    allowed_chat = "9999"
    monkeypatch.setenv("TELEGRAM_CHAT_ID", allowed_chat)
    get_settings.cache_clear()
    stop = asyncio.Event()

    async def quick_reply(*_args, **_kwargs):
        return "still processing, check the app"

    monkeypatch.setattr("app.channels.telegram._wait_for_reply_text", quick_reply)

    class PoisonThenNextTransport(FakeTelegramTransport):
        def __init__(self, raw: bytes):
            super().__init__(raw)
            self.offsets: list[int | None] = []
            self.poison = _photo_update(allowed_chat, file_id="poison", update_id=1)
            self.following = _photo_update(allowed_chat, file_id="following", update_id=2)

        async def get_updates(self, *, offset: int | None, timeout: int = 25):
            self.offsets.append(offset)
            if offset is None:
                return [self.poison, self.following]
            if offset == 2:
                return [self.following]
            if offset == 3:
                stop.set()
                return []
            return []

        async def get_file(self, file_id: str):
            if file_id == "poison":
                raise RuntimeError("permanent getFile failure")
            return await super().get_file(file_id)

    transport = PoisonThenNextTransport(make_jpeg())
    caplog.set_level(logging.WARNING, logger="app.channels.telegram")

    asyncio.run(
        _poll_loop(
            stop,
            transport,
            offset=None,
            poll_timeout=0,
            backoff_seconds=0,
            enforce_policy=False,
        )
    )

    assert transport.offsets == [None, None, None, 2, 3]
    assert "dead-lettering Telegram update 1 after 3 failures" in caplog.text
    assert transport.replies == [(allowed_chat, "still processing, check the app")]
    with engine.read_conn(app_env) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
