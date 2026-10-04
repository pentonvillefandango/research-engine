"""API-key auth for REST and MCP (§8). GUI session cookie support is added in step 6."""

import hmac
import json

from research_engine_client.models import SCHEMA_VERSION, ErrorCode
from starlette.types import ASGIApp, Receive, Scope, Send

OPEN_PATHS = ("/health", "/version", "/openapi.json", "/docs", "/redoc", "/login", "/static/")


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self.app = app
        self._key = api_key.encode()

    def _is_open(self, path: str) -> bool:
        return any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and self._is_open(scope["path"]):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"x-api-key", b"")
        if supplied and hmac.compare_digest(supplied, self._key):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # websocket: refuse before accept (policy violation)
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {
                "data": None,
                "errors": [
                    {
                        "code": ErrorCode.UNAUTHORIZED.value,
                        "message": "missing or invalid API key",
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
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'ApiKey header="X-API-Key"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
