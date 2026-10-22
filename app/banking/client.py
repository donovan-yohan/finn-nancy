"""Fixed-host, bounded Plaid production transport. No automatic retries."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from collections.abc import Callable

import httpx


class BankingError(ValueError):
    """An allowlisted safe error code, never provider text or secrets."""


ENDPOINTS = frozenset({
    '/link/token/create', '/link/token/get', '/item/public_token/exchange',
    '/accounts/get', '/transactions/sync', '/item/remove',
})
SAFE_ERRORS = frozenset({
    'ITEM_LOGIN_REQUIRED', 'PRODUCT_NOT_READY', 'RATE_LIMIT_EXCEEDED',
    'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION', 'INVALID_LINK_TOKEN',
})


class PlaidClient:
    def __init__(self, credentials_file: Path, *, policy: Callable[[], None],
                 transport: httpx.BaseTransport | None = None):
        self.credentials_file = Path(credentials_file)
        self.policy = policy
        self.transport = transport

    def post(self, endpoint: str, payload: dict) -> dict:
        from .secrets import check_private_path, private_read
        if endpoint not in ENDPOINTS:
            raise BankingError('unsupported_endpoint')
        self.policy()
        try:
            credentials = json.loads(private_read(self.credentials_file))
            body = {**payload, 'client_id': credentials['client_id'], 'secret': credentials['secret']}
            # No ambient proxy, redirects, cookie jar, request hooks or retry loop.
            with httpx.Client(transport=self.transport, trust_env=False,
                              follow_redirects=False, timeout=httpx.Timeout(20, connect=5)) as client:
                self.policy()  # includes withdrawal between credential read and dispatch
                check_private_path(self.credentials_file)
                with client.stream('POST', 'https://production.plaid.com' + endpoint, json=body) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 2_000_000:
                            raise BankingError('provider_response_too_large')
                    parsed = json.loads(data, parse_float=Decimal,
                                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if not isinstance(parsed, dict):
                        raise BankingError('invalid_provider_response')
                    if response.status_code != 200:
                        code = parsed.get('error_code')
                        raise BankingError(code if code in SAFE_ERRORS else 'provider_request_failed')
                    return parsed
        except BankingError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError):
            raise BankingError('provider_request_uncertain') from None
