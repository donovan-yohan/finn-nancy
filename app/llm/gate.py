"""Process-wide LLM serialization gate.

The backend keeps one model resident and has few parallel slots, so every LLM call
(ingest worker today, chat tomorrow) acquires this semaphore. Lazily created so it
binds to the running event loop.
"""
from __future__ import annotations

import asyncio

from ..config import get_settings

_gate: asyncio.Semaphore | None = None


def llm_gate() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(get_settings().llm_concurrency)
    return _gate
