from __future__ import annotations

import asyncio
import json
import os
import subprocess
import textwrap
from collections.abc import Callable, Iterable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.config import get_settings
from app.llm.client import make_llm
from app.llm.gate import llm_gate
from app.llm.warm import warm

PINNED_MODEL = "qwen3.8-27b-unleashed"


def make_pinned_llm():
    settings = get_settings()
    if settings.chat_model != PINNED_MODEL:
        raise RuntimeError(
            f"CHAT_MODEL must stay pinned to {PINNED_MODEL!r}; got {settings.chat_model!r}"
        )
    return make_llm(temperature=0.0)


def run_with_gate(fn: Callable[[], Any]) -> Any:
    async def _run() -> Any:
        async with llm_gate():
            return await asyncio.to_thread(fn)

    return asyncio.run(_run())


def warm_pinned_model() -> bool:
    async def _run() -> bool:
        async with llm_gate():
            return await warm(make_pinned_llm())

    return asyncio.run(_run())


def curl_models_preflight() -> tuple[bool, str]:
    cmd = (
        "set -a; source .env; set +a; "
        'curl -sS --max-time 30 -H "Authorization: Bearer ${LOCAL_LLM_KEY}" '
        '"${LOCAL_LLM_BASE%/}/models"'
    )
    proc = subprocess.run(
        ["bash", "-lc", cmd],
        check=False,
        capture_output=True,
        text=True,
        timeout=40,
    )
    if proc.returncode != 0:
        return False, trim_text(proc.stderr or proc.stdout)
    try:
        payload = json.loads(proc.stdout)
        model_ids = [item.get("id") for item in payload.get("data", [])]
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not parse /models JSON: {exc}; body={trim_text(proc.stdout)}"
    return PINNED_MODEL in model_ids, f"models={model_ids}"


def messages_from_state(state: dict[str, Any]) -> list[BaseMessage]:
    return list(state.get("messages", []))


def final_text(state: dict[str, Any]) -> str:
    for message in reversed(messages_from_state(state)):
        if isinstance(message, AIMessage) and message.content:
            return content_to_text(message.content)
    return ""


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text") or block.get("content")
                if value:
                    parts.append(str(value))
        return "\n".join(parts)
    return str(content)


def tool_call_names(messages: Iterable[BaseMessage]) -> list[str]:
    names: list[str] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                names.append(str(call.get("name")))
    return names


def tool_result_texts(messages: Iterable[BaseMessage]) -> list[str]:
    return [content_to_text(message.content) for message in messages if isinstance(message, ToolMessage)]


def trim_text(value: Any, *, limit: int = 700) -> str:
    text = textwrap.shorten(str(value).replace("\n", " "), width=limit, placeholder=" ...")
    return text


def short_error(exc: BaseException) -> str:
    return trim_text(f"{type(exc).__name__}: {exc}", limit=900)


def env_summary() -> str:
    settings = get_settings()
    key_set = "set" if (os.getenv("LOCAL_LLM_KEY") or settings.local_llm_key) else "missing"
    return f"base_url=<LOCAL_LLM_BASE redacted>, model={settings.chat_model}, key={key_set}"
