"""Second-pass verifier for extracted receipts (FN-110).

Constrained decoding guarantees valid JSON, not correct values — a hallucinated
amount still passes schema validation. This pass adds a deterministic arithmetic
check (line items reconcile with the stated subtotal/total) plus an optional
LLM-as-judge vision call that scores the extracted fields against the source image.

A verifier *finding* (mismatch / rejection) routes the document to ``needs_review``
instead of auto-filing. A verifier *failure* (the judge call raising) is swallowed
and treated as a pass, so a flaky second call never blocks ingestion — the same
fail-safe posture as triage. The optional judge is the single extra LLM call, and
runs inside the caller's LLM gate.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ..config import get_settings
from ..llm.vision import strip_think, vision_messages
from .extract.receipt import _content_text, _loads_json, _to_image_bytes, preprocess_image
from .schemas import ExtractedReceipt

logger = logging.getLogger(__name__)


class VerifierVerdict(BaseModel):
    fields_match: bool = True
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""


_SYSTEM = (
    "You audit a receipt extraction against its source image for a personal-finance app. "
    "Return only the requested structured data."
)


def _judge_prompt(receipt: ExtractedReceipt) -> str:
    return (
        "Here is what an extractor read from this receipt image:\n"
        f"- merchant: {receipt.merchant!r}\n"
        f"- total_cents: {int(receipt.total_cents or 0)}\n"
        f"- subtotal_cents: {int(receipt.subtotal_cents or 0)}\n"
        f"- tax_cents: {int(receipt.tax_cents or 0)}\n"
        "Money is INTEGER CENTS ($12.34 -> 1234). Set fields_match=false only if a value "
        "clearly contradicts the image. Set confidence in [0,1]."
    )


def _line_items_sum(receipt: ExtractedReceipt) -> int:
    return sum(abs(int(li.amount_cents or 0)) for li in receipt.line_items)


def _within(a: int, b: int) -> bool:
    """True when ``a`` reconciles to ``b`` within a 1%-or-one-dollar tolerance."""
    return abs(a - b) <= max(100, int(0.01 * abs(b)))


def arithmetic_ok(receipt: ExtractedReceipt) -> tuple[bool, str]:
    """Deterministic check that itemized amounts reconcile with the stated totals.

    Line items normally sum to the pre-tax subtotal, but some receipts price each
    line tax-inclusive so the sum matches the grand total; either reconciliation
    passes. Receipts without a positive total or without line items carry no
    itemized signal to check and pass (upstream ``_validate`` owns ``no_total`` and
    the subtotal+tax+tip==total check).
    """
    total = int(receipt.total_cents or 0)
    if total <= 0 or not receipt.line_items:
        return True, "ok"
    line_sum = _line_items_sum(receipt)
    subtotal = int(receipt.subtotal_cents or 0)
    if subtotal > 0 and _within(line_sum, subtotal):
        return True, "ok"
    if _within(line_sum, total):
        return True, "ok"
    return False, "line_items_mismatch"


def _judge(llm, image_jpeg: bytes, receipt: ExtractedReceipt) -> VerifierVerdict:
    messages = vision_messages(_SYSTEM, _judge_prompt(receipt), image_jpeg, "image/jpeg")
    try:
        structured = llm.with_structured_output(
            VerifierVerdict, method=get_settings().structured_method
        )
        verdict = structured.invoke(messages)
    except Exception:
        resp = llm.invoke(messages)
        verdict = VerifierVerdict(**_loads_json(strip_think(_content_text(resp))))
    return verdict if isinstance(verdict, VerifierVerdict) else VerifierVerdict.model_validate(verdict)


def verify_receipt(
    llm, raw_image: bytes, receipt: ExtractedReceipt, source_document_id: int | None = None
) -> tuple[bool, str]:
    """Verify an extracted receipt; return ``(ok, reason)``.

    ``ok=False`` means a verifier finding should route the doc to review. Any
    exception is caught and treated as a pass (fail-safe) so verification never
    crashes ingestion.
    """
    try:
        ok, reason = arithmetic_ok(receipt)
        if not ok:
            return False, reason
        settings = get_settings()
        if not settings.verifier_llm_judge:
            return True, "ok"
        image_jpeg = preprocess_image(_to_image_bytes(raw_image))
        verdict = _judge(llm, image_jpeg, receipt)  # the single extra LLM call
        if (not verdict.fields_match) and verdict.confidence >= settings.verifier_min_confidence:
            return False, "verifier_rejected"
        return True, "ok"
    except Exception:  # noqa: BLE001 - verification must never block ingestion
        logger.exception(
            "receipt verifier failed; auto-filing without verification doc_id=%s",
            source_document_id,
        )
        return True, "ok"
