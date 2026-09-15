"""Merchant/description normalization — the single canonical form.

Shared by classification (merchant_aliases key) and, later, reconciliation
(row_hash). Keep it deterministic and boring.
"""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
_NUM_TOKEN = re.compile(r"\b\d+\b")


def norm_merchant(raw: str) -> str:
    """Normalize a merchant string to a stable uppercase token.

    'LOBLAWS #123' / 'Loblaws Store 123' -> 'LOBLAWS'. Empty in -> empty out.
    """
    s = (raw or "").lower()
    s = _NON_ALNUM.sub(" ", s)
    s = _NUM_TOKEN.sub(" ", s)  # drop store numbers etc.
    s = _WS.sub(" ", s).strip()
    return s.upper()


def row_hash(account_id: int | None, posted_on: str, amount_cents: int,
             merchant: str, occ: int) -> str:
    """Canonical statement-line identity.

    occ is the ordinal among identical (account, date, amount, merchant) tuples in
    account history: preserves two legitimate identical charges while collapsing
    re-exports of the same statement row. Promoted rows reuse this as
    transactions.external_id (source='statement') so re-promotion is idempotent.
    """
    import hashlib

    key = f"{account_id or 0}|{posted_on}|{amount_cents}|{norm_merchant(merchant)}|{occ}"
    return hashlib.sha1(key.encode()).hexdigest()
