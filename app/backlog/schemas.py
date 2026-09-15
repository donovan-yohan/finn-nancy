"""Structured-output schemas for backlog classification fallback."""
from __future__ import annotations

from pydantic import BaseModel, Field


class BacklogCategoryGuess(BaseModel):
    category_name: str = Field(
        default="",
        description="One existing expense category name, or empty if no category fits.",
    )
    confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    rationale: str = ""
