"""Warm the model so the first real call doesn't eat the full cold-start penalty."""
from __future__ import annotations

import asyncio

from .client import make_llm
from .gate import llm_gate


async def warm(llm=None) -> bool:
    """Fire a tiny completion. Returns True on success; swallows errors (endpoint may be down)."""
    llm = llm or make_llm()
    try:
        async with llm_gate():
            await asyncio.to_thread(llm.invoke, "ready?")
        return True
    except Exception:
        return False
