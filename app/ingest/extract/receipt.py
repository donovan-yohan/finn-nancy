"""Receipt image -> ExtractedReceipt via the vision model.

Primary path uses ``with_structured_output``; if the backend doesn't honor it, we
fall back to a plain call + JSON parse + one repair (plan risks #1/#2).
"""
from __future__ import annotations

import io
import json

from PIL import Image, ImageOps

try:  # iPhone photos are HEIC; register the opener if available.
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

from ...config import get_settings
from ...llm.vision import strip_think, vision_messages
from ..schemas import ExtractedReceipt
from .pdf import first_page_image

def _system_prompt() -> str:
    s = get_settings()
    return (
        "You are a meticulous receipt-extraction engine for a personal-finance app. "
        f"The user lives in {s.home_locale}; {s.home_currency} is the home currency, but do not "
        "infer it when the receipt has no reliable currency evidence. Leave currency empty and "
        "list it in unreadable_fields when ambiguous. Use your knowledge of local merchants near "
        f"{s.home_locale} to set an accurate category_guess (recognize local restaurants, grocers, "
        "transit, etc.). Return ONLY the requested structured data. All money values are INTEGER "
        "CENTS (e.g. $12.34 -> 1234) and positive magnitudes. Dates are YYYY-MM-DD."
    )


_PROMPT = (
    "Extract the fields from this receipt image. If a field is unreadable, leave it empty/0 "
    "and list its name in unreadable_fields. category_guess is a short spending category such "
    "as Groceries, Restaurants, Transport, Utilities. Set confidence in [0,1]."
)


def _to_image_bytes(raw: bytes) -> bytes:
    """A PDF receipt becomes page-1 raster; an image passes through."""
    if raw[:5] == b"%PDF-":
        return first_page_image(raw)
    return raw


def preprocess_image(raw: bytes, max_side: int = 1536) -> bytes:
    """Orient, flatten to RGB, downscale, re-encode as JPEG — smaller + model-friendly."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _content_text(resp) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):  # langchain v1 content blocks
        parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in content]
        content = " ".join(parts)
    return str(content)


def _loads_json(text: str) -> dict:
    text = text.strip().strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def extract_receipt(llm, raw_image: bytes) -> ExtractedReceipt:
    settings = get_settings()
    jpeg = preprocess_image(_to_image_bytes(raw_image))
    messages = vision_messages(_system_prompt(), _PROMPT, jpeg, "image/jpeg")
    try:
        structured = llm.with_structured_output(ExtractedReceipt, method=settings.structured_method)
        receipt = structured.invoke(messages)
    except Exception:
        resp = llm.invoke(messages)
        receipt = ExtractedReceipt(**_loads_json(strip_think(_content_text(resp))))
    return receipt
