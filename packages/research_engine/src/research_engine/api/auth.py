"""Auth for REST, MCP and the GUI (§8, B2): ``X-API-Key`` header or the signed GUI session cookie.

Precedence for every HTTP request:
1. open path -> pass;
2. valid ``X-API-Key`` -> pass (no Origin check: not a browser credential);
3. valid ``re_session`` cookie -> pass, but state-changing methods must also pass the Origin
   check (``gui.session.same_origin``), else 403 (envelope on /v1 and /mcp, plain page elsewhere);
4. otherwise 401 envelope on /v1 and /mcp, and 303 to ``/login?next=<path>`` for anything else.

WebSocket connections accept only ``X-API-Key`` and are closed with 1008 otherwise; every other
scope type except ``lifespan`` fails closed.
"""

import hmac
import json
from urllib.parse import quote

from research_engine_client.models import SCHEMA_VERSION, ErrorCode
from starlette.types import ASGIApp, Receive, Scope, Send

from research_engine.gui.session import (
    COOKIE,
    STATE_CHANGING,
    SessionCodec,
    cookie_values,
    same_origin,
)

OPEN_PATHS = ("/health", "/version", "/openapi.json", "/docs", "/redoc", "/login", "/static/")
API_PREFIXES = ("/v1", "/mcp")


def is_api_path(path: str) -> bool:
    """``/v1`` and ``/mcp`` (and below) speak envelopes, never HTML or redirects."""
    return any(path == p or path.startswith(p + "/") for p in API_PREFIXES)


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [v.decode("latin-1") for k, v in scope.get("headers") or [] if k == name]


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, api_key: str, codec: SessionCodec, site_host: str) -> None:
        self.app = app
        self._key = api_key.encode()
        self._codec = codec
        self._site_host = site_host

    def _is_open(self, path: str) -> bool:
        return any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS)

    def _has_key(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"x-api-key", b"")
        return bool(supplied) and hmac.compare_digest(supplied, self._key)

    def _has_session(self, scope: Scope) -> bool:
        values = cookie_values(_header_values(scope, b"cookie"), COOKIE)
        # Check every candidate (no early exit) so a planted junk cookie cannot shadow ours.
        return any([self._codec.valid(v) for v in values])

    def _origin_ok(self, scope: Scope) -> bool:
        hosts = _header_values(scope, b"host")
        return same_origin(
            _header_values(scope, b"origin"),
            _header_values(scope, b"referer"),
            hosts[0] if len(hosts) == 1 else "",
            self._site_host,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and self._is_open(scope["path"]):
            await self.app(scope, receive, send)
            return
        if self._has_key(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # websocket: refuse before accept (policy violation)
            await send({"type": "websocket.close", "code": 1008})
            return
        path: str = scope["path"]
        if self._has_session(scope):
            if scope["method"] not in STATE_CHANGING or self._origin_ok(scope):
                await self.app(scope, receive, send)
                return
            await self._forbidden(scope, send)
            return
        if is_api_path(path):
            await _send_envelope(
                scope,
                send,
                401,
                "missing or invalid API key",
                [(b"www-authenticate", b'ApiKey header="X-API-Key"')],
            )
            return
        target = path
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        location = "/login?next=" + quote(target, safe="/")
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [(b"location", location.encode()), (b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _forbidden(self, scope: Scope, send: Send) -> None:
        message = "cross-origin request refused"
        if is_api_path(scope["path"]):
            await _send_envelope(scope, send, 403, message)
            return
        body = f"403 Forbidden: {message}\n".encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _send_envelope(
    scope: Scope,
    send: Send,
    status: int,
    message: str,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(
        {
            "data": None,
            "errors": [
                {
                    "code": ErrorCode.UNAUTHORIZED.value,
                    "message": message,
                    "retryable": False,
                    "source": None,
                }
            ],
            "meta": {
                "request_id": scope.get("state", {}).get("request_id", ""),
                "schema_version": SCHEMA_VERSION,
                "took_ms": 0,
                "cache_hit": False,
            },
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(extra_headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
