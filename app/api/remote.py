"""Small remote client for existing HTTP workflows, without local ledger access."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_RESPONSE_BYTES = 4_000_000


def request(method: str, path: str, *, base_url: str | None = None,
            data: dict | None = None, form: bool = False) -> dict:
    base_url = base_url or os.environ.get('FN_API_URL', '')
    key = os.environ.get('FN_API_KEY', '')
    if not key:
        raise ValueError('FN_API_KEY is required')
    if not key.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in base_url + path + key):
        raise ValueError('invalid connection configuration')
    try:
        base = urlsplit(base_url)
        target = urlsplit(path)
        base.port
        httpx.URL(base_url)
    except (ValueError, httpx.InvalidURL):
        raise ValueError('invalid connection configuration') from None
    if (base.scheme not in {'http', 'https'} or not base.hostname
            or base.username is not None or base.password is not None
            or '?' in base_url or '#' in base_url or base.path not in {'', '/'}):
        raise ValueError('FN_API_URL must be an HTTP(S) origin without credentials, query or fragment')
    if not path.startswith('/') or path.startswith('//') or target.scheme or target.netloc or '#' in path or '\\' in path:
        raise ValueError('endpoint must be an origin-relative path without fragment')
    try:
        url = httpx.URL(urlunsplit((base.scheme, base.netloc, target.path, target.query, '')))
    except httpx.InvalidURL:
        raise ValueError('invalid connection configuration') from None
    method = method.upper()
    if method not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}:
        raise ValueError('unsupported HTTP method')
    if data is not None and not isinstance(data, dict):
        raise ValueError('request data must be a JSON object')
    if data is not None:
        try:
            json.dumps(data, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError('request data must be valid JSON') from None
    body = {}
    if form and data is not None:
        if any(not isinstance(k, str) or isinstance(v, (dict, list)) for k, v in data.items()):
            raise ValueError('form values must be scalar')
        body['data'] = {k: str(v).lower() if isinstance(v, bool) else str(v)
                        for k, v in data.items() if v is not None}
    elif data is not None:
        body['json'] = data
    try:
        # One exchange only: no automatic redirects, proxies or connection retries.
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False,
                          follow_redirects=False, timeout=httpx.Timeout(30, connect=5)) as client:
            with client.stream(method, url,
                               headers={'Authorization': 'Bearer ' + key}, **body) as response:
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise ValueError('response exceeds 4000000 bytes')
                if chunks:
                    try:
                        value = json.loads(chunks)
                        # Nonfinite/overflow JSON cannot be represented in our envelope.
                        json.dumps(value, allow_nan=False)
                    except (ValueError, UnicodeError):
                        value = chunks.decode('utf-8', errors='replace')
                else:
                    value = None
                return {'status_code': response.status_code,
                        'request_id': response.headers.get('x-request-id'),
                        'caller': response.headers.get('x-fn-caller'), 'data': value}
    except httpx.HTTPError:
        raise ValueError('request outcome uncertain; inspect current state before retrying') from None
