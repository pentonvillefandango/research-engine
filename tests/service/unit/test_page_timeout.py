"""PAGE_TIMEOUT_S is the operator's upper bound on any single page fetch (v1.0.1).

Every fetch's effective timeout is ``min(request timeout_s, PAGE_TIMEOUT_S)``, on the REST
fetch path and for job page fetches (batch fetch, search_read) alike.
"""

import httpx
import pytest
from fastapi import FastAPI
from research_engine.adapters.fetch import Fetcher, HopHook, RawPage
from research_engine.config import Settings
from research_engine_client.models import FetchMode, FetchRequest

from .test_fetch_service import html_page, make

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
