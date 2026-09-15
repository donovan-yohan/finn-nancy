"""Structured-output schemas for extraction. Magnitudes are POSITIVE integer cents;
sign is applied at promotion time (expenses negative)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ReceiptLineItem(BaseModel):
    description: str = ""
    amount_cents: int = 0  # positive magnitude


class StatementRow(BaseModel):
    posted_on: str = ""          # 'YYYY-MM-DD'
    description: str = ""
    amount_cents: int = 0        # SIGNED: expense negative, income/credit positive
    balance_cents: int | None = None
    is_pending: bool = False
    page_number: int = 0         # 1-based source page; 0 means unanchored
    field_confidence: dict[str, float] = Field(default_factory=dict)


class ExtractedStatement(BaseModel):
    institution: str = ""
    account_hint: str = ""       # e.g. account name/type printed on the statement
    account_last4: str = ""      # last 4 digits of the account/card number, if shown
    currency: str = ""  # empty means ambiguous; promotion must fail closed
    statement_period: str = ""   # 'YYYY-MM' of the closing date
    period_start_on: str = ""    # actual first day printed by the statement
    period_end_on: str = ""      # actual closing day printed by the statement
    statement_issued_on: str = ""
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None
    declared_page_count: int | None = None
    declared_row_count: int | None = None
    zero_activity: bool = False
    field_confidence: dict[str, float] = Field(default_factory=dict)
    field_pages: dict[str, int] = Field(default_factory=dict)
    rows: list[StatementRow] = Field(default_factory=list)
    confidence: float = 0.0
    # Ground-truth extraction-envelope fields.  The extractor overwrites these
    # after the model call; model output is never trusted for them.
    observed_page_count: int = 0
    extracted_page_count: int = 0
    extraction_truncated: bool = False


class ExtractedReceipt(BaseModel):
    merchant: str = ""
    purchased_on: str = ""  # 'YYYY-MM-DD' best-effort; empty if unreadable
    currency: str = ""  # empty means ambiguous; promotion must fail closed
    subtotal_cents: int = 0
    tax_cents: int = 0
    tip_cents: int = 0
    total_cents: int = 0
    line_items: list[ReceiptLineItem] = Field(default_factory=list)
    card_last4: str = ""
    category_guess: str = ""  # a short spending category, e.g. "Groceries"
    confidence: float = 0.0  # [0,1]
    unreadable_fields: list[str] = Field(default_factory=list)
