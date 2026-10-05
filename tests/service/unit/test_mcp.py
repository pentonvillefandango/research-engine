"""MCP server at /mcp (V1-12): four structured-output tools over streamable HTTP, behind the
API-key middleware, driven model-free by the OpenAI Agents SDK's real MCP client (B6a)."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import structlog.testing
from agents.mcp import MCPServerStreamableHttp
from fastapi import FastAPI
from mcp.types import CallToolResult, TextContent
from research_engine import __version__
from research_engine.gui.session import COOKIE, SessionCodec
from research_engine.shutdown import begin_shutdown
from research_engine_client.models import (
    Document,
    JobDetail,
    JobStatus,
    SearchResponse,
)

TOOLS = {"web_search", "web_fetch", "search_and_read", "get_job"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "X-API-Key": "test-key"}
MODERN = "2026-07-28"  # the newer protocol revision mcp 2.3.0 also serves
LISTEN = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "subscriptions/listen",
    "params": {
        "notifications": {"toolsListChanged": True},
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
        },
    },
}
LISTEN_HEADERS = MCP_HEADERS | {
    "MCP-Protocol-Version": MODERN,
    "Mcp-Method": "subscriptions/listen",
}


@pytest.fixture
async def mcp_client(live_server: str) -> AsyncIterator[MCPServerStreamableHttp]:
    server = MCPServerStreamableHttp(
        name="re",
        params={"url": f"{live_server}/mcp", "headers": {"X-API-Key": "test-key"}},
        client_session_timeout_seconds=20,
    )
    async with server:
        yield server


def _text(res: CallToolResult) -> str:
    return "\n".join(c.text for c in res.content if isinstance(c, TextContent))


# --- auth and transport -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key"}])
async def test_mcp_requires_key(
    live_server: str, headers: dict[str, str], method: str, path: str
) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.request(method, f"{live_server}{path}", json=INIT, headers=headers)
    assert r.status_code == 401
    body = r.json()
    assert body["errors"][0]["code"] == "unauthorized"
    assert body["data"] is None


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_gui_session_cookie_does_not_authenticate_mcp(live_server: str, path: str) -> None:
    """MCP clients hold the API key, never the GUI cookie: a valid cookie with a same-origin
    Origin is still 401 on /mcp (while /v1 keeps accepting it, B2)."""
    cookie = SessionCodec("test-session-secret-0123456789abcdef").issue()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Cookie": f"{COOKIE}={cookie}",
        "Origin": live_server,
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{live_server}{path}", json=INIT, headers=headers)
        assert r.status_code == 401
        assert r.json()["errors"][0]["code"] == "unauthorized"
        v1 = await c.get(f"{live_server}/v1/jobs/{'0' * 32}", headers=headers)
    assert v1.status_code == 404  # authenticated by the cookie, then not found


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
async def test_mcp_is_post_only(live_server: str, method: str, path: str) -> None:
    """Stateless mode has no use for GET (an idle SSE stream) or DELETE (session end)."""
    async with httpx.AsyncClient() as c, asyncio.timeout(5):
        r = await c.request(method, f"{live_server}{path}", headers=MCP_HEADERS)
    assert r.status_code == 405


async def test_subscriptions_listen_is_not_served(live_server: str) -> None:
    """Our tools are static; a listen stream would only hold uvicorn's graceful drain."""
    async with (
        httpx.AsyncClient() as c,
        asyncio.timeout(5),
        c.stream("POST", f"{live_server}/mcp", json=LISTEN, headers=LISTEN_HEADERS) as r,
    ):
        assert not r.headers.get("content-type", "").startswith("text/event-stream")
        body = json.loads(await r.aread())
    assert "error" in body and "result" not in body


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
async def test_initialize_with_key_no_redirect(live_server: str, path: str) -> None:
    """Both spellings serve MCP directly: a 307 could drop X-API-Key in some clients."""
    async with httpx.AsyncClient(follow_redirects=False) as c:
        r = await c.post(f"{live_server}{path}", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")  # json_response=True
    result = r.json()["result"]
    assert result["serverInfo"]["name"] == "research-engine"
    assert result["serverInfo"]["version"] == __version__
    instructions = result["instructions"]
    assert "untrusted" in instructions and "get_job" in instructions


async def test_dns_rebinding_protection_rejects_foreign_host(live_server: str) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{live_server}/mcp", json=INIT, headers=MCP_HEADERS | {"Host": "evil.example"}
        )
    assert r.status_code == 421


async def test_dns_rebinding_protection_rejects_foreign_origin(live_server: str) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{live_server}/mcp",
            json=INIT,
            headers=MCP_HEADERS | {"Origin": "https://evil.example"},
        )
    assert r.status_code == 403


