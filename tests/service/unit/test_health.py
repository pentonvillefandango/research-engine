import asyncio

import httpx


async def test_health_up_without_auth(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
    body = r.json()["data"]
    assert r.status_code == 200 and body["status"] == "up"
    assert set(body["dependencies"]) == {"searxng", "crawl4ai", "database", "cache"}
    assert body["git_sha"] == "unknown"
    assert body["version"]
    cache = body["dependencies"]["cache"]
    assert cache["state"] == "up"
    assert cache["detail"].startswith("hit_rate=0.00 entries=")


async def test_degraded_and_timeout(app) -> None:
    async def down() -> bool:
        return False

    async def hang() -> bool:
        await asyncio.sleep(30)
        return True

    app.state.services.health_checks["searxng"] = down
    app.state.services.health_checks["crawl4ai"] = hang
    app.state.services.health_timeout_s = 0.1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
    body = r.json()["data"]
    assert r.status_code == 200 and body["status"] == "degraded"
    assert body["dependencies"]["searxng"]["state"] == "down"
    assert body["dependencies"]["crawl4ai"]["state"] == "down"
    assert body["dependencies"]["crawl4ai"]["detail"] == "timeout"


async def test_error_detail_never_leaks_message(app) -> None:
    async def boom() -> bool:
        raise RuntimeError("connect to http://internal-host.lab:8080 failed")

    app.state.services.health_checks["searxng"] = boom
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
    assert r.json()["data"]["dependencies"]["searxng"]["detail"] == "RuntimeError"
    assert "internal-host" not in r.text


async def test_database_down_is_503(app) -> None:
    class BrokenEngine:
        def connect(self):
            raise OSError("db file /secret/path gone")

    real = app.state.services.engine
    app.state.services.engine = BrokenEngine()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.get("/health")
    finally:
        app.state.services.engine = real
    body = r.json()
    assert r.status_code == 503 and body["data"]["status"] == "down"
    assert body["data"]["dependencies"]["database"]["detail"] == "OSError"
    assert "secret" not in r.text


async def test_concurrent_health_checks_run_once(app) -> None:
    calls = {"searxng": 0, "crawl4ai": 0}

    def counting(name: str):
        async def check() -> bool:
            calls[name] += 1
            await asyncio.sleep(0.05)
            return True

        return check

    app.state.services.health_checks["searxng"] = counting("searxng")
    app.state.services.health_checks["crawl4ai"] = counting("crawl4ai")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        rs = await asyncio.gather(*(c.get("/health") for _ in range(20)))
        assert all(r.status_code == 200 for r in rs)
        await c.get("/health")  # still inside the TTL
    assert calls == {"searxng": 1, "crawl4ai": 1}


async def test_health_rechecks_after_ttl(app) -> None:
    n = 0

    async def check() -> bool:
        nonlocal n
        n += 1
        return True

    app.state.services.health_checks["searxng"] = check
    app.state.services.health_ttl_s = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/health")
        await c.get("/health")
    assert n == 2


async def test_health_emits_no_events(app) -> None:
    before = len(await app.state.services.events.tail(1000))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/health")
    assert len(await app.state.services.events.tail(1000)) == before


async def test_version(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/version")
    assert r.json()["data"]["schema_version"] == "1.0.0"


async def test_schemas(client: httpx.AsyncClient) -> None:
    names = (await client.get("/v1/schemas")).json()["data"]
    assert "Document" in names
    r = await client.get("/v1/schemas/Document")
    assert r.headers["content-type"].startswith("application/schema+json")
    assert r.json()["title"] == "Document"
    nope = await client.get("/v1/schemas/Nope")
    assert nope.status_code == 404 and nope.json()["errors"][0]["code"] == "not_found"


async def test_schemas_need_auth(app) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/v1/schemas")).status_code == 401
        assert (await c.get("/v1/schemas/Document")).status_code == 401
