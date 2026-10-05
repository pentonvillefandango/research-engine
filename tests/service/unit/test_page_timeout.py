"""PAGE_TIMEOUT_S is the operator's upper bound on any single page fetch (v1.0.1).

Every fetch's effective timeout is ``min(request timeout_s, PAGE_TIMEOUT_S)``, on the REST
fetch path and for job page fetches (batch fetch, search_read) alike.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from agents.mcp import MCPServerStreamableHttp
from fastapi import FastAPI
from research_engine.adapters.fetch import Fetcher, HopHook, RawPage
from research_engine.config import Settings
from research_engine_client.models import FetchMethod, FetchMode, FetchRequest

from tests.conftest import TEST_API_KEY

from .test_fetch_service import _thin, html_page, make

CAP = 7


class Recording:
    """Wraps a Fetcher and records the ``timeout_s`` each call was given."""

    def __init__(self, inner: Fetcher) -> None:
        self.inner = inner
        self.timeouts: list[float] = []

    async def fetch(self, url: str, *, timeout_s: float, on_hop: HopHook | None = None) -> RawPage:
        self.timeouts.append(timeout_s)
        return await self.inner.fetch(url, timeout_s=timeout_s, on_hop=on_hop)

    async def health(self) -> bool:
        return await self.inner.health()


@pytest.fixture
def capped(settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAGE_TIMEOUT_S", str(CAP))


def _record_static(app: FastAPI) -> Recording:
    svc = app.state.services.fetch
    rec = Recording(svc._static)
    svc._static = rec
    return rec


async def test_service_caps_request_timeout(settings_env: None) -> None:
    settings = Settings(page_timeout_s=CAP)  # type: ignore[call-arg]
    url = "https://blog.example/post"
    svc, s, _, _ = make({url: html_page(url, "article.html")}, {}, settings)
    await svc.fetch(FetchRequest(url=url, mode=FetchMode.STATIC, timeout_s=60))
    assert s.timeouts == [CAP]


async def test_service_keeps_shorter_request_timeout(settings_env: None) -> None:
    settings = Settings(page_timeout_s=CAP)  # type: ignore[call-arg]
    url = "https://blog.example/post"
    svc, s, _, _ = make({url: html_page(url, "article.html")}, {}, settings)
    await svc.fetch(FetchRequest(url=url, mode=FetchMode.STATIC, timeout_s=3))
    assert s.timeouts == [3]


async def test_auto_mode_budget_split_uses_capped_timeout(settings_env: None) -> None:
    settings = Settings(page_timeout_s=30)  # type: ignore[call-arg]
    url = "https://blog.example/post"
    svc, s, _, _ = make({url: html_page(url, "article.html")}, {}, settings)
    await svc.fetch(FetchRequest(url=url, timeout_s=300))
    assert s.timeouts == [10]  # a third of the capped 30 s, not 100 (a third of 300)


async def test_browser_mode_is_capped(settings_env: None) -> None:
    """The browser (Crawl4AI ``page_timeout``) gets the capped budget, less the reserve."""
    settings = Settings(page_timeout_s=CAP)  # type: ignore[call-arg]
    url = "https://app.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, _, b, _ = make({}, {url: rendered}, settings)
    await svc.fetch(FetchRequest(url=url, mode=FetchMode.BROWSER, timeout_s=120))
    assert len(b.timeouts) == 1 and CAP - 1.5 < b.timeouts[0] <= CAP


async def test_escalated_browser_gets_capped_remainder(settings_env: None) -> None:
    settings = Settings(page_timeout_s=CAP)  # type: ignore[call-arg]
    url = "https://thin.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, s, b, _ = make({url: _thin(url)}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url, timeout_s=120))
    assert doc.provenance.method is FetchMethod.BROWSER
    assert s.timeouts[0] <= CAP * 2 / 3
    assert 0 < b.timeouts[0] <= CAP


@pytest.fixture
async def capped_mcp(capped: None, live_server: str) -> AsyncIterator[MCPServerStreamableHttp]:
    server = MCPServerStreamableHttp(
        name="re",
        params={"url": f"{live_server}/mcp", "headers": {"X-API-Key": TEST_API_KEY}},
        client_session_timeout_seconds=20,
    )
    async with server:
        yield server


async def test_mcp_web_fetch_is_capped(
    capped: None, app: FastAPI, capped_mcp: MCPServerStreamableHttp
) -> None:
    rec = _record_static(app)
    res = await capped_mcp.call_tool(
        "web_fetch", {"url": "https://blog.example/post", "mode": "static"}
    )
    assert not res.is_error
    assert rec.timeouts == [CAP]  # web_fetch's default 60 s request, capped


async def test_rest_fetch_is_capped(capped: None, app: FastAPI, client: httpx.AsyncClient) -> None:
    rec = _record_static(app)
    r = await client.post(
        "/v1/fetch",
        json={"url": "https://blog.example/post", "mode": "static", "timeout_s": 120},
    )
    assert r.status_code == 200
    assert rec.timeouts == [CAP]


async def test_batch_job_fetches_are_capped(
    capped: None, app: FastAPI, client: httpx.AsyncClient
) -> None:
    rec = _record_static(app)
    r = await client.post(
        "/v1/fetch/batch",
        json={"urls": ["https://blog.example/post"], "mode": "static", "timeout_s": 120},
    )
    detail = (await client.get(r.headers["location"], params={"wait": 10})).json()["data"]
    assert detail["job"]["status"] == "done"
    assert rec.timeouts == [CAP]


async def test_search_read_job_fetches_are_capped(
    capped: None, app: FastAPI, client: httpx.AsyncClient
) -> None:
    rec = _record_static(app)
    r = await client.post(
        "/v1/search_read",
        json={
            "search": {"query": "q"},
            "top_n": 2,
            "fetch": {"mode": "static", "timeout_s": 120},
        },
    )
    detail = (await client.get(r.headers["location"], params={"wait": 10})).json()["data"]
    assert detail["job"]["status"] in {"done", "partial"}
    assert rec.timeouts and all(t == CAP for t in rec.timeouts)
