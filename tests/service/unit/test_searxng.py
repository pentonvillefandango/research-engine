import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from research_engine.adapters.searxng import SearxngProvider
from research_engine.errors import ServiceError
from research_engine_client.models import ErrorCode, TimeRange

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "searxng"
BASE = "http://searxng.test:8080"


async def _search(provider: SearxngProvider, pageno: int = 1):
    return await provider.search(
        "q", categories=[], engines=[], language="en-GB", time_range=None, pageno=pageno
    )


@pytest.fixture
async def provider() -> AsyncIterator[SearxngProvider]:
    async with httpx.AsyncClient() as client:
        yield SearxngProvider(BASE, client, timeout_s=5)


@respx.mock
async def test_params_with_engines(provider: SearxngProvider) -> None:
    route = respx.get(f"{BASE}/search").respond(
        json=json.loads((FIX / "technical_page1.json").read_text())
    )
    page = await provider.search(
        "q",
        categories=["it"],
        engines=["github", "stackoverflow"],
        language="en-GB",
        time_range=TimeRange.MONTH,
        pageno=2,
    )
    params = dict(route.calls.last.request.url.params)
    assert params == {
        "q": "q",
        "format": "json",
        "language": "en-GB",
        "pageno": "2",
        "safesearch": "0",
        "engines": "github,stackoverflow",
        "time_range": "month",
    }
    assert page.hits and all(h.engines for h in page.hits)
    assert min(p for h in page.hits for p in h.positions) >= 11
    assert all(h.published is None or h.published.tzinfo for h in page.hits)


@respx.mock
async def test_recorded_fixtures_parse(provider: SearxngProvider) -> None:
    for name in ("technical_page1", "general_page1"):
        body = json.loads((FIX / f"{name}.json").read_text())
        respx.get(f"{BASE}/search").respond(json=body)
        page = await _search(provider)
        assert len(page.hits) == len(body["results"]) > 0
        assert all(h.engines and h.positions and h.url for h in page.hits)
    # Real SearXNG records one position per engine, so a hit has several positions.
    assert any(len(h.positions) > 1 for h in page.hits)
    assert {u.engine for u in page.unresponsive} == {"brave", "mwmbl", "qwant"}


@respx.mock
async def test_categories_when_no_engines(provider: SearxngProvider) -> None:
    route = respx.get(f"{BASE}/search").respond(
        json={"results": [], "unresponsive_engines": [["bing", "timeout"]]}
    )
    page = await provider.search(
        "q", categories=["general"], engines=[], language="en-GB", time_range=None, pageno=1
    )
    params = dict(route.calls.last.request.url.params)
    assert params["categories"] == "general"
    assert "engines" not in params and "time_range" not in params
    assert page.unresponsive[0].engine == "bing" and page.unresponsive[0].error == "timeout"


@respx.mock
async def test_missing_fields_and_date_formats(provider: SearxngProvider) -> None:
    body = {
        "results": [
            {"url": "https://a.example/", "engine": "bing"},  # no engines, no positions
            {"url": "", "engines": ["x"]},  # no url: skipped
            {
                "url": "https://b.example/",
                "engines": ["x"],
                "positions": [3],
                "publishedDate": "2026-01-02T03:04:05",
            },  # naive
            {
                "url": "https://c.example/",
                "engines": ["x"],
                "positions": [1],
                "publishedDate": "2026-01-02T03:04:05+02:00",
            },
            {
                "url": "https://d.example/",
                "engines": ["x"],
                "positions": [1],
                "publishedDate": "not a date",
            },
        ],
        "infoboxes": [
            {
                "infobox": "Thing",
                "content": "c",
                "engine": "wikidata",
                "urls": [{"title": "t", "url": "https://w.example/"}, {"title": "no url"}],
            }
        ],
        "suggestions": ["s1"],
    }
    respx.get(f"{BASE}/search").respond(json=body)
    page = await _search(provider, pageno=3)
    assert [h.url for h in page.hits] == [
        "https://a.example/",
        "https://b.example/",
        "https://c.example/",
        "https://d.example/",
    ]
    a, b, c, d = page.hits
    assert (a.engines, a.positions) == (["bing"], [21])
    assert b.positions == [23]
    assert b.published is not None and b.published.utcoffset() is not None
    assert c.published is not None and c.published.utcoffset() is not None
    assert d.published is None
    assert page.infoboxes[0].title == "Thing" and len(page.infoboxes[0].urls) == 1
    assert page.suggestions == ["s1"]


@respx.mock
async def test_timeout_maps_to_typed_error(provider: SearxngProvider) -> None:
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ServiceError) as ei:
        await _search(provider)
    d = ei.value.detail
    assert (d.code, d.retryable, d.source, ei.value.http_status) == (
        ErrorCode.UPSTREAM_TIMEOUT,
        True,
        "searxng",
        504,
    )


@respx.mock
async def test_connect_error_is_retryable_upstream_error(provider: SearxngProvider) -> None:
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ServiceError) as ei:
        await _search(provider)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_ERROR and ei.value.detail.retryable


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(503, True), (403, False)])
async def test_http_errors(provider: SearxngProvider, status: int, retryable: bool) -> None:
    respx.get(f"{BASE}/search").respond(status)
    with pytest.raises(ServiceError) as ei:
        await _search(provider)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_ERROR
    assert ei.value.detail.retryable is retryable
    if status == 403:
        assert "search.formats" in ei.value.detail.message
    else:
        assert ei.value.http_status == 502


@respx.mock
async def test_health(provider: SearxngProvider) -> None:
    respx.get(f"{BASE}/healthz").respond(200)
    assert await provider.health() is True
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ConnectError("down"))
    assert await provider.health() is False
