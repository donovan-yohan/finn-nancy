"""Shared form-parsing helpers for dollar-amount inputs."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import HTTPException


def dollars_to_cents(raw: str) -> int:
    """Parse a dollar string (optional leading '-') into integer cents.

    Ties round away from zero (ROUND_HALF_UP) so '19.995' -> 2000, matching how
    a human would round a half-cent rather than silently truncating it.
    """
    try:
        return int((Decimal(raw.strip()) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, AttributeError):
        raise HTTPException(400, "invalid amount") from None
