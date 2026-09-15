"""Fast first-page document classification before extraction routing."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..config import get_settings
from ..llm.vision import vision_messages
from .extract.pdf import first_page_image
from .extract.receipt import preprocess_image


class TriageResult(BaseModel):
    kind: Literal["statement", "receipt", "invoice", "other"]
    confidence: float = Field(ge=0.0, le=1.0)


_SYSTEM = "You classify financial documents. Return only the requested structured data."
_PROMPT = (
    "Classify this document as statement (bank or credit card), receipt (completed purchase), "
    "invoice (request for payment), or other. Set confidence from 0 to 1."
)


def triage_document(llm, raw: bytes, mime: str) -> TriageResult:
    """Classify a document using only its first page/image."""
    if mime == "application/pdf" or raw[:5] == b"%PDF-":
        image = first_page_image(raw)
    else:
        image = raw
    jpeg = preprocess_image(image)
    messages = vision_messages(_SYSTEM, _PROMPT, jpeg, "image/jpeg")
    structured = llm.with_structured_output(
        TriageResult, method=get_settings().structured_method
    )
    result = structured.invoke(messages)
    return result if isinstance(result, TriageResult) else TriageResult.model_validate(result)
