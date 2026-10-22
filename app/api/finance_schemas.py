"""Bounded machine inputs. Actor and approval authority are not client fields."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from ..accounting.contract import FlowKind

SQLITE_MAX_ID = 9223372036854775807
PositiveId = Annotated[int, Field(strict=True, gt=0, le=SQLITE_MAX_ID)]


def _http_id(value):
    # HTTP path/query IDs arrive as text, but decimal floats and booleans are
    # not identifiers. Preserve strict integers for JSON/service callers.
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return value


HttpId = Annotated[PositiveId, BeforeValidator(_http_id)]


class TransactionSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str | None = Field(None, max_length=200)
    account_id: HttpId | None = None
    category_id: HttpId | None = None
    date_from: date | None = None
    date_to: date | None = None
    flow_kind: FlowKind | None = None
    review: Literal["needs_review", "resolved"] | None = None
    cursor: str | None = Field(None, max_length=500)
    limit: int = Field(50, ge=1, le=500)

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not follow date_to")
        return self


class CategoryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_key: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]*$")
    transaction_id: PositiveId
    split_id: PositiveId
    to_category_id: PositiveId
    expected_state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rationale: str = Field(min_length=1, max_length=2000)
    confidence: float | None = Field(None, ge=0, le=1, allow_inf_nan=False)
    evidence_refs: list[str] | None = Field(None, max_length=20)


class FinancialQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sql: str = Field(min_length=1, max_length=16000)
    parameters: dict[str, str | int | float | None] = Field(default_factory=dict, max_length=50)
    limit: int = Field(100, ge=1, le=500)