async def test_good_origin_with_foreign_host_is_rejected(live_server: str) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{live_server}/mcp",
            json=INIT,
            headers=MCP_HEADERS | {"Host": "evil.example", "Origin": "https://research.localhost"},
        )
    assert r.status_code == 421


@pytest.mark.parametrize(
    "host", ["research.localhost", "research.localhost:8443", "localhost:1234", "127.0.0.1"]
)
async def test_allowed_hosts(live_server: str, host: str) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{live_server}/mcp",
            json=INIT,
            headers=MCP_HEADERS | {"Host": host, "Origin": f"https://{host}"},
        )
    assert r.status_code == 200, r.text


# --- OpenAI Agents SDK client end to end (B6a) ----------------------------------------------


async def test_openai_agents_client_end_to_end(mcp_client: MCPServerStreamableHttp) -> None:
    tools = {t.name: t for t in await mcp_client.list_tools()}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.output_schema, tool.name
        assert tool.description, tool.name
    # The published output schema is exactly the public model's JSON schema.
    assert tools["web_search"].output_schema == SearchResponse.model_json_schema()
    assert tools["web_fetch"].output_schema == Document.model_json_schema()
    assert tools["search_and_read"].output_schema == JobDetail.model_json_schema()
    assert tools["get_job"].output_schema == JobDetail.model_json_schema()

    res = await mcp_client.call_tool("web_search", {"query": "vector db", "depth": "quick"})
    assert not res.is_error, _text(res)
    found = SearchResponse.model_validate(res.structured_content)
    assert found.query == "vector db" and found.results

    doc = await mcp_client.call_tool("web_fetch", {"url": "https://blog.example/post"})
    assert not doc.is_error, _text(doc)
    document = Document.model_validate(doc.structured_content)
    assert document.markdown

    bad = await mcp_client.call_tool("web_fetch", {"url": "https://fake-blocked.example/"})
    assert bad.is_error
    assert "ssrf_blocked" in _text(bad)
    assert bad.structured_content is None


async def test_search_and_read_then_get_job(mcp_client: MCPServerStreamableHttp) -> None:
    res = await mcp_client.call_tool("search_and_read", {"query": "vector db", "top_n": 2})
    assert not res.is_error, _text(res)
    detail = JobDetail.model_validate(res.structured_content)
    assert detail.job.status in (JobStatus.DONE, JobStatus.PARTIAL)
    assert detail.result is not None and detail.result.kind == "search_read"

    again = await mcp_client.call_tool("get_job", {"job_id": detail.job.id})
    assert not again.is_error, _text(again)
    assert JobDetail.model_validate(again.structured_content).job.id == detail.job.id


async def test_get_job_unknown_and_malformed_ids(
    app: FastAPI, mcp_client: MCPServerStreamableHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = await mcp_client.call_tool("get_job", {"job_id": "0" * 32})
    assert missing.is_error and "not_found" in _text(missing)

    jobs = app.state.services.jobs

    async def no_lookup(*_a: object, **_k: object) -> None:
        raise AssertionError("a malformed id must not reach the DB")

    monkeypatch.setattr(jobs, "get", no_lookup)
    monkeypatch.setattr(jobs, "wait", no_lookup)
    for job_id in ("../etc/passwd", "X" * 200, ""):
        bad = await mcp_client.call_tool("get_job", {"job_id": job_id, "wait_s": 5})
        assert bad.is_error and "not_found" in _text(bad), job_id
        assert len(_text(bad)) < 200  # the id is truncated in the message


# --- input validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("web_search", {"query": ""}),
        ("web_search", {"query": "   "}),
        ("web_search", {"query": "x", "max_results": 0}),
        ("web_search", {"query": "x", "language": "english please"}),
        ("web_search", {"query": "x", "intent": "nonsense"}),
        ("web_fetch", {"url": "ftp://files.example/a.txt"}),
        ("web_fetch", {"url": "not a url"}),
        ("web_fetch", {"url": "https://blog.example/post", "mode": "teleport"}),
        ("search_and_read", {"query": "x", "top_n": 999}),
        ("search_and_read", {"query": "x", "top_n": 0}),
        ("search_and_read", {"query": ""}),
    ],
)
async def test_invalid_arguments_are_error_results(
    mcp_client: MCPServerStreamableHttp, tool: str, args: dict[str, Any]
) -> None:
    res = await mcp_client.call_tool(tool, args)
    assert res.is_error, _text(res)
    assert res.structured_content is None
    assert _text(res)
    # The session survives: the next call works.
    ok = await mcp_client.call_tool("get_job", {"job_id": "0" * 32})
    assert "not_found" in _text(ok)


async def test_model_validation_failure_is_invalid_request(
    mcp_client: MCPServerStreamableHttp,
) -> None:
    res = await mcp_client.call_tool("web_fetch", {"url": "ftp://files.example/a.txt"})
    assert res.is_error
    assert _text(res).startswith("invalid_request: ")
    assert "url" in _text(res)


