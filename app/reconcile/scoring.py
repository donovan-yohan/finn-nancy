"""Match scoring: merchant text similarity, date proximity, and their weighted composite."""
from __future__ import annotations

import datetime as dt

from rapidfuzz import fuzz

from ..ingest.normalize import norm_merchant

AUTO_MERCHANT = 0.87
LLM_MIN_MERCHANT = 0.0
LLM_CONF = 0.6
DATE_WINDOW_BACK = 7
DATE_WINDOW_FWD = 1

_AMOUNT_W, _MERCHANT_W, _DATE_W = 0.55, 0.30, 0.15


def merchant_score(a: str, b: str) -> float:
    """token_set_ratio on the canonical merchant form, in [0,1]. Blank inputs score 0."""
    na, nb = norm_merchant(a), norm_merchant(b)
    if not na or not nb:
        return 0.0
    return fuzz.token_set_ratio(na, nb) / 100.0


def date_score(line_date: str, txn_date: str) -> float:
    """Peaks at a 2-day lag (typical settlement delay), decays over an 8-day span."""
    lag = (dt.date.fromisoformat(line_date) - dt.date.fromisoformat(txn_date)).days
    return max(0.0, 1 - abs(lag - 2) / 8)


def composite(amount: float, merchant: float, date: float) -> float:
    return _AMOUNT_W * amount + _MERCHANT_W * merchant + _DATE_W * date
