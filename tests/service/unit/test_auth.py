import httpx
from research_engine_client.models import ErrorCode


async def test_missing_key_401(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/search", json={"query": "x"})
    assert r.status_code == 401
    body = r.json()
    assert body["data"] is None and body["errors"][0]["code"] == ErrorCode.UNAUTHORIZED


async def test_401_carries_request_id(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/search", json={"query": "x"})
    rid = r.headers["x-request-id"]
    assert len(rid) == 32 and r.json()["meta"]["request_id"] == rid


async def test_wrong_key_401(app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": "nope"}
    ) as c:
        r = await c.post("/v1/search", json={"query": "x"})
    assert r.status_code == 401


async def test_openapi_is_public(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/openapi.json")
    assert r.status_code == 200
    assert "example" in str(r.json()["paths"]["/v1/search"]["post"]["requestBody"])
