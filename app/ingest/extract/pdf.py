"""PDF helpers (PyMuPDF).

A PDF can be a one-off *receipt* (ExampleRide Eats, an emailed invoice saved to PDF) or a
multi-page *statement*. We rasterize page 1 for the vision extractor and use a cheap
page-count + keyword triage to tell receipts from statements (statements → M3).
"""
from __future__ import annotations

import re

import fitz  # PyMuPDF

_STATEMENT_MARKERS = (
    "opening balance", "closing balance", "previous balance", "statement period",
    "statement date", "minimum payment", "payment due", "account summary",
    "account statement", "monthly statement",
)

# Statements put the word in the header ("March 2026 Statement", "Synthetic Checking Account
# Statement"); receipts put "receipt" there instead. Checked before receipt markers.
_STATEMENT_HEADER = re.compile(r"\bstatement\b")

# Delivery-app receipts (ExampleRide Eats etc.) are often 2-3 page PDFs — page count alone
# must not force "statement".
_RECEIPT_MARKERS = (
    "your receipt", "receipt for", "thanks for tipping", "order total",
    "thank you for your order", "subtotal",
)


def page_count(raw: bytes) -> int:
    with fitz.open(stream=raw, filetype="pdf") as doc:
        return doc.page_count


def first_page_image(raw: bytes, dpi: int = 200) -> bytes:
    """Render page 1 to PNG bytes for the vision model."""
    with fitz.open(stream=raw, filetype="pdf") as doc:
        pix = doc.load_page(0).get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def extract_text(raw: bytes) -> str:
    with fitz.open(stream=raw, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def looks_like_statement(raw: bytes) -> bool:
    try:
        pages = page_count(raw)
        text = extract_text(raw).lower()
    except Exception:
        return True  # unreadable -> defer to statement/needs_review
    if any(marker in text for marker in _STATEMENT_MARKERS):
        return True
    if _STATEMENT_HEADER.search(text[:400]):
        return True
    if any(marker in text for marker in _RECEIPT_MARKERS):
        return False
    # No signals either way: single page reads as a receipt; longer docs park for review.
    return pages > 1


def pdf_doc_kind(raw: bytes) -> str:
    """'receipt' for a single-page non-statement PDF, else 'statement'."""
    return "statement" if looks_like_statement(raw) else "receipt"
