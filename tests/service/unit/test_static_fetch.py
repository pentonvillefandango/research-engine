import asyncio
import gzip
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from research_engine.adapters.static_fetch import StaticFetcher
from research_engine.errors import ServiceError
from research_engine.safety.http import make_fetch_client
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, FetchMethod

UA = "UA/1"


async def _resolve(host: str) -> list[str]:
    return {"evil.example": ["10.0.0.1"]}.get(host, ["93.184.216.34"])


async def _nosleep(_: float) -> None:
    return None


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with make_fetch_client(UA) as c:
        yield c


@pytest.fixture
def fetcher(client: httpx.AsyncClient) -> StaticFetcher:
    return StaticFetcher(
        client,
        SsrfGuard(frozenset(), resolver=_resolve),
        max_bytes=1000,
        allowed_types=frozenset({"text/html", "application/pdf"}),
        user_agent=UA,
        sleep=_nosleep,
    )


async def _must_not_be_read() -> AsyncIterator[bytes]:
    raise AssertionError("body was read")
    yield b""  # pragma: no cover


@respx.mock
async def test_html_ok(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/").respond(
        200,
        html="<html><body>hé</body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    )
    page = await fetcher.fetch("https://a.example/", timeout_s=5)
    assert page.method is FetchMethod.STATIC and page.status == 200 and "hé" in (page.html or "")
    assert page.final_url == "https://a.example/" and page.redirects == []


@respx.mock
async def test_redirect_to_private_blocked(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/r").respond(302, headers={"location": "http://evil.example/admin"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/r", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "//evil.example/x",  # scheme-relative
        "http://169.254.169.254/latest/meta-data",
        "ftp://a.example/file",
        "file:///etc/passwd",
    ],
)
@respx.mock
async def test_redirect_targets_checked(fetcher: StaticFetcher, location: str) -> None:
    respx.get("https://a.example/r").respond(302, headers={"location": location})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/r", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED and ei.value.detail.retryable is False


