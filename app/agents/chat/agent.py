"""Streaming and checkpoint helpers for the chat agent."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import get_settings

from .graph import CHAT_RECURSION_LIMIT, build_chat_graph

MAX_STEP_OUTPUT = 900

_thread_locks: dict[str, asyncio.Lock] = {}


def _thread_lock(thread_id: str) -> asyncio.Lock:
    lock = _thread_locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _thread_locks[thread_id] = lock
    return lock


def _lock_has_waiters(lock: asyncio.Lock) -> bool:
    return bool(getattr(lock, "_waiters", None))


def _discard_thread_lock(thread_id: str, lock: asyncio.Lock) -> None:
    if not lock.locked() and not _lock_has_waiters(lock) and _thread_locks.get(thread_id) is lock:
        _thread_locks.pop(thread_id, None)


def checkpoint_path() -> Path:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.data_dir / settings.chat_checkpoint_db


@asynccontextmanager
async def _checkpointer(path: Path | None = None):
    target = path or checkpoint_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(target)) as saver:
        await saver.setup()
        yield saver


def _text(value: Any) -> str:
    content = getattr(value, "content", value)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(content or "")


def _truncate(value: Any, limit: int = MAX_STEP_OUTPUT) -> str:
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _tool_message_output(value: Any) -> str:
    if isinstance(value, ToolMessage):
        return _truncate(value.content)
    return _truncate(value)


def _final_ai_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            return _text(message)
    return ""


def _has_tool_calls(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        if value.get("tool_calls"):
            return True
        return any(_has_tool_calls(value.get(key)) for key in ("message", "chunk", "output", "generations"))
    if isinstance(value, (list, tuple)):
        return any(_has_tool_calls(item) for item in value)
    if getattr(value, "tool_calls", None):
        return True
    return any(_has_tool_calls(getattr(value, attr, None)) for attr in ("message", "chunk", "output", "generations"))


def messages_for_template(messages: list[Any]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    pending_steps: list[dict[str, Any]] = []

    for message in messages:
        if isinstance(message, HumanMessage):
            rendered.append({"role": "user", "content": _text(message), "steps": []})
            pending_calls = {}
            pending_steps = []
            continue

        if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
            for call in message.tool_calls:
                pending_calls[call["id"]] = {
                    "tool": call["name"],
                    "input": call.get("args", {}),
                    "output": "",
                }
            continue

        if isinstance(message, ToolMessage):
            step = pending_calls.get(message.tool_call_id, {"tool": message.name or "tool", "input": {}})
            pending_steps.append({**step, "output": _tool_message_output(message)})
            continue

        if isinstance(message, AIMessage):
            rendered.append({"role": "assistant", "content": _text(message), "steps": pending_steps})
            pending_calls = {}
            pending_steps = []

    return rendered


async def load_thread_messages(db_path: str, thread_id: str) -> list[dict[str, Any]]:
    async with _checkpointer() as saver:
        graph = build_chat_graph(db_path, thread_id=thread_id, checkpointer=saver)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": CHAT_RECURSION_LIMIT}
        state = await graph.aget_state(config)
        return messages_for_template(list(state.values.get("messages", [])))


def _event_error(exc: BaseException) -> dict[str, str]:
    message = str(exc).strip() or type(exc).__name__
    if "recursion limit" in message.lower():
        message = "The chat agent hit its tool-step limit before finishing. Try a narrower question."
    return {"type": "error", "message": message}


async def stream_chat(
    db_path: str,
    thread_id: str,
    user_message: str,
    *,
    llm: Any | None = None,
    llm_factory: Any | None = None,
    warm_after_s: float = 2.0,
) -> AsyncIterator[dict[str, Any]]:
    """Yield typed chat events for one user turn."""
    user_message = user_message.strip()
    if not user_message:
        yield {"type": "error", "message": "Message is required."}
        return

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    token_parts: list[str] = []
    final_text = ""
    model_runs_with_tokens: set[str] = set()

    async def worker() -> None:
        nonlocal final_text
        lock = _thread_lock(thread_id)
        try:
            async with lock:
                async with _checkpointer() as saver:
                    graph = build_chat_graph(
                        db_path,
                        thread_id=thread_id,
                        llm=llm,
                        llm_factory=llm_factory,
                        checkpointer=saver,
                    )
                    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": CHAT_RECURSION_LIMIT}
                    inputs = {"messages": [HumanMessage(content=user_message)]}
                    async for event in graph.astream_events(inputs, config=config, version="v2"):
                        name = event.get("name")
                        data = event.get("data") or {}
                        event_type = event.get("event")
                        run_id = event.get("run_id")

                        if event_type == "on_tool_start":
                            await queue.put(
                                {
                                    "type": "step_start",
                                    "id": run_id,
                                    "tool": name,
                                    "input": data.get("input") or {},
                                }
                            )
                            continue

                        if event_type == "on_tool_end":
                            await queue.put(
                                {
                                    "type": "step_end",
                                    "id": run_id,
                                    "tool": name,
                                    "output": _tool_message_output(data.get("output")),
                                }
                            )
                            continue

                        if event_type == "on_chat_model_stream":
                            chunk_text = _text(data.get("chunk"))
                            if chunk_text:
                                if run_id:
                                    model_runs_with_tokens.add(str(run_id))
                                token_parts.append(chunk_text)
                                await queue.put({"type": "token", "text": chunk_text})
                            continue

                        if event_type == "on_chat_model_end":
                            if run_id and str(run_id) in model_runs_with_tokens and _has_tool_calls(data.get("output")):
                                token_parts.clear()
                                await queue.put({"type": "token_reset"})
                            continue

                        if event_type == "on_chain_end" and name == "LangGraph":
                            output = data.get("output") or {}
                            final_text = _final_ai_text(list(output.get("messages", [])))

                    if final_text and not token_parts:
                        token_parts.append(final_text)
                        await queue.put({"type": "token", "text": final_text})
                    await queue.put({"type": "done", "message": final_text or "".join(token_parts)})
        except BaseException as exc:
            await queue.put(_event_error(exc))
        finally:
            _discard_thread_lock(thread_id, lock)
            await queue.put(None)

    task = asyncio.create_task(worker())
    warmed = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=warm_after_s if not warmed else None)
            except asyncio.TimeoutError:
                warmed = True
                yield {"type": "warming"}
                continue
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
