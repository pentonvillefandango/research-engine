"""Per-request API usage log (V1-24): middleware, batched writer, pruning, stats and paging."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog.testing
from research_engine.gui.session import COOKIE, SessionCodec
from research_engine.keys.usage import RequestRecord, UsageLog
from research_engine.store.db import create_engine_for, init_db
from sqlalchemy import text

from tests.conftest import TEST_API_KEY, TEST_ENV

MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}


def raw(app, key: str | None, ip: str = "10.1.2.3") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 4000)),
        base_url="http://research.localhost",
        headers={"X-API-Key": key} if key else {},
    )


async def rows(app) -> list[dict[str, Any]]:
    await app.state.services.usage.flush()
    async with app.state.services.engine.connect() as conn:
        res = await conn.execute(text("SELECT * FROM api_request ORDER BY id"))
        return [dict(r._mapping) for r in res]


def _rec(ts: datetime | None = None, **over: Any) -> RequestRecord:
    base: dict[str, Any] = {
        "ts": ts or datetime.now(UTC),
        "caller_kind": "env",
        "key_id": None,
        "caller_name": "env (built-in)",
        "method": "GET",
        "path": "/v1/x",
        "tool": None,
        "status": 200,
        "duration_ms": 1,
        "client_ip": "1.1.1.1",
    }
    return RequestRecord(**(base | over))


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 3, "method": method, "params": params or {}}


async def test_named_key_request_logged(app) -> None:
    row, key = await app.state.services.keys.create("newsdesk")
    async with raw(app, key) as c:
        r = await c.post("/v1/search", json={"query": "zebra-unique-term", "depth": "quick"})
        assert r.status_code == 200
    [r] = await rows(app)
    assert (r["caller_kind"], r["key_id"], r["caller_name"]) == ("key", row.id, "newsdesk")
    assert (r["method"], r["path"], r["status"], r["client_ip"]) == (
        "POST",
        "/v1/search",
        200,
        "10.1.2.3",
    )
    assert r["duration_ms"] >= 0 and r["tool"] is None
    assert "zebra-unique-term" not in repr(r)


async def test_query_string_not_stored(app) -> None:
    async with raw(app, TEST_API_KEY) as c:
        await c.get("/v1/engines", params={"q": "zebra-unique-term"})
    [r] = await rows(app)
    assert r["path"] == "/v1/engines" and "zebra-unique-term" not in repr(r)


async def test_unauthenticated_and_open_not_logged(app) -> None:
    async with raw(app, None) as c:
        assert (await c.post("/v1/search", json={"query": "x"})).status_code == 401
        await c.get("/health")
    async with raw(app, TEST_API_KEY) as c:
        await c.get("/health")
    assert await rows(app) == []


async def test_mcp_tool_call_logs_tool_name(app) -> None:
    async with raw(app, TEST_API_KEY) as c:
        call = _rpc("tools/call", {"name": "web_search", "arguments": {"query": "zebra-unique"}})
        r1 = await c.post("/mcp", json=call, headers=MCP_ACCEPT)
        assert r1.status_code == 200, r1.text
        r2 = await c.post("/mcp", json=_rpc("tools/list"), headers=MCP_ACCEPT)
        assert r2.status_code == 200, r2.text
    got = await rows(app)
    assert [(r["path"], r["tool"], r["caller_kind"]) for r in got] == [
        ("/mcp", "web_search", "env"),
        ("/mcp", None, "env"),
    ]
    assert "zebra-unique" not in repr(got)


async def test_gui_cookie_logged_as_gui_and_pages_not_logged(app) -> None:
    cookie = SessionCodec(TEST_ENV["SESSION_SECRET"]).issue()
    async with raw(app, None) as c:
        c.cookies.set(COOKIE, cookie)
        assert (await c.get("/v1/engines")).status_code == 200
        assert (await c.get("/")).status_code == 200
        await c.get("/keys")
    [r] = await rows(app)
    assert (r["caller_kind"], r["caller_name"], r["key_id"], r["path"]) == (
        "gui",
        "GUI session",
        None,
        "/v1/engines",
    )


async def test_overflow_drops(app) -> None:
    log = UsageLog(app.state.services.engine, max_queue=2)
    for _ in range(3):
        log.record(_rec())
    assert log.dropped == 1


async def test_failing_flush_logs_and_does_not_raise(settings_env: None) -> None:
    engine = create_engine_for(":memory:")  # no init_db: the INSERT fails (no such table)
    log = UsageLog(engine)
    log.record(_rec())
    with structlog.testing.capture_logs() as logs:
        assert await log.flush() == 0
    await engine.dispose()
    assert any(e["event"] == "request log flush failed" for e in logs)


async def test_prune(app) -> None:
    usage = app.state.services.usage
    usage.record(_rec(datetime.now(UTC) - timedelta(days=31)))
    usage.record(_rec())
    await usage.flush()
    assert await usage.prune(30) == 1
    assert len(await rows(app)) == 1


async def test_key_stats(app) -> None:
    usage = app.state.services.usage
    now = datetime.now(UTC)
    usage.record(_rec(now - timedelta(days=40)))  # outside 30 days
    usage.record(_rec(now - timedelta(days=3), client_ip="2.2.2.2"))
    usage.record(_rec(now - timedelta(hours=1), client_ip="3.3.3.3"))
    usage.record(_rec(now - timedelta(days=2), caller_kind="gui", caller_name="GUI session"))
    usage.record(
        _rec(
            now - timedelta(minutes=5), caller_kind="key", key_id=7, caller_name="k", client_ip="9"
        )
    )
    usage.record(
        _rec(
            now - timedelta(minutes=9), caller_kind="key", key_id=7, caller_name="k", client_ip="8"
        )
    )
    await usage.flush()
    stats = await usage.key_stats()
    assert set(stats) == {"env", "gui", "key:7"}
    env = stats["env"]
    assert (env.last_24h, env.last_30d, env.last_ip) == (1, 2, "3.3.3.3")
    assert env.last_used is not None and env.last_used.tzinfo is UTC
    assert abs((env.last_used - (now - timedelta(hours=1))).total_seconds()) < 1
    assert (stats["gui"].last_24h, stats["gui"].last_30d) == (0, 1)
    assert (stats["key:7"].last_24h, stats["key:7"].last_30d, stats["key:7"].last_ip) == (
        2,
        2,
        "9",
    )


async def test_recent_filters_and_paging(app) -> None:
    row, key = await app.state.services.keys.create("pager")
    async with raw(app, key) as c:
        assert (await c.post("/v1/search", json={"query": ""})).status_code == 422
        await c.get("/v1/engines")
    async with raw(app, TEST_API_KEY) as c:
        await c.get("/v1/engines")
    await app.state.services.usage.flush()
    usage = app.state.services.usage
    every = await usage.recent()
    assert [r.caller_kind for r in every] == ["env", "key", "key"]  # newest first
    mine = await usage.recent(caller=f"key:{row.id}")
    assert [r.status for r in mine] == [200, 422]
    assert [r.status for r in await usage.recent(errors_only=True)] == [422]
    assert [r.caller_kind for r in await usage.recent(caller="env")] == ["env"]
    assert await usage.recent(caller="gui") == []
    assert len(await usage.recent(limit=1)) == 1
    older = await usage.recent(before_id=every[0].id)
    assert [r.id for r in older] == [r.id for r in every[1:]]


async def test_stop_flushes_queued_rows(settings_env: None, tmp_path) -> None:
    engine = create_engine_for(str(tmp_path / "u.db"))
    await init_db(engine)
    log = UsageLog(engine, interval_s=3600)
    log.start()
    log.record(_rec())
    await log.stop()
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM api_request"))).scalar_one()
    await engine.dispose()
    assert n == 1


async def test_record_failure_never_fails_the_request(app, monkeypatch) -> None:
    def boom(_rec: RequestRecord) -> None:
        raise RuntimeError("log broken")

    monkeypatch.setattr(app.state.services.usage, "record", boom)
    async with raw(app, TEST_API_KEY) as c:
        assert (await c.get("/v1/engines")).status_code == 200
