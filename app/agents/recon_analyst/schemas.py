"""Typed review contract for the reconciliation analyst."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PayloadValue = int | float | str | bool | None | list[int]
FindingKind = Literal[
    "coverage_summary",
    "unmatched_line",
    "suspicious_match",
    "recurring_change",
    "recurring_price_increase",
    "recurring_price_decrease",
    "new_subscription",
    "budget_overrun",
    "planning_insight",
]
ActionKind = Literal[
    "confirm_match",
    "review_unmatched",
    "add_subscription",
    "adjust_budget",
    "categorize",
]

_DROP = object()


def _coerce_int_list(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]

    seen: set[int] = set()
    out: list[int] = []
    for item in raw_items:
        if item is None or item == "":
            continue
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        out.append(parsed)
    return out


def _sanitize_payload_value(value: Any) -> PayloadValue | object:
    if isinstance(value, dict):
        return _DROP
    if isinstance(value, (list, tuple, set)):
        if not value:
            return []
        items: list[int] = []
        seen: set[int] = set()
        for item in value:
            if isinstance(item, bool) or isinstance(item, dict) or isinstance(item, (list, tuple, set)):
                continue
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            items.append(parsed)
        return items if items else _DROP
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _DROP


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_ids: list[int] = Field(default_factory=list)
    statement_line_ids: list[int] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)

    @field_validator("transaction_ids", "statement_line_ids", "category_ids", mode="before")
    @classmethod
    def _ids_as_ints(cls, value: Any) -> list[int]:
        return _coerce_int_list(value)

    def is_empty(self) -> bool:
        return not (self.transaction_ids or self.statement_line_ids or self.category_ids)


class StatementRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_document_id: int | None = None
    month: str
    months: list[str] = Field(default_factory=list)
    account_ids: list[int] = Field(default_factory=list)
    document_name: str | None = None

    @field_validator("account_ids", mode="before")
    @classmethod
    def _account_ids_as_ints(cls, value: Any) -> list[int]:
        return _coerce_int_list(value)

    @field_validator("months", mode="before")
    @classmethod
    def _months_as_strings(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = [value]
        seen: set[str] = set()
        out: list[str] = []
        for item in raw_items:
            month = str(item).strip()
            if not month or month in seen:
                continue
            seen.add(month)
            out.append(month)
        return sorted(out)

    @model_validator(mode="after")
    def _default_months(self) -> "StatementRun":
        if not self.months:
            self.months = [self.month]
        return self


class CoverageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_count: int = 0
    statement_spend_cents: int = 0
    covered_spend_cents: int = 0
    unmatched_spend_cents: int = 0
    ignored_spend_cents: int = 0
    income_cents: int = 0
    attention_spend_cents: int = 0
    coverage_pct: float = 0.0


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    kind: str
    title: str
    detail: str
    priority: int = Field(ge=1)
    severity: Literal["info", "watch", "warn", "critical"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: Evidence = Field(default_factory=Evidence)


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    payload: dict[str, PayloadValue] = Field(default_factory=dict)
    evidence: Evidence = Field(default_factory=Evidence)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    agent_run_id: str

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_is_flat(cls, value: Any) -> dict[str, PayloadValue]:
        if not isinstance(value, dict):
            return {}
        out: dict[str, PayloadValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            sanitized = _sanitize_payload_value(item)
            if sanitized is _DROP:
                continue
            out[key] = sanitized  # type: ignore[assignment]
        return out


class LLMReviewFinding(ReviewFinding):
    kind: FindingKind


class LLMProposedAction(ProposedAction):
    kind: ActionKind


class ReconciliationReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_run_id: str
    statement_run: StatementRun
    period_summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    coverage: CoverageSummary | None = None
    guard_notes: list[str] = Field(default_factory=list)
