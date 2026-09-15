"""Bank/credit-card statement PDF -> ExtractedStatement.

Digital PDFs (text layer present) get one text-only structured call. Scanned PDFs
(sparse/no text layer) get one vision structured call with every page rasterized
and attached as images — mirrors app/llm/vision.py's block format.
"""
from __future__ import annotations

import re

import fitz  # PyMuPDF
from langchain_core.messages import HumanMessage, SystemMessage

from ...config import get_settings
from ...llm.vision import image_data_url
from ..schemas import ExtractedStatement
from .pdf import extract_text, page_count

_MAX_SCANNED_PAGES = 8
_SCAN_DPI = 170
_DIGITAL_CHARS_PER_PAGE = 200  # below this average -> treat as scanned/sparse
_MAX_DIGITAL_CHARS = 30000  # cap the LLM prompt; checksum_ok gate catches any row loss


def _system_prompt() -> str:
    s = get_settings()
    return (
        "You are a meticulous bank/credit-card statement extraction engine for a personal "
        f"finance app. The user lives in {s.home_locale}; {s.home_currency} is the home "
        "currency, but do not infer it when the statement has no reliable currency evidence. "
        "Leave currency empty when ambiguous. All money "
        "values are SIGNED INTEGER CENTS (e.g. $12.34 -> 1234): debits/purchases/withdrawals "
        "are NEGATIVE, credits/deposits/payments-received are POSITIVE. Dates are "
        "'YYYY-MM-DD' — infer the year from the statement period for rows printed with only a "
        "month and day. Include EVERY transaction row; skip summary/subtotal/total lines. "
        "statement_period is the closing month as 'YYYY-MM'. Extract the printed "
        "period_start_on, period_end_on, and statement_issued_on dates; never substitute the "
        "latest transaction date for period_end_on. Fill opening_balance_cents and "
        "closing_balance_cents from the printed balances when shown. Fill account_last4 with "
        "the last 4 digits of the printed account/card number when shown. Capture printed "
        "page and transaction-row counts when present. Set zero_activity only when the "
        "statement explicitly says there were no transactions. For every metadata field, "
        "provide field_confidence and a 1-based field_pages source page; every row needs its "
        "1-based page_number and per-field confidence. Set is_pending=true for rows the "
        "statement marks pending/authorized-but-not-posted. Set confidence in [0,1]."
    )


def _digital_prompt(text: str) -> str:
    return "Extract every transaction row from this bank/credit-card statement text:\n\n" + text


def _vision_prompt(n_pages: int) -> str:
    return (
        f"Extract every transaction row from this {n_pages}-page bank/credit-card statement. "
        "The pages are attached as images, in order."
    )


def _rasterize_pages(raw_pdf: bytes, n: int) -> list[bytes]:
    with fitz.open(stream=raw_pdf, filetype="pdf") as doc:
        return [doc.load_page(i).get_pixmap(dpi=_SCAN_DPI).tobytes("png") for i in range(n)]


def extract_statement(llm, raw_pdf: bytes) -> ExtractedStatement:
    settings = get_settings()
    pages = page_count(raw_pdf)
    text = extract_text(raw_pdf)
    avg_chars = len(text) / max(1, pages)

    truncated = False
    extracted_pages = pages
    if avg_chars >= _DIGITAL_CHARS_PER_PAGE:
        if len(text) > _MAX_DIGITAL_CHARS:
            text = text[:_MAX_DIGITAL_CHARS] + "\n[TRUNCATED]"
            truncated = True
        messages = [
            SystemMessage(content=_system_prompt()),
            HumanMessage(content=_digital_prompt(text)),
        ]
    else:
        n = min(pages, _MAX_SCANNED_PAGES)
        extracted_pages = n
        truncated = n < pages
        images = _rasterize_pages(raw_pdf, n)
        content = [{"type": "text", "text": _vision_prompt(n)}]
        content += [
            {"type": "image_url", "image_url": {"url": image_data_url(img, "image/png")}}
            for img in images
        ]
        messages = [
            SystemMessage(content=_system_prompt()),
            HumanMessage(content=content),
        ]

    structured = llm.with_structured_output(ExtractedStatement, method=settings.structured_method)
    parsed = structured.invoke(messages)
    digits = re.sub(r"\D", "", parsed.account_last4 or "")
    safe_last4 = digits[-4:] if len(digits) >= 4 else ""
    return parsed.model_copy(
        update={
            "account_last4": safe_last4,
            "observed_page_count": pages,
            "extracted_page_count": extracted_pages,
            "extraction_truncated": truncated,
        }
    )


def checksum_ok(parsed: ExtractedStatement) -> bool:
    """True if opening/closing balances aren't both printed (can't check), else within 1c
    under EITHER sign convention.

    Most accounts are asset-style: balance = opening + signed_sum. Credit-card statements
    are debt-style: the printed balance is a debt that RISES with spending even though our
    rows stay debit-negative, so balance = opening - signed_sum there. We can't tell which
    convention a given statement uses up front, so accept whichever one reconciles.
    """
    if parsed.opening_balance_cents is None or parsed.closing_balance_cents is None:
        return True
    total = sum(row.amount_cents for row in parsed.rows)
    asset_style = abs(parsed.opening_balance_cents + total - parsed.closing_balance_cents) <= 1
    debt_style = abs(parsed.opening_balance_cents - total - parsed.closing_balance_cents) <= 1
    return asset_style or debt_style