@respx.mock
async def test_unparseable_redirect_location_is_fetch_failed(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/r").respond(302, headers={"location": "javascript:alert(1)"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/r", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and ei.value.detail.retryable is False


@respx.mock
async def test_ssrf_check_on_initial_url(fetcher: StaticFetcher) -> None:
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("http://evil.example/", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@respx.mock
async def test_second_hop_is_checked_too(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/1").respond(301, headers={"location": "/2"})
    respx.get("https://a.example/2").respond(301, headers={"location": "http://evil.example/"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/1", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@respx.mock
async def test_redirect_chain_recorded(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/1").respond(301, headers={"location": "/2"})
    respx.get("https://a.example/2").respond(
        200, html="<html></html>", headers={"content-type": "text/html"}
    )
    page = await fetcher.fetch("https://a.example/1", timeout_s=5)
    assert page.final_url == "https://a.example/2" and page.redirects == ["https://a.example/1"]


@respx.mock
async def test_absolute_redirect_followed(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/1").respond(302, headers={"location": "https://b.example/x"})
    respx.get("https://b.example/x").respond(200, html="<html>b</html>")
    page = await fetcher.fetch("https://a.example/1", timeout_s=5)
    assert page.final_url == "https://b.example/x"


@respx.mock
async def test_five_redirects_ok_six_fail(fetcher: StaticFetcher) -> None:
    for i in range(6):
        respx.get(f"https://a.example/{i}").respond(302, headers={"location": f"/{i + 1}"})
    respx.get("https://a.example/6").respond(200, html="<html>end</html>")
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/0", timeout_s=5)  # 6 redirects
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and ei.value.detail.retryable is False
    page = await fetcher.fetch("https://a.example/1", timeout_s=5)  # 5 redirects
    assert page.final_url == "https://a.example/6" and len(page.redirects) == 5


@respx.mock
async def test_too_many_redirects_loop(fetcher: StaticFetcher) -> None:
    route = respx.get("https://a.example/loop").respond(302, headers={"location": "/loop"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/loop", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and ei.value.detail.retryable is False
    assert route.call_count == 1  # loop detected on the first repeat, no retry


@respx.mock
async def test_two_step_loop(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/a").respond(302, headers={"location": "/b"})
    respx.get("https://a.example/b").respond(302, headers={"location": "/a"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/a", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED


@respx.mock
async def test_too_large(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/big").respond(
        200, content=b"x" * 5000, headers={"content-type": "text/html"}
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/big", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.RESPONSE_TOO_LARGE and ei.value.http_status == 413
    assert ei.value.detail.retryable is False


@respx.mock
async def test_too_large_while_streaming_without_content_length(fetcher: StaticFetcher) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b"x" * 100

    respx.get("https://a.example/s").respond(
        200, content=chunks(), headers={"content-type": "text/html"}
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/s", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.RESPONSE_TOO_LARGE


@respx.mock
async def test_declared_content_length_rejected_without_reading(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/big").respond(
        200,
        content=_must_not_be_read(),
        headers={"content-type": "text/html", "content-length": "999999"},
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/big", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.RESPONSE_TOO_LARGE


@respx.mock
async def test_gzip_bomb_capped_on_decoded_stream(fetcher: StaticFetcher) -> None:
    payload = gzip.compress(b"A" * 200_000)
    assert len(payload) < 1000  # small on the wire, huge decoded
    respx.get("https://a.example/bomb").respond(
        200, content=payload, headers={"content-type": "text/html", "content-encoding": "gzip"}
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/bomb", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.RESPONSE_TOO_LARGE


@respx.mock
async def test_small_gzip_decoded(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/gz").respond(
        200,
        content=gzip.compress(b"<html>zipped</html>"),
        headers={"content-type": "text/html", "content-encoding": "gzip"},
    )
    page = await fetcher.fetch("https://a.example/gz", timeout_s=5)
    assert page.html == "<html>zipped</html>"


@respx.mock
async def test_content_type_rejected(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/z").respond(
        200, content=b"PK..", headers={"content-type": "application/zip"}
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/z", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.CONTENT_TYPE_NOT_ALLOWED
    assert ei.value.http_status == 415 and ei.value.detail.retryable is False


@respx.mock
async def test_content_type_rejected_before_body_is_read(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/z").respond(
        200, content=_must_not_be_read(), headers={"content-type": "application/zip"}
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/z", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.CONTENT_TYPE_NOT_ALLOWED


@respx.mock
async def test_pdf_sniffed_without_header(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/f").respond(200, content=b"%PDF-1.7\n...")
    page = await fetcher.fetch("https://a.example/f", timeout_s=5)
    assert page.content_type == "application/pdf" and page.html is None
    assert page.body.startswith(b"%PDF-")


@respx.mock
async def test_html_sniffed_and_unknown_rejected(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/h").respond(200, content=b"  <!DOCTYPE html><p>x")
    assert (await fetcher.fetch("https://a.example/h", timeout_s=5)).content_type == "text/html"
    respx.get("https://a.example/u").respond(200, content=b"\x00\x01binary")
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/u", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.CONTENT_TYPE_NOT_ALLOWED


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (404, False)])
async def test_status_errors(fetcher: StaticFetcher, status: int, retryable: bool) -> None:
    route = respx.get("https://a.example/s").respond(status)
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/s", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED
    assert ei.value.detail.retryable is retryable
    assert route.call_count == (3 if status >= 500 else 1)  # 429 is never retried in-call


@respx.mock
async def test_timeout_maps_to_upstream_timeout(fetcher: StaticFetcher) -> None:
    route = respx.get("https://a.example/t").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/t", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_TIMEOUT and ei.value.detail.retryable
    assert route.call_count == 3


@respx.mock
async def test_retry_recovers_from_transient_503(fetcher: StaticFetcher) -> None:
    route = respx.get("https://a.example/f")
    route.side_effect = [httpx.Response(503), httpx.Response(200, html="<html>ok</html>")]
    page = await fetcher.fetch("https://a.example/f", timeout_s=5)
    assert page.status == 200 and route.call_count == 2


@respx.mock
async def test_charset_from_header_then_meta_then_utf8(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/l1").respond(
        200,
        content="<html>caf\xe9</html>".encode("latin-1"),
        headers={"content-type": "text/html; charset=iso-8859-1"},
    )
    assert "café" in (await fetcher.fetch("https://a.example/l1", timeout_s=5)).html  # type: ignore[operator]
    respx.get("https://a.example/l2").respond(
        200,
        content=b'<html><meta charset="windows-1252">caf\xe9</html>',
        headers={"content-type": "text/html"},
    )
    assert "café" in (await fetcher.fetch("https://a.example/l2", timeout_s=5)).html  # type: ignore[operator]
    respx.get("https://a.example/l3").respond(
        200, content=b"<html>caf\xe9</html>", headers={"content-type": "text/html"}
    )
    assert "�" in (await fetcher.fetch("https://a.example/l3", timeout_s=5)).html  # type: ignore[operator]
    respx.get("https://a.example/l4").respond(
        200, content=b"<html>x</html>", headers={"content-type": "text/html; charset=bogus-xyz"}
    )
    assert (await fetcher.fetch("https://a.example/l4", timeout_s=5)).html == "<html>x</html>"


@respx.mock
async def test_no_cookies_or_auth_sent_and_fresh_headers(client: httpx.AsyncClient) -> None:
    client.cookies.set("sid", "secret", domain="a.example")
    client.headers["Authorization"] = "Bearer secret"
    client.headers["X-Api-Key"] = "k"
    f = StaticFetcher(
        client,
        SsrfGuard(frozenset(), resolver=_resolve),
        max_bytes=1000,
        allowed_types=frozenset({"text/html"}),
        user_agent=UA,
        sleep=_nosleep,
    )
    first = respx.get("https://a.example/1").respond(
        302, headers={"location": "/2", "set-cookie": "tracker=1; Path=/"}
    )
    second = respx.get("https://a.example/2").respond(200, html="<html></html>")
    await f.fetch("https://a.example/1", timeout_s=5)
    for call in (first.calls.last, second.calls.last):
        h = call.request.headers
        assert "cookie" not in h and "authorization" not in h and "x-api-key" not in h
        assert h["user-agent"] == UA
        assert call.request.method == "GET"
    await f.fetch("https://a.example/2", timeout_s=5)  # later fetch: still no cookie
    assert "cookie" not in second.calls.last.request.headers


@respx.mock
async def test_429_attempted_once_but_flagged_retryable(fetcher: StaticFetcher) -> None:
    route = respx.get("https://a.example/s").respond(429)
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/s", timeout_s=5)
    assert route.call_count == 1
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and ei.value.detail.retryable is True


@respx.mock
async def test_total_budget_bounded_across_retries(client: httpx.AsyncClient) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, html="<html></html>")

    f = StaticFetcher(
        client,
        SsrfGuard(frozenset(), resolver=_resolve),
        max_bytes=1000,
        allowed_types=frozenset({"text/html"}),
        user_agent=UA,
    )  # real backoff sleeps
    route = respx.get("https://a.example/t").mock(side_effect=slow)
    t0 = time.monotonic()
    with pytest.raises(ServiceError) as ei:
        await f.fetch("https://a.example/t", timeout_s=0.2)
    assert time.monotonic() - t0 < 0.35  # one budget, not 3x plus backoff
    assert ei.value.detail.code is ErrorCode.UPSTREAM_TIMEOUT
    assert route.call_count <= 2


@respx.mock
async def test_userinfo_stripped_no_basic_auth(fetcher: StaticFetcher) -> None:
    route = respx.get("https://a.example/p").respond(200, html="<html></html>")
    page = await fetcher.fetch("https://user:pw@a.example/p", timeout_s=5)
    req = route.calls.last.request
    assert "authorization" not in req.headers and b"pw" not in bytes(req.url.raw_path)
    assert "user" not in page.final_url and "pw" not in page.final_url


@respx.mock
@pytest.mark.parametrize("host", ["straße.example", "ς.example", "faß.de"])
async def test_idna_hosts_checked_in_2008_form_initial_and_redirect(
    client: httpx.AsyncClient, host: str
) -> None:
    puny = httpx.URL(f"http://{host}/").raw_host.decode()
    seen: list[str] = []

    async def resolve(h: str) -> list[str]:
        seen.append(h)
        return ["10.0.0.9"] if h == puny else ["93.184.216.34"]

    f = StaticFetcher(
        client,
        SsrfGuard(frozenset(), resolver=resolve),
        max_bytes=1000,
        allowed_types=frozenset({"text/html"}),
        user_agent=UA,
        sleep=_nosleep,
    )
    with pytest.raises(ServiceError) as ei:  # as the original URL
        await f.fetch(f"https://{host}/", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED and puny in seen
    respx.get("https://ok.example/r").respond(
        302, headers=[(b"location", f"https://{host}/x".encode())]
    )
    with pytest.raises(ServiceError) as ei:  # as a redirect Location
        await f.fetch("https://ok.example/r", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@respx.mock
async def test_zwj_host_blocked(fetcher: StaticFetcher) -> None:
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a\u200db.example/", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED
    respx.get("https://ok.example/r").respond(
        302, headers=[(b"location", "https://a\u200db.example/".encode())]
    )
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://ok.example/r", timeout_s=5)
    assert ei.value.detail.code in (ErrorCode.SSRF_BLOCKED, ErrorCode.FETCH_FAILED)


@respx.mock
async def test_checked_host_is_connected_host(client: httpx.AsyncClient) -> None:
    f = StaticFetcher(
        client,
        SsrfGuard(frozenset(), resolver=_resolve),
        max_bytes=1000,
        allowed_types=frozenset({"text/html"}),
        user_agent=UA,
        sleep=_nosleep,
    )
    route = respx.get("https://xn--strae-oqa.example/").respond(200, html="<html></html>")
    page = await f.fetch("https://straße.example/", timeout_s=5)
    assert route.call_count == 1 and page.status == 200


@respx.mock
async def test_allowed_types_case_insensitive(client: httpx.AsyncClient) -> None:
    f = StaticFetcher(
        client,
        SsrfGuard(frozenset(), resolver=_resolve),
        max_bytes=1000,
        allowed_types=frozenset({"Text/HTML"}),
        user_agent=UA,
        sleep=_nosleep,
    )
    respx.get("https://a.example/").respond(200, html="<html></html>")
    assert (await f.fetch("https://a.example/", timeout_s=5)).content_type == "text/html"


@respx.mock
async def test_set_cookie_not_retained(fetcher: StaticFetcher, client: httpx.AsyncClient) -> None:
    respx.get("https://a.example/c").respond(
        200, html="<html></html>", headers={"set-cookie": "sid=1; Path=/"}
    )
    await fetcher.fetch("https://a.example/c", timeout_s=5)
    assert len(client.cookies) == 0


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 503])
async def test_status_error_carries_upstream_status(fetcher: StaticFetcher, status: int) -> None:
    respx.get("https://a.example/s").respond(status)
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/s", timeout_s=5)
    assert ei.value.upstream_status == status


@respx.mock
async def test_on_hop_wraps_every_request(fetcher: StaticFetcher) -> None:
    log: list[str] = []

    @asynccontextmanager
    async def hook(url: str) -> AsyncIterator[None]:
        log.append(f"enter {url}")
        yield
        log.append(f"exit {url}")

    respx.get("https://a.example/1").respond(302, headers={"Location": "https://b.example/2"})
    respx.get("https://b.example/2").respond(302, headers={"Location": "/3"})
    respx.get("https://b.example/3").respond(200, html="<p>ok</p>")
    page = await fetcher.fetch("https://a.example/1", timeout_s=5, on_hop=hook)
    assert page.final_url == "https://b.example/3"
    assert log == [
        "enter https://a.example/1",
        "exit https://a.example/1",
        "enter https://b.example/2",
        "exit https://b.example/2",
        "enter https://b.example/3",
        "exit https://b.example/3",
    ]


@respx.mock
async def test_on_hop_wraps_first_request_of_each_attempt(fetcher: StaticFetcher) -> None:
    entered: list[str] = []

    @asynccontextmanager
    async def hook(url: str) -> AsyncIterator[None]:
        entered.append(url)
        yield

    respx.get("https://a.example/1").respond(302, headers={"Location": "https://b.example/2"})
    respx.get("https://b.example/2").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, html="<p>ok</p>")]
    )
    await fetcher.fetch("https://a.example/1", timeout_s=5, on_hop=hook)
    assert entered == [
        "https://a.example/1",
        "https://b.example/2",
        "https://a.example/1",  # the retry starts at the original URL again
        "https://b.example/2",
    ]


@respx.mock
async def test_on_hop_refusal_stops_before_request(fetcher: StaticFetcher) -> None:
    @asynccontextmanager
    async def deny(url: str) -> AsyncIterator[None]:
        raise ServiceError.of(ErrorCode.ROBOTS_DISALLOWED, "no", retryable=False)
        yield  # pragma: no cover

    respx.get("https://a.example/1").respond(302, headers={"Location": "https://b.example/2"})
    hop = respx.get("https://b.example/2").respond(200, html="<p>x</p>")
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/1", timeout_s=5, on_hop=deny)
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED and not hop.called
