import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter
from research_engine.safety.http import make_fetch_client
from research_engine.safety.limiter import DomainLimiter
from research_engine.safety.robots import MAX_ROBOTS_BYTES, RobotsPolicy
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, Event, EventKind

UA = "ResearchEngine/1.0 (+https://github.com/pentonvillefandango/research-engine)"
ROBOTS = (
    "User-agent: *\nDisallow: /private\n\n"
    "User-agent: ResearchEngine\nDisallow: /nobots\nCrawl-delay: 5\n"
)


async def _pub(host: str) -> list[str]:
    return ["93.184.216.34"]


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with make_fetch_client(UA) as c:
        yield c


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def policy(client: httpx.AsyncClient, clock: Clock) -> tuple[RobotsPolicy, DomainLimiter]:
    lim = DomainLimiter(concurrency=1, delay_s=1)
    guard = SsrfGuard(frozenset(), resolver=_pub)
    return RobotsPolicy(client, UA, lim, guard, clock=clock), lim


@respx.mock
async def test_allow_disallow_and_crawl_delay(policy: Any) -> None:
    pol, lim = policy
    route = respx.get("https://a.example/robots.txt").respond(200, text=ROBOTS)
    await pol.check("https://a.example/public")
    with pytest.raises(ServiceError) as ei:
        await pol.check("https://a.example/nobots/page")
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED and ei.value.http_status == 403
    assert ei.value.detail.retryable is False
    assert route.call_count == 1  # cached per origin
    assert lim.delay_for("a.example") == 5


@respx.mock
async def test_4xx_allows_all(policy: Any) -> None:
    pol, _ = policy
    respx.get("https://b.example/robots.txt").respond(404)
    await pol.check("https://b.example/anything")


@respx.mock
async def test_5xx_disallows_all(policy: Any) -> None:
    pol, _ = policy
    respx.get("https://c.example/robots.txt").respond(503)
    with pytest.raises(ServiceError):
        await pol.check("https://c.example/x")


@respx.mock
async def test_5xx_cached_only_600s_then_recovers(policy: Any, clock: Clock) -> None:
    pol, _ = policy
    route = respx.get("https://c.example/robots.txt")
    route.side_effect = [httpx.Response(503), httpx.Response(200, text="")]
    with pytest.raises(ServiceError):
        await pol.check("https://c.example/x")
    clock.now += 599
    with pytest.raises(ServiceError):
        await pol.check("https://c.example/x")
    assert route.call_count == 1
    clock.now += 2
    await pol.check("https://c.example/x")
    assert route.call_count == 2


@respx.mock
async def test_success_cached_for_ttl(policy: Any, clock: Clock) -> None:
    pol, _ = policy
    route = respx.get("https://a.example/robots.txt").respond(200, text="")
    await pol.check("https://a.example/")
    clock.now += 3599
    await pol.check("https://a.example/")
    assert route.call_count == 1
    clock.now += 2
    await pol.check("https://a.example/")
    assert route.call_count == 2


@respx.mock
async def test_network_error_disallows_all(policy: Any) -> None:
    pol, _ = policy
    respx.get("https://d.example/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ServiceError) as ei:
        await pol.check("https://d.example/x")
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED


@respx.mock
async def test_timeout_disallows_all(client: httpx.AsyncClient, clock: Clock) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="")

    respx.get("https://slow.example/robots.txt").mock(side_effect=slow)
    lim = DomainLimiter(concurrency=1, delay_s=0)
    pol = RobotsPolicy(
        client, UA, lim, SsrfGuard(frozenset(), resolver=_pub), clock=clock, timeout_s=0.05
    )
    with pytest.raises(ServiceError) as ei:
        await pol.check("https://slow.example/x")
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED


@respx.mock
async def test_redirect_treated_as_allow_and_not_followed(policy: Any) -> None:
    pol, _ = policy
    respx.get("https://r.example/robots.txt").respond(
        301, headers={"location": "http://169.254.169.254/robots.txt"}
    )
    await pol.check("https://r.example/anything")


@respx.mock
async def test_unparseable_redirect_location_treated_as_allow(policy: Any) -> None:
    pol, _ = policy
    respx.get("https://r.example/robots.txt").respond(302, headers={"location": "javascript:x(1)"})
    await pol.check("https://r.example/anything")


@respx.mock
async def test_concurrent_checks_fetch_once(policy: Any) -> None:
    pol, _ = policy

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, text=ROBOTS)

    route = respx.get("https://a.example/robots.txt").mock(side_effect=slow)
    results = await asyncio.gather(
        *(pol.check("https://a.example/public") for _ in range(10)), return_exceptions=True
    )
    assert all(r is None for r in results)
    assert route.call_count == 1


