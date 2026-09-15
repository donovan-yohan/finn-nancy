"""Jinja2 template filters."""
from __future__ import annotations


def money(cents: int | None) -> str:
    """Format integer cents as ``$1,234.56`` (or ``-$1,234.56``). Matches the Go portal."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"
