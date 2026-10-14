import httpx
from fastapi import Request
from research_engine.gui.session import COOKIE
from research_engine.keys.models import CallerKind

from tests.conftest import TEST_API_KEY

ORIGIN = {"Origin": "http://research.localhost"}
SEARCH = {"query": "x", "depth": "quick"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}


def raw(app, key: str | None) -> httpx.AsyncClient:
    headers = {"X-API-Key": key} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://research.localhost",
        headers=headers,
    )


async def test_named_key_then_revoked(app) -> None:
    row, key = await app.state.services.keys.create("newsdesk")
    async with raw(app, key) as c:
        assert (await c.post("/v1/search", json=SEARCH)).status_code == 200
        await app.state.services.keys.revoke(row.id)
        assert (await c.post("/v1/search", json=SEARCH)).status_code == 401


async def test_mcp_named_key_then_revoked(live_server: str, app) -> None:
    row, key = await app.state.services.keys.create("agent")
    headers = MCP_ACCEPT | {"X-API-Key": key}
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{live_server}/mcp", json=LIST_TOOLS, headers=headers)
        assert r.status_code == 200
        assert "web_search" in r.text
        await app.state.services.keys.revoke(row.id)
        r = await c.post(f"{live_server}/mcp", json=LIST_TOOLS, headers=headers)
        assert r.status_code == 401


async def test_caller_in_state(app) -> None:
    @app.get("/v1/_whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"kind": request.state.caller.kind, "name": request.state.caller.name}

    _, key = await app.state.services.keys.create("laptop")
    async with raw(app, key) as c:
        assert (await c.get("/v1/_whoami")).json() == {"kind": CallerKind.KEY, "name": "laptop"}
    async with raw(app, TEST_API_KEY) as c:
        assert (await c.get("/v1/_whoami")).json()["kind"] == CallerKind.ENV
    async with raw(app, None) as c:
        login = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"}, headers=ORIGIN)
        assert login.status_code == 303
        assert COOKIE in c.cookies
        assert (await c.get("/v1/_whoami")).json()["kind"] == CallerKind.GUI


async def test_named_key_does_not_log_in_to_gui(app) -> None:
    _, key = await app.state.services.keys.create("nogui")
    async with raw(app, None) as c:
        r = await c.post("/login", data={"api_key": key, "next": "/"}, headers=ORIGIN)
    assert r.status_code != 303
