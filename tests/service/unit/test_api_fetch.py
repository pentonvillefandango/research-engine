import httpx
from research_engine_client.models import Document, Envelope, ErrorCode, FetchMethod


async def test_fetch_envelope_and_cache(client: httpx.AsyncClient) -> None:
    r1 = await client.post("/v1/fetch", json={"url": "https://blog.example/post"})
    assert r1.status_code == 200, r1.text
    env = Envelope[Document].model_validate(r1.json())
    assert env.data is not None and env.data.word_count > 0
    assert env.data.provenance.method is FetchMethod.STATIC and env.data.html is None
    assert env.meta.cache_hit is False
    r2 = await client.post("/v1/fetch", json={"url": "https://blog.example/post"})
    assert r2.json()["meta"]["cache_hit"] is True


async def test_fetch_spa_escalates_through_fake_browser(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/fetch", json={"url": "https://app.example/", "formats": ["html"]})
    assert r.status_code == 200, r.text
    doc = Envelope[Document].model_validate(r.json()).data
    assert doc is not None and doc.provenance.method is FetchMethod.BROWSER and doc.html


async def test_fetch_ssrf_blocked_is_403(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/fetch", json={"url": "https://fake-blocked.example/x"})
    assert r.status_code == 403
    body = r.json()
    assert body["data"] is None and body["errors"][0]["code"] == ErrorCode.SSRF_BLOCKED


async def test_fetch_validation(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/fetch", json={"url": "ftp://x.example/"})
    assert r.status_code == 422
    assert r.json()["errors"][0]["code"] == ErrorCode.INVALID_REQUEST


async def test_fetch_requires_api_key(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/fetch", json={"url": "https://blog.example/post"}, headers={"X-API-Key": "wrong"}
    )
    assert r.status_code == 401


async def test_openapi_documents_fetch_errors(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    responses = spec["paths"]["/v1/fetch"]["post"]["responses"]
    assert {"403", "413", "415", "502", "504"} <= set(responses)
    assert "extraction_failed" in responses["502"]["description"]


async def test_fetch_unknown_page_is_404_error_not_document(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/fetch", json={"url": "https://blog.example/missing"})
    assert r.status_code == 502 and r.json()["errors"][0]["code"] == ErrorCode.FETCH_FAILED


async def test_unavailable_robots_is_403_retryable(app, client: httpx.AsyncClient) -> None:
    """robots.txt answering 5xx: HTTP 403, ``robots_disallowed``, ``retryable: true``."""
    from research_engine.safety.limiter import DomainLimiter
    from research_engine.safety.robots import RobotsPolicy
    from research_engine.safety.ssrf import SsrfGuard

    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as http:
        guard = SsrfGuard(frozenset(), resolver=public)
        app.state.services.fetch._robots = RobotsPolicy(http, "UA/1", DomainLimiter(2, 0), guard)
        r = await client.post("/v1/fetch", json={"url": "https://blog.example/post"})
    assert r.status_code == 403
    body = r.json()
    assert body["data"] is None
    err = body["errors"][0]
    assert err["code"] == ErrorCode.ROBOTS_DISALLOWED and err["retryable"] is True
    assert "unavailable (HTTP 503)" in err["message"] and "retry later" in err["message"]
