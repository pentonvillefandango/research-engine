import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from research_engine.adapters.crawl4ai import Crawl4AIFetcher
from research_engine.errors import ServiceError
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, FetchMethod

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "crawl4ai" / "crawl_example.json"
BASE = "http://crawl4ai.test:11235"


async def _resolve(host: str) -> list[str]:
    return {"internal.example": ["10.0.0.9"]}.get(host, ["93.184.216.34"])


async def _no_sleep(_s: float) -> None:
    return None


@pytest.fixture
def fetcher() -> Crawl4AIFetcher:
    return Crawl4AIFetcher(
        BASE, "tok", httpx.AsyncClient(), SsrfGuard(frozenset(), resolver=_resolve), sleep=_no_sleep
    )


def _one(result: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "results": [result]}


@respx.mock
async def test_crawl_ok(fetcher: Crawl4AIFetcher) -> None:
    route = respx.post(f"{BASE}/crawl").respond(json=json.loads(FIX.read_text()))
    page = await fetcher.fetch("https://example.com", timeout_s=30)
    req = route.calls.last.request
    sent = json.loads(req.content)
    assert req.headers["authorization"] == "Bearer tok"
    assert sent["urls"] == ["https://example.com"]
    assert sent["crawler_config"]["params"]["page_timeout"] == 30000
    assert "proxy" not in json.dumps(sent) and "extra_args" not in json.dumps(sent)
    assert page.method is FetchMethod.BROWSER and page.html and page.markdown


@respx.mock
async def test_recorded_fixture_shape(fetcher: Crawl4AIFetcher) -> None:
    """The real recorded response: markdown is a dict, redirected_url adds a trailing slash."""
    raw = json.loads(FIX.read_text())
    respx.post(f"{BASE}/crawl").respond(json=raw)
    page = await fetcher.fetch("https://example.com", timeout_s=30)
    rec = raw["results"][0]
    assert page.url == "https://example.com"
    assert page.html == rec["html"] and page.body == rec["html"].encode()
    assert page.markdown == rec["markdown"]["raw_markdown"]
    assert page.final_url == "https://example.com/"
    assert page.status == 200 and page.content_type == "text/html"


@respx.mock
async def test_string_markdown_and_default_status(fetcher: Crawl4AIFetcher) -> None:
    respx.post(f"{BASE}/crawl").respond(
        json=_one(
            {"url": "https://example.com/", "success": True, "html": "<p>x</p>", "markdown": "md"}
        )
    )
    page = await fetcher.fetch("https://example.com/", timeout_s=30)
    assert page.markdown == "md" and page.status == 200
    assert page.final_url == "https://example.com/" and page.redirects == []


@respx.mock
async def test_sends_checked_url_not_raw_input(fetcher: Crawl4AIFetcher) -> None:
    route = respx.post(f"{BASE}/crawl").respond(
        json=_one({"success": True, "html": "<p>x</p>", "markdown": "x"})
    )
    await fetcher.fetch("https://user:pw@Bücher.example/a", timeout_s=30)
    sent = json.loads(route.calls.last.request.content)
    assert sent["urls"] == ["https://xn--bcher-kva.example/a"]


@respx.mock
async def test_guard_checked_before_request(fetcher: Crawl4AIFetcher) -> None:
    route = respx.post(f"{BASE}/crawl").respond(json=_one({"success": True}))
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("http://internal.example/", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED
    assert not route.called


@respx.mock
async def test_unsuccessful_crawl(fetcher: Crawl4AIFetcher) -> None:
    route = respx.post(f"{BASE}/crawl").respond(
        json=_one(
            {
                "url": "https://example.com",
                "success": False,
                "error_message": "net::ERR_NAME_NOT_RESOLVED",
            }
        )
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and "ERR_NAME" in ei.value.detail.message
    assert ei.value.detail.retryable and route.call_count == 2  # retried once


@respx.mock
async def test_redirect_to_internal_blocked(fetcher: Crawl4AIFetcher) -> None:
    respx.post(f"{BASE}/crawl").respond(
        json=_one(
            {
                "url": "https://example.com",
                "success": True,
                "html": "<p>x</p>",
                "markdown": "x",
                "redirected_url": "http://internal.example/",
            }
        )
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_auth_failure(fetcher: Crawl4AIFetcher, status: int) -> None:
    route = respx.post(f"{BASE}/crawl").respond(status)
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_ERROR and not ei.value.detail.retryable
    assert "CRAWL4AI_API_TOKEN" in ei.value.detail.message
    assert "tok" not in ei.value.detail.message.replace("TOKEN", "")
    assert route.call_count == 1


@respx.mock
async def test_timeout_not_retried(fetcher: Crawl4AIFetcher) -> None:
    route = respx.post(f"{BASE}/crawl").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_TIMEOUT and ei.value.http_status == 504
    assert route.call_count == 1


@respx.mock
async def test_error_message_sanitised_and_source_is_checked_target(
    fetcher: Crawl4AIFetcher,
) -> None:
    respx.post(f"{BASE}/crawl").respond(
        json=_one({"success": False, "error_message": "bad\x1b[31m\nthing " + "x" * 1000})
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://user:pw@example.com/a", timeout_s=30)
    msg = ei.value.detail.message
    assert "\x1b" not in msg and "\n" not in msg
    assert len(msg) <= len("browser fetch failed: ") + 201
    assert ei.value.detail.source == "https://example.com/a"


@respx.mock
async def test_invalid_json_is_upstream_error(fetcher: Crawl4AIFetcher) -> None:
    respx.post(f"{BASE}/crawl").respond(200, text="<html>not json</html>")
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_ERROR


@respx.mock
async def test_health(fetcher: Crawl4AIFetcher) -> None:
    route = respx.get(f"{BASE}/health")
    route.respond(200, json={"status": "ok"})
    assert await fetcher.health() is True
    route.respond(503)
    assert await fetcher.health() is False
    route.mock(side_effect=httpx.ConnectError("down"))
    assert await fetcher.health() is False