@respx.mock
async def test_size_cap_truncates_oversized_robots(policy: Any) -> None:
    pol, _ = policy
    head = "User-agent: *\nDisallow: /early\n"
    tail = "Disallow: /late\n"
    body = head + "# pad\n" * (MAX_ROBOTS_BYTES // 6 + 10) + tail
    assert len(body) > MAX_ROBOTS_BYTES
    respx.get("https://big.example/robots.txt").respond(200, text=body)
    with pytest.raises(ServiceError):
        await pol.check("https://big.example/early")
    await pol.check("https://big.example/late")  # beyond the cap: never parsed


@respx.mock
async def test_declared_oversize_is_not_downloaded_whole(policy: Any) -> None:
    pol, _ = policy
    respx.get("https://big.example/robots.txt").respond(
        200,
        content=b"User-agent: *\nDisallow: /x\n" + b"#" * (MAX_ROBOTS_BYTES * 4),
    )
    with pytest.raises(ServiceError):
        await pol.check("https://big.example/x")


@respx.mock
async def test_robots_request_has_no_cookies_or_auth(client: httpx.AsyncClient) -> None:
    client.cookies.set("sid", "secret", domain="a.example")
    client.headers["Authorization"] = "Bearer x"
    pol = RobotsPolicy(client, UA, DomainLimiter(1, 0), SsrfGuard(frozenset(), resolver=_pub))
    route = respx.get("https://a.example/robots.txt").respond(200, text="")
    await pol.check("https://a.example/")
    req = route.calls.last.request
    assert "cookie" not in req.headers and "authorization" not in req.headers
    assert req.headers["user-agent"] == UA


async def test_ssrf_guard_applies_to_robots_fetch(client: httpx.AsyncClient) -> None:
    pol = RobotsPolicy(client, UA, DomainLimiter(1, 0), SsrfGuard(frozenset(), resolver=_pub))
    with pytest.raises(ServiceError) as ei:
        await pol.check("http://127.0.0.1/x")
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@respx.mock
async def test_events_emitted(policy: Any) -> None:
    pol, _ = policy
    sink = ListSink()
    em = Emitter(sink)
    respx.get("https://a.example/robots.txt").respond(200, text=ROBOTS)
    await pol.check("https://a.example/public", em)
    with pytest.raises(ServiceError):
        await pol.check("https://a.example/nobots", em)
    assert [e.kind for e in sink.events] == [EventKind.ROBOTS_FETCHED, EventKind.ROBOTS_DISALLOWED]


@respx.mock
async def test_origin_case_insensitive_and_userinfo_ignored(policy: Any) -> None:
    pol, _ = policy
    route = respx.get("https://a.example/robots.txt").respond(200, text="")
    await pol.check("https://A.Example/x")
    await pol.check("https://user:pw@a.example/y")
    assert route.call_count == 1


@respx.mock
async def test_cache_is_bounded(client: httpx.AsyncClient) -> None:
    pol = RobotsPolicy(
        client,
        UA,
        DomainLimiter(1, 0),
        SsrfGuard(frozenset(), resolver=_pub),
        max_origins=3,
    )
    respx.get(host__regex=r".*").respond(404)
    for i in range(10):
        await pol.check(f"https://h{i}.example/")
    assert pol.cached_origins <= 3


async def test_locks_do_not_leak_on_failed_fetches(client: httpx.AsyncClient) -> None:
    async def nx(host: str) -> list[str]:
        raise OSError("NXDOMAIN")

    pol = RobotsPolicy(client, UA, DomainLimiter(1, 0), SsrfGuard(frozenset(), resolver=nx))
    for i in range(50):
        with pytest.raises(ServiceError):
            await pol.check(f"https://h{i}.example/")
    assert pol.tracked_locks == 0
    # concurrent failures too
    await asyncio.gather(
        *(pol.check(f"https://c{i % 5}.example/") for i in range(20)), return_exceptions=True
    )
    assert pol.tracked_locks == 0


@respx.mock
async def test_idna_origin_uses_checked_punycode_host(client: httpx.AsyncClient) -> None:
    seen: list[str] = []

    async def resolve(h: str) -> list[str]:
        seen.append(h)
        return ["10.0.0.9"] if h == "xn--strae-oqa.example" else ["93.184.216.34"]

    pol = RobotsPolicy(client, UA, DomainLimiter(1, 0), SsrfGuard(frozenset(), resolver=resolve))
    with pytest.raises(ServiceError) as ei:
        await pol.check("https://straße.example/x")
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED and seen == ["xn--strae-oqa.example"]


@respx.mock
async def test_robots_set_cookie_not_retained(policy: Any, client: httpx.AsyncClient) -> None:
    pol, _ = policy
    respx.get("https://a.example/robots.txt").respond(200, text="", headers={"set-cookie": "a=b"})
    await pol.check("https://a.example/")
    assert len(client.cookies) == 0


@respx.mock
async def test_robots_userinfo_not_sent_as_auth(policy: Any) -> None:
    pol, _ = policy
    route = respx.get("https://a.example/robots.txt").respond(200, text="")
    await pol.check("https://u:p@a.example/")
    assert "authorization" not in route.calls.last.request.headers
