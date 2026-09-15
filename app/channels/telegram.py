"""Telegram capture channel using raw Bot API long polling.

The bot fails closed: a token without TELEGRAM_CHAT_ID will not process any
message. Network operations live behind TelegramTransport so tests can drive the
handler with an in-memory fake transport.

Polling is at-least-once with bounded poison-update dead-lettering: an update is
acknowledged only after it is handled successfully, transient handler failures
leave the offset unchanged for Telegram redelivery, and the same update is
dead-lettered after three failures. Offset is in-memory; ungraceful restarts may
replay recent updates, and capture sha-dedupe makes those replays harmless aside
from a duplicate note to the chat.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings
from ..db import engine, repo_captures
from ..ingest.storage import capture

logger = logging.getLogger(__name__)


class ThirdPartyTransportBlocked(RuntimeError):
    """Telegram egress is disabled by strict-local mode or missing consent."""


@dataclass
class TelegramRuntimeState:
    configured: bool = False
    allowed: bool = False
    running: bool = False

    def snapshot(self) -> dict[str, bool]:
        return {
            "configured": self.configured,
            "allowed": self.allowed,
            "running": self.running,
        }


def telegram_credentials_configured(settings=None) -> bool:
    settings = settings or get_settings()
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def telegram_transport_allowed(db_path=None) -> bool:
    settings = get_settings()
    if settings.strict_local_mode:
        return False
    try:
        with engine.read_conn(db_path or settings.db_path) as conn:
            return repo_captures.transport_allowed(
                conn,
                "telegram",
                strict_local_mode=settings.strict_local_mode,
            )
    except Exception:  # noqa: BLE001 - third-party policy must fail closed
        return False


def _require_telegram_transport_allowed(db_path=None) -> None:
    if not telegram_transport_allowed(db_path):
        raise ThirdPartyTransportBlocked(
            "Telegram is blocked until strict-local mode is disabled and "
            "the current disclosure is consented"
        )


class TelegramTransport:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._client = client
        self._own_client = client is None
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._file_url = f"https://api.telegram.org/file/bot{token}"

    async def __aenter__(self) -> "TelegramTransport":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("TelegramTransport must be used as an async context manager")
        return self._client

    async def get_updates(self, *, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        _require_telegram_transport_allowed()
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            params["offset"] = offset
        resp = await self.client.get(f"{self._base_url}/getUpdates", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram getUpdates failed")
        return list(data.get("result") or [])

    async def get_file(self, file_id: str) -> dict[str, Any]:
        _require_telegram_transport_allowed()
        resp = await self.client.get(f"{self._base_url}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError("Telegram getFile failed")
        return dict(data.get("result") or {})

    async def download(self, file_path: str) -> bytes:
        _require_telegram_transport_allowed()
        resp = await self.client.get(f"{self._file_url}/{file_path}")
        resp.raise_for_status()
        return bytes(resp.content)

    async def send_message(self, chat_id: str | int, text: str) -> None:
        _require_telegram_transport_allowed()
        resp = await self.client.post(
            f"{self._base_url}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        resp.raise_for_status()


def _redact_token(text: str) -> str:
    """Strip the bot token from error text (httpx errors embed the request URL)."""
    token = get_settings().telegram_bot_token
    return text.replace(token, "***") if token else text


def _message(update: dict[str, Any]) -> dict[str, Any] | None:
    raw = update.get("message")
    return raw if isinstance(raw, dict) else None


def _chat_id(message: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    return str(chat.get("id") or "")


def _authorized(message: dict[str, Any], allowed_chat_id: str) -> bool:
    chat_id = _chat_id(message)
    allowed = str(allowed_chat_id or "").strip()
    if not allowed:
        logger.warning("dropping Telegram update because TELEGRAM_CHAT_ID is not configured")
        return False
    if chat_id != allowed:
        logger.warning("dropping unauthorized Telegram update")
        return False
    return True


def _photo_file(message: dict[str, Any]) -> tuple[str, str, str | None, str] | None:
    photos = message.get("photo") or []
    if not photos:
        return None
    photo = max(
        photos,
        key=lambda item: (
            int(item.get("file_size") or 0),
            int(item.get("width") or 0) * int(item.get("height") or 0),
        ),
    )
    file_id = str(photo["file_id"])
    unique = str(photo.get("file_unique_id") or file_id)
    return file_id, f"telegram-photo-{unique}.jpg", "image/jpeg", unique


def _document_file(message: dict[str, Any]) -> tuple[str, str, str | None, str] | None:
    document = message.get("document")
    if not isinstance(document, dict):
        return None
    file_id = str(document["file_id"])
    name = str(document.get("file_name") or f"telegram-document-{document.get('file_unique_id') or file_id}")
    unique = str(document.get("file_unique_id") or file_id)
    return file_id, Path(name).name, document.get("mime_type"), unique


def _money(cents: int | None) -> str:
    value = abs(int(cents or 0))
    return f"${value // 100:,}.{value % 100:02d}"


def _document_result(db_path, source_document_id: int, job_id: int) -> dict[str, Any]:
    with engine.read_conn(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        doc = conn.execute("SELECT * FROM source_documents WHERE id=?", (source_document_id,)).fetchone()
        txn = conn.execute(
            """
            SELECT
              t.id,
              t.counterparty,
              t.description,
              t.amount_cents,
              c.name AS category_name
            FROM transactions t
            LEFT JOIN transaction_splits ts ON ts.transaction_id = t.id
            LEFT JOIN categories c ON c.id = ts.category_id
            WHERE t.source_document_id = ?
            ORDER BY t.id DESC
            LIMIT 1
            """,
            (source_document_id,),
        ).fetchone()
    return {
        "job_status": job["status"] if job else None,
        "doc_status": doc["status"] if doc else None,
        "transaction": dict(txn) if txn else None,
    }


async def _wait_for_reply_text(
    db_path,
    *,
    source_document_id: int,
    job_id: int,
    wait_seconds: float,
    poll_seconds: float,
) -> str:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        result = await asyncio.to_thread(_document_result, db_path, source_document_id, job_id)
        txn = result["transaction"]
        if result["job_status"] == "done":
            if txn is not None:
                merchant = txn.get("counterparty") or txn.get("description") or "receipt"
                category = txn.get("category_name") or "uncategorized"
                return f"Classified: {merchant} · {category} · {_money(txn.get('amount_cents'))}"
            return "staged for review"
        if result["doc_status"] == "needs_review":
            return "staged for review"
        if result["job_status"] in {"dead", "error"}:
            return "staged for review"
        if asyncio.get_running_loop().time() >= deadline:
            return "still processing, check the app"
        await asyncio.sleep(poll_seconds)


async def handle_update(
    update: dict[str, Any],
    transport,
    *,
    db_path=None,
    allowed_chat_id: str | None = None,
    wait_seconds: float = 30.0,
    poll_seconds: float = 1.0,
) -> dict[str, Any]:
    """Handle one Telegram update without owning network or process lifecycle."""
    settings = get_settings()
    db = db_path or settings.db_path
    allowed = settings.telegram_chat_id if allowed_chat_id is None else allowed_chat_id
    message = _message(update)
    if message is None:
        return {"status": "ignored", "reason": "no_message"}
    chat_id = _chat_id(message)
    if not _authorized(message, allowed):
        return {"status": "ignored", "reason": "unauthorized"}

    file_info = _document_file(message) or _photo_file(message)
    if file_info is None:
        await transport.send_message(
            chat_id,
            "Send a receipt photo, image, or PDF document and I will add it to finn-nancy.",
        )
        return {"status": "ack"}

    file_id, original_name, mime, file_unique_id = file_info
    remote_file = await transport.get_file(file_id)
    file_path = str(remote_file.get("file_path") or "")
    if not file_path:
        await transport.send_message(chat_id, "staged for review")
        return {"status": "missing_file_path"}
    raw = await transport.download(file_path)
    captured = await asyncio.to_thread(
        capture,
        raw=raw,
        original_name=original_name,
        channel="telegram",
        declared_mime=mime,
        client_capture_id=repo_captures.stable_origin_id(
            "telegram",
            f"{int(update.get('update_id') or 0)}:{file_unique_id}",
        ),
        source_metadata={"source": "telegram"},
    )
    if captured["status"] == "duplicate":
        await transport.send_message(chat_id, "duplicate upload already captured")
        return captured

    reply = await _wait_for_reply_text(
        db,
        source_document_id=int(captured["source_document_id"]),
        job_id=int(captured["job_id"]),
        wait_seconds=wait_seconds,
        poll_seconds=poll_seconds,
    )
    await transport.send_message(chat_id, reply)
    return {**captured, "reply": reply}


async def run_poller(
    stop: asyncio.Event,
    *,
    transport=None,
    poll_timeout: int = 25,
    policy_poll_seconds: float = 1.0,
    runtime_state: TelegramRuntimeState | None = None,
) -> None:
    settings = get_settings()
    state = runtime_state or TelegramRuntimeState()
    state.configured = telegram_credentials_configured(settings)
    if not state.configured:
        state.allowed = False
        state.running = False
        logger.info("Telegram poller disabled because credentials are incomplete")
        return
    offset: int | None = None
    try:
        if transport is not None:
            await _poll_loop(
                stop,
                transport,
                offset=offset,
                poll_timeout=poll_timeout,
                enforce_policy=True,
                policy_poll_seconds=policy_poll_seconds,
                db_path=settings.db_path,
                runtime_state=state,
            )
            return

        async with TelegramTransport(settings.telegram_bot_token) as live_transport:
            await _poll_loop(
                stop,
                live_transport,
                offset=offset,
                poll_timeout=poll_timeout,
                enforce_policy=True,
                policy_poll_seconds=policy_poll_seconds,
                db_path=settings.db_path,
                runtime_state=state,
            )
    finally:
        state.running = False
        state.allowed = telegram_transport_allowed(settings.db_path)


async def _poll_loop(
    stop: asyncio.Event,
    transport,
    *,
    offset: int | None,
    poll_timeout: int,
    backoff_seconds: float = 5,
    enforce_policy: bool = True,
    policy_poll_seconds: float = 1.0,
    db_path=None,
    runtime_state: TelegramRuntimeState | None = None,
) -> None:
    failures: dict[int, int] = {}
    while not stop.is_set():
        if enforce_policy:
            allowed = telegram_transport_allowed(db_path)
            if runtime_state is not None:
                runtime_state.allowed = allowed
                runtime_state.running = allowed
            if not allowed:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=policy_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                continue
        try:
            updates = await transport.get_updates(offset=offset, timeout=poll_timeout)
        except asyncio.CancelledError:
            raise
        except ThirdPartyTransportBlocked:
            if runtime_state is not None:
                runtime_state.allowed = False
                runtime_state.running = False
            try:
                await asyncio.wait_for(stop.wait(), timeout=policy_poll_seconds)
            except asyncio.TimeoutError:
                pass
            continue
        except Exception as exc:  # noqa: BLE001 - keep the poller alive
            logger.warning("Telegram poller error: %s", _redact_token(str(exc)))
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff_seconds)
            except asyncio.TimeoutError:
                pass
            continue

        should_backoff = False
        policy_blocked = False
        for update in updates:
            update_id = int(update.get("update_id") or 0)
            if enforce_policy and not telegram_transport_allowed(db_path):
                if runtime_state is not None:
                    runtime_state.allowed = False
                    runtime_state.running = False
                policy_blocked = True
                break
            try:
                await handle_update(update, transport)
            except asyncio.CancelledError:
                raise
            except ThirdPartyTransportBlocked:
                if runtime_state is not None:
                    runtime_state.allowed = False
                    runtime_state.running = False
                policy_blocked = True
                break
            except Exception as exc:  # noqa: BLE001 - retry handler failures at least once
                count = failures.get(update_id, 0) + 1
                failures[update_id] = count
                redacted_error = _redact_token(str(exc))
                if count >= 3:
                    logger.warning(
                        "dead-lettering Telegram update %s after %d failures: %s",
                        update_id,
                        count,
                        redacted_error,
                    )
                    offset = max(offset or 0, update_id + 1)
                    failures.pop(update_id, None)
                else:
                    logger.warning(
                        "Telegram update %s failed (%d/3): %s",
                        update_id,
                        count,
                        redacted_error,
                    )
                should_backoff = True
                break

            failures.pop(update_id, None)
            offset = max(offset or 0, update_id + 1)

        if should_backoff:
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff_seconds)
            except asyncio.TimeoutError:
                pass
        elif policy_blocked:
            try:
                await asyncio.wait_for(stop.wait(), timeout=policy_poll_seconds)
            except asyncio.TimeoutError:
                pass