# --- error mapping --------------------------------------------------------------------------


async def test_unexpected_exception_does_not_leak(
    app: FastAPI, mcp_client: MCPServerStreamableHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("secret internal detail /var/lib/db")

    monkeypatch.setattr(app.state.services.search, "search", boom)
    with structlog.testing.capture_logs() as logs:
        res = await mcp_client.call_tool("web_search", {"query": "x"})
    assert res.is_error
    assert _text(res) == "internal_error: internal error"
    assert "secret" not in json.dumps(res.model_dump(mode="json"))
    logged = [e for e in logs if e.get("tool") == "web_search"]
    assert logged and logged[0]["log_level"] == "error" and logged[0].get("exc_info")


async def test_validation_error_inside_a_service_is_internal(
    app: FastAPI, mcp_client: MCPServerStreamableHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only building the request from the tool's arguments is ``invalid_request``; a model
    failing validation deeper down is a bug, reported without its field paths."""

    async def bad(*_a: object, **_k: object) -> None:
        SearchResponse.model_validate({"internal_field": 1})

    monkeypatch.setattr(app.state.services.search, "search", bad)
    res = await mcp_client.call_tool("web_search", {"query": "x"})
    assert res.is_error
    assert _text(res) == "internal_error: internal error"


async def test_service_error_text_has_code_and_message(
    mcp_client: MCPServerStreamableHttp,
) -> None:
    res = await mcp_client.call_tool("web_fetch", {"url": "https://fake-blocked.example/"})
    text = _text(res)
    assert text.startswith("ssrf_blocked: ")
    assert "non-public address" in text


# --- wait clamping and shutdown -------------------------------------------------------------


async def test_wait_s_is_clamped(
    app: FastAPI, mcp_client: MCPServerStreamableHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = app.state.services.jobs
    real_wait = jobs.wait
    seen: list[float] = []

    async def wait(job_id: str, timeout_s: float) -> JobDetail | None:
        seen.append(timeout_s)
        return await real_wait(job_id, 0)

    monkeypatch.setattr(jobs, "wait", wait)
    res = await mcp_client.call_tool("search_and_read", {"query": "x", "wait_s": 999})
    job_id = JobDetail.model_validate(res.structured_content).job.id
    await mcp_client.call_tool("search_and_read", {"query": "x", "wait_s": -5})
    await mcp_client.call_tool("get_job", {"job_id": job_id, "wait_s": 999})
    assert seen == [60, 0, 60]
    get_calls: list[str] = []
    real_get = jobs.get

    async def get(jid: str) -> JobDetail | None:
        get_calls.append(jid)
        return await real_get(jid)

    monkeypatch.setattr(jobs, "get", get)
    await mcp_client.call_tool("get_job", {"job_id": job_id, "wait_s": -1})
    assert get_calls == [job_id] and seen == [60, 0, 60]  # negative -> plain get, no wait


async def test_begin_shutdown_releases_in_flight_search_and_read(
    app: FastAPI, mcp_client: MCPServerStreamableHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long ``wait_s`` must not hold uvicorn's graceful drain: the SIGTERM hook wakes job
    long-polls, so the tool returns the job's current (non-terminal) state at once."""
    stuck = asyncio.Event()

    async def hang(*_a: object, **_k: object) -> None:
        await stuck.wait()

    monkeypatch.setattr(app.state.services.search, "search", hang)
    call = asyncio.create_task(
        mcp_client.call_tool("search_and_read", {"query": "x", "wait_s": 60})
    )
    await asyncio.sleep(0.3)
    assert not call.done()
    t0 = time.perf_counter()
    begin_shutdown(app)
    res = await asyncio.wait_for(call, 5)
    assert time.perf_counter() - t0 < 2
    assert not res.is_error, _text(res)
    detail = JobDetail.model_validate(res.structured_content)
    assert detail.job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    stuck.set()


async def test_mcp_session_manager_failure_is_a_startup_failure(
    settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    from research_engine.app import create_app
    from research_engine.config import Settings
    from research_engine.testing import build_test_services

    settings = Settings()  # type: ignore[call-arg]
    services = build_test_services(settings)
    application = create_app(settings, services=services)
    stopped: list[bool] = []
    real_stop = services.jobs.stop

    async def stop() -> None:
        stopped.append(True)
        await real_stop()

    @asynccontextmanager
    async def broken_run() -> AsyncIterator[None]:
        raise RuntimeError("mcp boom")
        yield  # pragma: no cover

    monkeypatch.setattr(services.jobs, "stop", stop)
    monkeypatch.setattr(application.state.mcp.session_manager, "run", broken_run)
    with pytest.raises(RuntimeError, match="mcp boom"):
        async with application.router.lifespan_context(application):
            pytest.fail("lifespan should not yield")
    assert stopped == [True]  # cleanup ran; the original error propagated
