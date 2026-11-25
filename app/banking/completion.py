"""Background Hosted Link completion.

Plaid's completion redirect only returns the user's browser; it proves nothing.
The public token must be exchanged within 30 minutes, so the server checks open
link sessions itself instead of waiting for a browser to come back and click.
Finn Nancy is tailnet-only, so this polls /link/token/get rather than exposing a
public webhook receiver.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger('uvicorn.error.banking')

POLL_SECONDS = 10.0


async def run_link_completion(stop: asyncio.Event, service, poll_seconds: float = POLL_SECONDS) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(service.complete_pending_links)
        except Exception as exc:  # noqa: BLE001 - keep polling; content-free log
            logger.warning('{"event":"bank_link_completion_failed","error":"%s"}', type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
