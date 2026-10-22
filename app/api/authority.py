"""Request provenance for a trusted household, not user identity or role scopes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from time import monotonic
from typing import Literal
from uuid import uuid4

from starlette.requests import HTTPConnection
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from .auth import check_token
from ..config import get_settings

# Inherit the server's configured handler/level without using its raw access log.
logger = logging.getLogger('uvicorn.error.requests')


@dataclass(frozen=True)
class RequestAuthority:
    source: Literal['web_ui', 'api', 'api_rejected']


def is_api_request(request: HTTPConnection) -> bool:
    """Only middleware-validated bearer credentials grant direct-call authority."""
    authority = getattr(request.state, 'authority', None)
    return isinstance(authority, RequestAuthority) and authority.source == 'api'


def api_session_binding(request: HTTPConnection) -> str:
    """Opaque single-key binding for one-shot Link, never a client identity claim."""
    if not is_api_request(request):
        raise ValueError('validated API request required')
    return 'api:' + hashlib.sha256(get_settings().api_token.encode()).hexdigest()


async def unhandled_error_response(request, exc):
    """ServerErrorMiddleware is outside user middleware; carry its saved metadata."""
    headers = {}
    authority = getattr(request.state, 'authority', None)
    request_id = getattr(request.state, 'request_id', None)
    if isinstance(authority, RequestAuthority) and request_id:
        headers = {'X-Request-ID': request_id, 'X-FN-Caller': authority.source}
    return PlainTextResponse('Internal Server Error', status_code=500, headers=headers)


class RequestAuthorityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope['type'] not in {'http', 'websocket'}:
            await self.app(scope, receive, send)
            return
        supplied = [v.decode('latin-1') for k, v in scope.get('headers', [])
                    if k.lower() == b'authorization']
        status = check_token(supplied[0]) if len(supplied) == 1 else 'unauthorized'
        rejected = bool(supplied) and (len(supplied) != 1 or status != 'ok')
        source = 'api_rejected' if rejected else ('api' if supplied else 'web_ui')
        state = scope.setdefault('state', {})
        state['authority'] = RequestAuthority(source)
        state['request_id'] = request_id = str(uuid4())
        started = monotonic()
        status_code = 500

        async def traced_send(message):
            nonlocal status_code
            if message['type'] in {'http.response.start', 'websocket.accept'}:
                status_code = message.get('status', 101)
                headers = [(k, v) for k, v in message.get('headers', [])
                           if k.lower() not in {b'x-request-id', b'x-fn-caller'}]
                headers += [(b'x-request-id', request_id.encode()),
                            (b'x-fn-caller', source.encode())]
                message = {**message, 'headers': headers}
            elif message['type'] == 'websocket.close':
                status_code = message.get('code', 1000)
            await send(message)

        try:
            if rejected:
                if scope['type'] == 'websocket':
                    await traced_send({'type': 'websocket.close', 'code': 4401})
                else:
                    disabled = status == 'disabled' and len(supplied) == 1
                    detail = ('API disabled: set API_TOKEN to enable programmatic access'
                              if disabled else 'invalid or missing bearer token')
                    body = json.dumps({'detail': detail}, separators=(',', ':')).encode()
                    await traced_send({'type': 'http.response.start',
                        'status': 503 if disabled else 401,
                        'headers': [(b'content-type', b'application/json'),
                                    (b'content-length', str(len(body)).encode())]})
                    await traced_send({'type': 'http.response.body', 'body': body})
                return
            await self.app(scope, receive, traced_send)
        finally:
            # Templates, not raw URLs: no financial IDs, query strings, bodies,
            # cookies, credentials, peer addresses or exception details enter logs.
            route = getattr(scope.get('route'), 'path', '<unmatched>')
            event = {'request_id': request_id, 'caller': source,
                     'method': scope.get('method', 'WEBSOCKET'), 'route': route,
                     'status_code': status_code,
                     'duration_ms': round((monotonic() - started) * 1000, 3)}
            logger.info(json.dumps(event, separators=(',', ':')), extra=event)
