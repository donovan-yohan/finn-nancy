"""Helpers for multimodal (vision) messages and thinking-model output.

Image message shape is isolated here (plan risk #1): if the endpoint prefers a
different block format, change it in one place.
"""
from __future__ import annotations

import base64
import re

from langchain_core.messages import HumanMessage, SystemMessage

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove Qwen ``<think>...</think>`` reasoning blocks before JSON parsing."""
    return _THINK.sub("", text or "").strip()


def image_data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"


def vision_messages(system: str, prompt: str, image_bytes: bytes, mime: str = "image/jpeg") -> list:
    return [
        SystemMessage(content=system),
        HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url(image_bytes, mime)}},
            ]
        ),
    ]
