from __future__ import annotations

from pydantic import BaseModel, Field

from ..accounting.contract import FlowKind


class TransactionIn(BaseModel):
    amount_cents: int = Field(
        description=(
            "Signed integer cents. Expenses must be negative and income must be positive; "
            "validation uses the resolved category kind. Transfer categories accept either sign."
        )
    )
    posted_on: str
    description: str
    counterparty: str = ""
    category: str | None = Field(
        default=None,
        description=(
            "Existing category name. Omitted or unmatched names fall back to Uncategorized "
            "(an expense category) and the response reports category_fallback."
        ),
    )
    account_id: int | None = None
    card_last4: str | None = None
    external_id: str | None = Field(
        default=None,
        description=(
            "Stable caller-supplied idempotency key. Without one, every call creates a new transaction."
        ),
    )
    source: str = "api"
    source_confidence: float = 1.0
    notes: str = ""
    flow_kind: FlowKind = Field(
        default=FlowKind.UNKNOWN,
        description=(
            "Movement semantics independent of category. Use unknown to explicitly "
            "defer ambiguous income/refund/transfer/payment meaning to review."
        ),
    )
