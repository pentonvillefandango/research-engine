import re

import httpx
from research_engine.errors import ServiceError
from research_engine_client.models import EnginesResponse, Envelope, ErrorCode, SearchResponse

from tests.conftest import TEST_API_KEY


async def test_search_envelope_and_cache(client: httpx.AsyncClient) -> None:
    r1 = await client.post("/v1/search", json={"query": "vector db", "depth": "quick"})
    assert r1.status_code == 200
    env = Envelope[SearchResponse].model_validate(r1.json())
    assert env.data is not None and len(env.data.results) > 0
    assert re.fullmatch(r"[0-9a-f]{32}", env.meta.request_id)
    assert r1.headers["x-request-id"] == env.meta.request_id
    assert env.meta.cache_hit is False
    r2 = await client.post("/v1/search", json={"query": "vector db", "depth": "quick"})
    assert r2.json()["meta"]["cache_hit"] is True


async def test_validation_error_envelope(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/search", json={"query": "  "})
    assert r.status_code == 422
    err = r.json()["errors"][0]
    assert err["code"] == ErrorCode.INVALID_REQUEST and "query" in err["message"]


async def test_service_error_envelope(app, client: httpx.AsyncClient, monkeypatch) -> None:
    async def boom(*a, **k):
        raise ServiceError.of(
            ErrorCode.UPSTREAM_TIMEOUT, "slow", retryable=True, source="searxng", http_status=504
        )

    monkeypatch.setattr(app.state.services.search, "search", boom)
    r = await client.post("/v1/search", json={"query": "x"})
    assert r.status_code == 504 and r.json()["errors"][0]["source"] == "searxng"
    assert r.json()["data"] is None


async def test_unexpected_error_hidden(app, client: httpx.AsyncClient, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(app.state.services.search, "search", boom)
    r = await client.post("/v1/search", json={"query": "x"})
    assert r.status_code == 500
    assert r.json()["errors"][0]["code"] == ErrorCode.INTERNAL_ERROR
    assert "secret internals" not in r.text


async def test_engines(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/engines")
    env = Envelope[EnginesResponse].model_validate(r.json())
    assert env.data is not None
    names = [e.name for e in env.data.engines]
    assert names == sorted(names) and "github" in names
    assert len(env.data.intents) == 7


async def test_unexpected_error_logged_without_secrets(
    app,
    client: httpx.AsyncClient,
    monkeypatch,
    capsys,
) -> None:
    async def boom(*a, **k):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(app.state.services.search, "search", boom)
    r = await client.post("/v1/search", json={"query": "x"})
    assert r.headers["x-request-id"] == r.json()["meta"]["request_id"]
    out = capsys.readouterr().out
    assert "secret internals" in out and "RuntimeError" in out  # traceback logged
    assert TEST_API_KEY not in out  # frame locals (request headers) are not
