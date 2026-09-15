"""Structured-output schema for the batched LLM residue call."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ReconDecision(BaseModel):
    statement_line_id: int
    transaction_id: int | None
    confidence: float
    reason: str = ""


class ReconBatchDecision(BaseModel):
    decisions: list[ReconDecision] = Field(default_factory=list)
