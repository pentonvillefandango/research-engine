import contextlib
from unittest.mock import AsyncMock, MagicMock

import httpx
from research_engine.app import create_app
from research_engine.config import Settings
from research_engine.testing import build_test_services
from research_engine_client.models import Envelope, ErrorCode

from tests.conftest import TEST_API_KEY


async def test_404_is_envelope(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/nope")
    assert r.status_code == 404
    env = Envelope[None].model_validate(r.json())
    assert env.data is None and env.errors[0].code == ErrorCode.NOT_FOUND
    assert r.headers["x-request-id"] == env.meta.request_id


async def test_405_is_envelope_with_allow(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/search")
    assert r.status_code == 405
    env = Envelope[None].model_validate(r.json())
    assert env.errors[0].code == ErrorCode.INVALID_REQUEST
    assert "POST" in r.headers["allow"]
    assert r.headers["x-request-id"] == env.meta.request_id


async def _openapi(app) -> dict:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return (await c.get("/openapi.json")).json()


async def test_openapi_security_scheme(app) -> None:
    spec = await _openapi(app)
    schemes = spec["components"]["securitySchemes"]
    assert any(
        s["type"] == "apiKey" and s["in"] == "header" and s["name"] == "X-API-Key"
        for s in schemes.values()
    )


async def test_openapi_documents_errors(app) -> None:
    spec = await _openapi(app)
    resp = spec["paths"]["/v1/search"]["post"]["responses"]
    for code in ("401", "422", "500", "502", "504"):
        ref = resp[code]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("Envelope_NoneType_"), (code, ref)
    assert "404" in spec["paths"]["/v1/engines"]["get"]["responses"]
    assert "HTTPValidationError" not in str(spec["paths"])
    assert "HTTPValidationError" not in spec["components"]["schemas"]


async def test_openapi_engines_example(app) -> None:
    spec = await _openapi(app)
    ok = spec["paths"]["/v1/engines"]["get"]["responses"]["200"]
    assert "example" in str(ok["content"]["application/json"])


async def _ws(app, headers: list[tuple[bytes, bytes]]) -> list[dict]:
    sent: list[dict] = []
    inbox = [{"type": "websocket.connect"}]

    async def receive() -> dict:
        return inbox.pop(0) if inbox else {"type": "websocket.disconnect", "code": 1000}

    async def send(m: dict) -> None:
        sent.append(m)

    scope = {"type": "websocket", "path": "/ws", "headers": headers, "query_string": b""}
    # downstream has no ws route: only the auth decision matters here
    with contextlib.suppress(Exception):
        await app(scope, receive, send)
    return sent


async def test_websocket_without_key_refused(app) -> None:
    sent = await _ws(app, [])
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_websocket_with_key_passes_auth(app) -> None:
    sent = await _ws(app, [(b"x-api-key", TEST_API_KEY.encode())])
    assert {"type": "websocket.close", "code": 1008} not in sent


async def test_injected_http_client_not_closed(settings_env: None) -> None:
    settings = Settings()  # type: ignore[call-arg]
    services = build_test_services(settings)
    services.http = MagicMock(spec=httpx.AsyncClient)
    services.http.aclose = AsyncMock()
    application = create_app(settings, services=services)
    async with application.router.lifespan_context(application):
        pass
    services.http.aclose.assert_not_called()
