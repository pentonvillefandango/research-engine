import gzip
from collections.abc import AsyncIterator

import httpx
import pytest
import respx
from research_engine.adapters.static_fetch import StaticFetcher
from research_engine.errors import ServiceError
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, FetchMethod

UA = "UA/1"


async def _resolve(host: str) -> list[str]:
    return {"evil.example": ["10.0.0.1"]}.get(host, ["93.184.216.34"])


async def _nosleep(_: float) -> None:
    return None


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
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
    assert route.call_count == (3 if retryable else 1)


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
