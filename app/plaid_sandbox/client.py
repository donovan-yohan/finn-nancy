"""The candidate has exactly one data authority; no environment/base URL option."""
from __future__ import annotations

import re
import json
import time
from decimal import Decimal

import httpx

ENDPOINTS = frozenset({
    "/link/token/create", "/link/token/get", "/item/public_token/exchange",
    "/accounts/get", "/item/get", "/item/remove", "/transactions/sync",
})


class SandboxError(Exception):
    """Only a locally selected, content-free error code may cross this boundary."""


def sandbox_token(value: str, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"{kind}-sandbox-[A-Za-z0-9-]{{1,200}}", value):
        raise SandboxError("sandbox_token_required")
    return value


class SandboxClient:
    def __init__(self, *, enabled=False, credentials=None, transport=None):
        self.enabled = enabled
        self._credentials = credentials
        self._transport = transport

    def call(self, path: str, **fields) -> dict:
        if not self.enabled:
            raise SandboxError("sandbox_disabled")
        if not self._credentials or not all(self._credentials):
            raise SandboxError("sandbox_not_configured")
        if path not in ENDPOINTS:
            raise SandboxError("sandbox_endpoint_required")
        for key, kind in (("access_token", "access"), ("public_token", "public"), ("link_token", "link")):
            if key in fields:
                sandbox_token(fields[key], kind)
        # Credential fields cannot be supplied/overridden by a caller.
        if "client_id" in fields or "secret" in fields:
            raise SandboxError("invalid_request")
        try:
            with httpx.Client(transport=self._transport, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(10, connect=5), headers={"Accept-Encoding": "identity"}) as client:
                deadline = time.monotonic() + 15
                with client.stream("POST", "https://sandbox.plaid.com" + path, json={
                    **fields, "client_id": self._credentials[0], "secret": self._credentials[1],
                }) as response:
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise SandboxError("invalid_response")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > 2 * 1024 * 1024 or time.monotonic() > deadline:
                            raise SandboxError("response_limit")
                        body.extend(chunk)
                    if 300 <= response.status_code < 400:
                        raise SandboxError("redirect_refused")
                    data = json.loads(body, parse_float=Decimal)
                    if not isinstance(data, dict):
                        raise SandboxError("invalid_response")
                    if response.status_code != 200:
                        code = data.get("error_code")
                        safe = {"ITEM_LOGIN_REQUIRED", "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
                                "PRODUCT_NOT_READY", "INVALID_ACCESS_TOKEN", "RATE_LIMIT_EXCEEDED"}
                        raise SandboxError(code if code in safe else "provider_rejected")
                    for key, kind in (("access_token", "access"), ("public_token", "public"), ("link_token", "link")):
                        if key in data:
                            sandbox_token(data[key], kind)
                    return data
        except httpx.HTTPError:
            raise SandboxError("network_uncertain") from None
        except (ValueError, TypeError):
            raise SandboxError("invalid_response") from None
