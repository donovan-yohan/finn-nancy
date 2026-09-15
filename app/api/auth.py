from __future__ import annotations

import hmac
from typing import Literal

from fastapi import Header, HTTPException

from ..config import get_settings

TokenCheck = Literal["ok", "disabled", "unauthorized"]


def check_token(authorization: str | None) -> TokenCheck:
    token = get_settings().api_token
    if not token:
        return "disabled"
    expected = f"Bearer {token}"
    # Compare as bytes: str compare_digest raises TypeError on non-ASCII input,
    # which would turn a garbage header into a 500 instead of a 401.
    if authorization is None or not hmac.compare_digest(
        authorization.encode("utf-8"), expected.encode("utf-8")
    ):
        return "unauthorized"
    return "ok"


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    status = check_token(authorization)
    if status == "disabled":
        raise HTTPException(
            status_code=503,
            detail="API disabled: set API_TOKEN to enable programmatic access",
        )
    if status == "unauthorized":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
