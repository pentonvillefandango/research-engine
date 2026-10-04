import asyncio
import hashlib
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx
from research_engine.adapters.extract import Extracted
from research_engine.adapters.fetch import RawPage, RedirectHook
from research_engine.adapters.html_extract import DefaultHtmlExtractor
from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.adapters.static_fetch import StaticFetcher
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter
from research_engine.pipeline.fetch import FetchService
from research_engine.safety.http import make_fetch_client
from research_engine.safety.limiter import DomainLimiter
from research_engine.safety.robots import RobotsPolicy
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import (
    DocumentFormat,
    ErrorCode,
    EscalationReason,
    Event,
    EventKind,
    FetchMethod,
    FetchMode,
    FetchRequest,
)

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"
UA = "ResearchEngine/0.1 (+https://github.com/pentonvillefandango/research-engine)"


class FakeFetcher:
    def __init__(self, method: FetchMethod, pages: dict[str, RawPage | ServiceError]) -> None:
        self.method, self.pages = method, pages
        self.calls: list[str] = []
        self.timeouts: list[float] = []

    async def fetch(
        self, url: str, *, timeout_s: float, on_redirect: RedirectHook | None = None
    ) -> RawPage:
        self.calls.append(url)
        self.timeouts.append(timeout_s)
        out = self.pages[url]
        if isinstance(out, ServiceError):
            raise out
        return out

    async def health(self) -> bool:
        return True


def html_page(
    url: str, file: str, method: FetchMethod = FetchMethod.STATIC, markdown: str | None = None
) -> RawPage:
    html = (PAGES / file).read_text()
    return RawPage(
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        body=html.encode(),
        html=html,
        markdown=markdown,
        method=method,
    )


class AllowRobots:
    def __init__(self, log: list[str] | None = None) -> None:
        self.checked: list[str] = []
        self.log = log

    async def check(self, url: str, em: Emitter | None = None) -> None:
        self.checked.append(url)
        if self.log is not None:
            self.log.append(f"robots:{url}")


class DenyRobots:
    async def check(self, url: str, em: Emitter | None = None) -> None:
        raise ServiceError.of(
            ErrorCode.ROBOTS_DISALLOWED, "no", retryable=False, source=url, http_status=403
        )


class Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class SpyCache(InMemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.ttls: list[int] = []
        self.gets = 0

    async def get(self, key: str) -> bytes | None:
        self.gets += 1
        return await super().get(key)

    async def set(self, key: str, value: bytes, ttl_s: int) -> None:
        self.ttls.append(ttl_s)
        await super().set(key, value, ttl_s)


def make(
    static: dict[str, RawPage | ServiceError],
    browser: dict[str, RawPage | ServiceError],
    settings: Settings,
    *,
    robots: object | None = None,
    limiter: DomainLimiter | None = None,
    cache: InMemoryCache | None = None,
) -> tuple[FetchService, FakeFetcher, FakeFetcher, Sink]:
    s, b, sink = (
        FakeFetcher(FetchMethod.STATIC, static),
        FakeFetcher(FetchMethod.BROWSER, browser),
        Sink(),
    )
    svc = FetchService(
        s,
        b,
        DefaultHtmlExtractor(),
        PypdfExtractor(),
        robots or AllowRobots(),  # type: ignore[arg-type]
        limiter or DomainLimiter(2, 0),
        cache or InMemoryCache(),
        sink,
        settings,
    )
    return svc, s, b, sink


@pytest.fixture
def settings(settings_env: None) -> Settings:
    return Settings()  # type: ignore[call-arg]


async def test_static_article(settings: Settings) -> None:
    url = "https://blog.example/post"
    svc, _, b, _ = make({url: html_page(url, "article.html")}, {}, settings)
    doc, hit = await svc.fetch(FetchRequest(url=url), job_id="j9")
    assert not hit and doc.provenance.method is FetchMethod.STATIC and b.calls == []
    assert (
        doc.provenance.content_hash == "sha256:" + hashlib.sha256(doc.markdown.encode()).hexdigest()
    )
    assert doc.provenance.job_id == "j9" and doc.quality.escalation_reason is None
    assert doc.html is None and doc.word_count >= settings.thin_word_threshold


async def test_spa_escalates(settings: Settings) -> None:
    url = "https://app.example/"
    rendered = html_page(
        url, "article.html", FetchMethod.BROWSER, markdown="# Rendered\n\n" + "word " * 200
    )
    svc, _, b, sink = make({url: html_page(url, "spa.html")}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.provenance.method is FetchMethod.BROWSER and doc.markdown.startswith("# Rendered")
    assert doc.quality.escalation_reason is EscalationReason.JS_RENDERED
    assert "escalated to browser: js_rendered" in doc.warnings
    assert EventKind.FETCH_ESCALATED in [e.kind for e in sink.events]
    assert b.calls == [url]
    # tables/links/structured data come from extracting the rendered HTML
    assert doc.links and doc.word_count == len(doc.markdown.split())


async def test_thin_escalates(settings: Settings) -> None:
    url = "https://thin.example/"
    thin = RawPage(
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        body=b"",
        html="<html><head><title>T</title></head><body><p>short page</p></body></html>",
        markdown=None,
        method=FetchMethod.STATIC,
    )
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, _, _, _ = make({url: thin}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.quality.escalation_reason is EscalationReason.THIN_CONTENT
    assert doc.provenance.method is FetchMethod.BROWSER


async def test_thin_then_browser_fails_keeps_static(settings: Settings) -> None:
    url = "https://thin.example/"
    thin = RawPage(
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        body=b"",
        html="<html><head><title>T</title></head><body><p>short page</p></body></html>",
        markdown=None,
        method=FetchMethod.STATIC,
    )
    err = ServiceError.of(ErrorCode.FETCH_FAILED, "boom", retryable=True)
    cache = SpyCache()
    svc, _, _, _ = make({url: thin}, {url: err}, settings, cache=cache)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.provenance.method is FetchMethod.STATIC
    assert "browser escalation failed: boom" in doc.warnings
    assert cache.ttls == []  # a degraded result is not pinned in the cache


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.SSRF_BLOCKED,
        ErrorCode.ROBOTS_DISALLOWED,
        ErrorCode.CONTENT_TYPE_NOT_ALLOWED,
        ErrorCode.RESPONSE_TOO_LARGE,
    ],
)
async def test_non_escalating_errors(settings: Settings, code: ErrorCode) -> None:
    url = "https://x.example/"
    svc, _, b, sink = make({url: ServiceError.of(code, "no", retryable=False)}, {}, settings)
    with pytest.raises(ServiceError) as ei:
        await svc.fetch(FetchRequest(url=url))
    assert ei.value.detail.code is code
    assert b.calls == []
    assert EventKind.FETCH_FAILED in [e.kind for e in sink.events]


@pytest.mark.parametrize(
    "err",
    [
        ServiceError.of(ErrorCode.FETCH_FAILED, "HTTP 403", retryable=False, upstream_status=403),
        ServiceError.of(ErrorCode.FETCH_FAILED, "HTTP 401", retryable=False, upstream_status=401),
        ServiceError.of(ErrorCode.FETCH_FAILED, "HTTP 429", retryable=True, upstream_status=429),
        ServiceError.of(ErrorCode.FETCH_FAILED, "HTTP 503", retryable=True, upstream_status=503),
        ServiceError.of(ErrorCode.FETCH_FAILED, "network", retryable=True),
        ServiceError.of(ErrorCode.UPSTREAM_TIMEOUT, "slow", retryable=True, http_status=504),
    ],
)
async def test_static_failure_escalates(settings: Settings, err: ServiceError) -> None:
    url = "https://f.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, _, _, _ = make({url: err}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.quality.escalation_reason is EscalationReason.STATIC_FAILED
    assert "escalated to browser: static_failed" in doc.warnings


async def test_modes(settings: Settings) -> None:
    url = "https://m.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, s, b, _ = make({url: html_page(url, "spa.html")}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url, mode=FetchMode.STATIC, use_cache=False))
    assert doc.provenance.method is FetchMethod.STATIC and b.calls == []
    doc, _ = await svc.fetch(FetchRequest(url=url, mode=FetchMode.BROWSER, use_cache=False))
    assert doc.quality.escalation_reason is EscalationReason.FORCED and s.calls == [url]
    assert doc.provenance.method is FetchMethod.BROWSER


async def test_static_mode_failure_not_escalated(settings: Settings) -> None:
    url = "https://f.example/"
    err = ServiceError.of(ErrorCode.FETCH_FAILED, "HTTP 403", retryable=False)
    svc, _, b, _ = make({url: err}, {}, settings)
    with pytest.raises(ServiceError):
        await svc.fetch(FetchRequest(url=url, mode=FetchMode.STATIC))
    assert b.calls == []


async def test_pdf(settings: Settings) -> None:
    url = "https://v.example/a.pdf"
    body = (PAGES / "sample.pdf").read_bytes()
    pdf = RawPage(
        url=url,
        final_url=url,
        status=200,
        content_type="application/pdf",
        body=body,
        html=None,
        markdown=None,
        method=FetchMethod.STATIC,
    )
    svc, _, b, _ = make({url: pdf}, {}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert "Widget Pro" in doc.markdown and b.calls == []
    assert doc.provenance.method is FetchMethod.STATIC and doc.quality.escalation_reason is None


async def test_html_format_and_cache(settings: Settings) -> None:
    url = "https://blog.example/post?utm_source=x"
    cache = SpyCache()
    svc, s, _, sink = make({url: html_page(url, "article.html")}, {}, settings, cache=cache)
    fmts = (DocumentFormat.MARKDOWN, DocumentFormat.HTML)
    doc, _ = await svc.fetch(FetchRequest(url=url, formats=fmts))
    assert doc.html
    _, hit = await svc.fetch(FetchRequest(url="https://blog.example/post", formats=fmts))
    assert hit and len(s.calls) == 1 and EventKind.CACHE_HIT in [e.kind for e in sink.events]
    assert cache.ttls == [settings.cache_ttl_page_s]


async def test_use_cache_false_bypasses_read(settings: Settings) -> None:
    url = "https://blog.example/post"
    cache = SpyCache()
    svc, s, _, _ = make({url: html_page(url, "article.html")}, {}, settings, cache=cache)
    await svc.fetch(FetchRequest(url=url))
    gets = cache.gets
    _, hit = await svc.fetch(FetchRequest(url=url, use_cache=False))
    assert not hit and len(s.calls) == 2 and cache.gets == gets


async def test_robots_and_slot_on_original_url_before_fetch(settings: Settings) -> None:
    url = "https://blog.example/post?utm_source=x"
    log: list[str] = []

    class LogLimiter(DomainLimiter):
        def slot(self, url: str):  # type: ignore[override]
            log.append(f"slot:{url}")
            return super().slot(url)

    class LogFetcher(FakeFetcher):
        async def fetch(
            self, url: str, *, timeout_s: float, on_redirect: RedirectHook | None = None
        ) -> RawPage:
            log.append(f"fetch:{url}")
            return await super().fetch(url, timeout_s=timeout_s)

    svc, _, _, _ = make({}, {}, settings, robots=AllowRobots(log), limiter=LogLimiter(2, 0))
    svc._static = LogFetcher(FetchMethod.STATIC, {url: html_page(url, "article.html")})
    await svc.fetch(FetchRequest(url=url))
    assert log == [f"robots:{url}", f"slot:{url}", f"fetch:{url}"]


async def test_robots_disallowed_never_fetches(settings: Settings) -> None:
    url = "https://blog.example/post"
    svc, s, b, _ = make({}, {}, settings, robots=DenyRobots())
    with pytest.raises(ServiceError) as ei:
        await svc.fetch(FetchRequest(url=url))
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED and s.calls == b.calls == []


@respx.mock
async def test_idn_crawl_delay_applies_to_limiter_slot(settings: Settings) -> None:
    async def public(_host: str) -> list[str]:
        return ["93.184.216.34"]

    respx.get("https://xn--bcher-kva.example/robots.txt").respond(
        text="User-agent: *\nCrawl-delay: 5\n"
    )
    url = "https://bücher.example/post"
    limiter = DomainLimiter(2, 0)
    guard = SsrfGuard(frozenset(), resolver=public)
    robots = RobotsPolicy(make_fetch_client(UA), UA, limiter, guard)
    svc, _, _, _ = make(
        {url: html_page(url, "article.html")}, {}, settings, robots=robots, limiter=limiter
    )
    await svc.fetch(FetchRequest(url=url))
    # one key: robots' crawl-delay and the slot used for the fetch share the punycode host
    assert limiter.tracked_domains == 1
    assert limiter.delay_for("xn--bcher-kva.example") == 5


async def test_extracted_warnings_copied(settings: Settings) -> None:
    class Warny(DefaultHtmlExtractor):
        def extract(self, html: str, base_url: str) -> Extracted:
            ex = super().extract(html, base_url)
            return replace(ex, warnings=["tables capped at 100"])

    url = "https://blog.example/post"
    svc, _, _, _ = make({url: html_page(url, "article.html")}, {}, settings)
    svc._html = Warny()
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.warnings == ["tables capped at 100"]


async def test_browser_warnings_include_extraction_and_escalation(settings: Settings) -> None:
    class Warny(DefaultHtmlExtractor):
        def extract(self, html: str, base_url: str) -> Extracted:
            ex = super().extract(html, base_url)
            return replace(ex, warnings=["html truncated to 5 chars", "trafilatura skipped"])

    url = "https://app.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, _, _, _ = make({}, {url: rendered}, settings)
    svc._html = Warny()
    doc, _ = await svc.fetch(FetchRequest(url=url, mode=FetchMode.BROWSER))
    # Crawl4AI's markdown replaces Trafilatura's, so the trafilatura note no longer applies
    assert doc.warnings == ["html truncated to 5 chars", "escalated to browser: forced"]


async def test_extraction_off_loop(settings: Settings) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []

    class Spy(DefaultHtmlExtractor):
        def extract(self, html: str, base_url: str) -> Extracted:
            seen.append(threading.get_ident())
            return super().extract(html, base_url)

    class PdfSpy(PypdfExtractor):
        def extract(self, body: bytes, url: str) -> Extracted:
            seen.append(threading.get_ident())
            return super().extract(body, url)

    url, pdf_url = "https://blog.example/post", "https://v.example/a.pdf"
    pdf = RawPage(
        url=pdf_url,
        final_url=pdf_url,
        status=200,
        content_type="application/pdf",
        body=(PAGES / "sample.pdf").read_bytes(),
        html=None,
        markdown=None,
        method=FetchMethod.STATIC,
    )
    spa = "https://app.example/"
    rendered = html_page(spa, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, _, _, _ = make(
        {url: html_page(url, "article.html"), pdf_url: pdf, spa: html_page(spa, "spa.html")},
        {spa: rendered},
        settings,
    )
    svc._html, svc._pdf = Spy(), PdfSpy()
    await svc.fetch(FetchRequest(url=url))
    await svc.fetch(FetchRequest(url=pdf_url))
    await svc.fetch(FetchRequest(url=spa))
    assert len(seen) == 4 and all(t != loop_thread for t in seen)


# --- Fix round 1 ------------------------------------------------------------------------------


class AnyPage(FakeFetcher):
    """Serves article.html for any URL."""

    async def fetch(
        self, url: str, *, timeout_s: float, on_redirect: RedirectHook | None = None
    ) -> RawPage:
        self.calls.append(url)
        return html_page(url, "article.html", self.method)


class Slow(FakeFetcher):
    async def fetch(
        self, url: str, *, timeout_s: float, on_redirect: RedirectHook | None = None
    ) -> RawPage:
        self.calls.append(url)
        self.timeouts.append(timeout_s)
        await asyncio.sleep(100)
        raise AssertionError("unreachable")


def _thin(url: str) -> RawPage:
    return RawPage(
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        body=b"",
        html="<html><head><title>T</title></head><body><p>short page</p></body></html>",
        markdown=None,
        method=FetchMethod.STATIC,
    )


# 1. definitive 4xx are raised; error pages are never cached


@pytest.mark.parametrize("status", [404, 410, 400])
async def test_definitive_4xx_not_escalated(settings: Settings, status: int) -> None:
    url = "https://gone.example/"
    err = ServiceError.of(
        ErrorCode.FETCH_FAILED, f"HTTP {status}", retryable=False, upstream_status=status
    )
    svc, _, b, _ = make({url: err}, {}, settings)
    with pytest.raises(ServiceError) as ei:
        await svc.fetch(FetchRequest(url=url))
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and b.calls == []


async def test_non_retryable_without_status_not_escalated(settings: Settings) -> None:
    url = "https://loop.example/"
    err = ServiceError.of(ErrorCode.FETCH_FAILED, "redirect loop", retryable=False)
    svc, _, b, _ = make({url: err}, {}, settings)
    with pytest.raises(ServiceError):
        await svc.fetch(FetchRequest(url=url))
    assert b.calls == []


async def test_error_status_document_not_cached(settings: Settings) -> None:
    url = "https://app.example/missing"
    page = replace(html_page(url, "article.html", FetchMethod.BROWSER, "word " * 300), status=404)
    cache = SpyCache()
    svc, _, b, _ = make({}, {url: page}, settings, cache=cache)
    doc, _ = await svc.fetch(FetchRequest(url=url, mode=FetchMode.BROWSER))
    assert doc.status == 404 and cache.ttls == []
    _, hit = await svc.fetch(FetchRequest(url=url, mode=FetchMode.BROWSER))
    assert not hit and len(b.calls) == 2


# 2. cache key


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://git.example/r?ref=main", "https://git.example/r?ref=dev"),
        ("https://spa.example/#/pricing", "https://spa.example/#/about"),
        ("https://spa.example/#!/pricing", "https://spa.example/#!/about"),
        ("https://d.example/docs", "https://d.example/docs/"),
        ("https://q.example/?a=1&b=2", "https://q.example/?b=2&a=1"),
    ],
)
async def test_cache_keys_do_not_collide(settings: Settings, a: str, b: str) -> None:
    svc, _, _, _ = make({}, {}, settings)
    svc._static = fetcher = AnyPage(FetchMethod.STATIC, {})
    _, hit_a = await svc.fetch(FetchRequest(url=a))
    _, hit_b = await svc.fetch(FetchRequest(url=b))
    assert not hit_a and not hit_b and fetcher.calls == [a, b]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://blog.example/post?utm_source=x", "https://blog.example/post"),
        ("https://blog.example/post?id=1&gclid=z&fbclid=y", "https://blog.example/post?id=1"),
        ("HTTPS://Blog.Example:443/post", "https://blog.example/post"),
        ("https://blog.example/post#section-2", "https://blog.example/post"),
    ],
)
async def test_cache_keys_shared(settings: Settings, a: str, b: str) -> None:
    svc, _, _, _ = make({}, {}, settings)
    svc._static = AnyPage(FetchMethod.STATIC, {})
    await svc.fetch(FetchRequest(url=a))
    _, hit = await svc.fetch(FetchRequest(url=b))
    assert hit


async def test_cache_key_ignores_format_order_and_timeout(settings: Settings) -> None:
    url = "https://blog.example/post"
    svc, s, _, _ = make({url: html_page(url, "article.html")}, {}, settings)
    md, html = DocumentFormat.MARKDOWN, DocumentFormat.HTML
    await svc.fetch(FetchRequest(url=url, formats=(md, html), timeout_s=60))
    _, hit = await svc.fetch(FetchRequest(url=url, formats=(html, md), timeout_s=30))
    assert hit and len(s.calls) == 1


# 7. a cache hit carries the current requester's url


async def test_cache_hit_reports_requested_url(settings: Settings) -> None:
    first, second = "https://blog.example/post?utm_source=x", "https://blog.example/post"
    svc, _, _, _ = make({first: html_page(first, "article.html")}, {}, settings)
    await svc.fetch(FetchRequest(url=first))
    doc, hit = await svc.fetch(FetchRequest(url=second))
    assert hit and doc.url == second
    assert doc.final_url == first and doc.provenance.url == first


# 3. latency budget


async def test_static_gets_a_share_and_browser_the_rest(settings: Settings) -> None:
    url = "https://thin.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, s, b, _ = make({url: _thin(url)}, {url: rendered}, settings)
    await svc.fetch(FetchRequest(url=url, timeout_s=60))
    assert s.timeouts == [20]
    assert 50 < b.timeouts[0] <= 60
    await svc.fetch(FetchRequest(url=url, mode=FetchMode.STATIC, use_cache=False, timeout_s=60))
    assert s.timeouts[-1] == 60


async def test_total_time_bounded_when_both_stages_slow(settings: Settings) -> None:
    url = "https://slow.example/"
    svc, _, _, _ = make({}, {}, settings)
    svc._static, svc._browser = Slow(FetchMethod.STATIC, {}), Slow(FetchMethod.BROWSER, {})
    t0 = time.monotonic()
    with pytest.raises(ServiceError) as ei:
        await svc.fetch(FetchRequest(url=url, timeout_s=1))
    assert time.monotonic() - t0 <= 1.3
    assert ei.value.detail.code is ErrorCode.UPSTREAM_TIMEOUT and ei.value.http_status == 504


async def test_thin_then_browser_timeout_keeps_static(settings: Settings) -> None:
    url = "https://thin.example/"
    svc, _, _, _ = make({url: _thin(url)}, {}, settings)
    svc._browser = Slow(FetchMethod.BROWSER, {})
    t0 = time.monotonic()
    doc, _ = await svc.fetch(FetchRequest(url=url, timeout_s=1))
    assert time.monotonic() - t0 <= 1.3
    assert doc.provenance.method is FetchMethod.STATIC
    assert any(w.startswith("browser escalation failed") for w in doc.warnings)


async def test_slow_robots_bounded_by_budget(settings: Settings) -> None:
    class SlowRobots:
        async def check(self, url: str, em: Emitter | None = None) -> None:
            await asyncio.sleep(100)

    url = "https://blog.example/post"
    svc, s, _, _ = make({url: html_page(url, "article.html")}, {}, settings, robots=SlowRobots())
    with pytest.raises(ServiceError) as ei:
        await svc.fetch(FetchRequest(url=url, timeout_s=1))
    assert ei.value.detail.code is ErrorCode.UPSTREAM_TIMEOUT and s.calls == []


# 4. robots and politeness on every hop


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.now += s


async def test_browser_stage_respaced_by_crawl_delay(settings: Settings) -> None:
    url = "https://thin.example/"
    clock = Clock()
    limiter = DomainLimiter(1, 0, clock=clock, sleep=clock.sleep)
    limiter.set_delay("thin.example", 3)  # as robots.txt Crawl-delay would
    starts: dict[str, float] = {}

    class Stamp(FakeFetcher):
        async def fetch(
            self, url: str, *, timeout_s: float, on_redirect: RedirectHook | None = None
        ) -> RawPage:
            starts[self.method.value] = clock.now
            return await super().fetch(url, timeout_s=timeout_s)

    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, _, _, _ = make({}, {}, settings, limiter=limiter)
    svc._static = Stamp(FetchMethod.STATIC, {url: _thin(url)})
    svc._browser = Stamp(FetchMethod.BROWSER, {url: rendered})
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.provenance.method is FetchMethod.BROWSER
    assert starts["browser"] - starts["static"] >= 3


def _real_static(client: httpx.AsyncClient, guard: SsrfGuard) -> StaticFetcher:
    return StaticFetcher(
        client,
        guard,
        max_bytes=1_000_000,
        allowed_types=frozenset({"text/html"}),
        user_agent=UA,
    )


async def _public(_host: str) -> list[str]:
    return ["93.184.216.34"]


@respx.mock
async def test_cross_origin_redirect_checks_target_robots(settings: Settings) -> None:
    respx.get("https://a.example/robots.txt").respond(text="")
    b_robots = respx.get("https://b.example/robots.txt").respond(
        text="User-agent: *\nDisallow: /y\n"
    )
    respx.get("https://a.example/x").respond(302, headers={"Location": "https://b.example/y"})
    target = respx.get("https://b.example/y").respond(200, html="<p>x</p>")
    limiter = DomainLimiter(1, 0)
    guard = SsrfGuard(frozenset(), resolver=_public)
    async with make_fetch_client(UA) as client:
        robots = RobotsPolicy(client, UA, limiter, guard)
        svc, _, b, _ = make({}, {}, settings, robots=robots, limiter=limiter)
        svc._static = _real_static(client, guard)
        with pytest.raises(ServiceError) as ei:
            await svc.fetch(FetchRequest(url="https://a.example/x"))
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED
    assert b_robots.called and not target.called and b.calls == []


@respx.mock
async def test_cross_origin_redirect_takes_target_slot(settings: Settings) -> None:
    respx.get("https://a.example/robots.txt").respond(text="")
    respx.get("https://b.example/robots.txt").respond(text="")
    respx.get("https://a.example/x").respond(302, headers={"Location": "https://b.example/y"})
    respx.get("https://b.example/y").respond(200, html=(PAGES / "article.html").read_text())
    slots: list[str] = []

    class LogLimiter(DomainLimiter):
        def slot(self, url: str):  # type: ignore[override]
            slots.append(url)
            return super().slot(url)

    limiter = LogLimiter(1, 0)
    guard = SsrfGuard(frozenset(), resolver=_public)
    async with make_fetch_client(UA) as client:
        robots = RobotsPolicy(client, UA, limiter, guard)
        svc, _, _, _ = make({}, {}, settings, robots=robots, limiter=limiter)
        svc._static = _real_static(client, guard)
        doc, _ = await svc.fetch(FetchRequest(url="https://a.example/x"))
    assert doc.final_url == "https://b.example/y"
    assert slots == ["https://a.example/x", "https://b.example/y"]


@respx.mock
async def test_same_origin_redirect_does_not_refetch_robots(settings: Settings) -> None:
    robots_route = respx.get("https://a.example/robots.txt").respond(text="")
    respx.get("https://a.example/x").respond(302, headers={"Location": "/z"})
    respx.get("https://a.example/z").respond(200, html=(PAGES / "article.html").read_text())
    limiter = DomainLimiter(1, 0)  # concurrency 1: re-taking the held slot would deadlock
    guard = SsrfGuard(frozenset(), resolver=_public)
    async with make_fetch_client(UA) as client:
        robots = RobotsPolicy(client, UA, limiter, guard)
        svc, _, _, _ = make({}, {}, settings, robots=robots, limiter=limiter)
        svc._static = _real_static(client, guard)
        async with asyncio.timeout(5):
            doc, _ = await svc.fetch(FetchRequest(url="https://a.example/x"))
    assert doc.final_url == "https://a.example/z" and robots_route.call_count == 1


async def test_same_origin_redirect_path_disallowed(settings: Settings) -> None:
    """Robots is checked per hop (cached per origin), so a redirect into a disallowed path fails."""

    class PathRobots:
        async def check(self, url: str, em: Emitter | None = None) -> None:
            if "/private" in url:
                raise ServiceError.of(ErrorCode.ROBOTS_DISALLOWED, "no", retryable=False)

    class Redirecting(FakeFetcher):
        async def fetch(
            self, url: str, *, timeout_s: float, on_redirect: RedirectHook | None = None
        ) -> RawPage:
            assert on_redirect is not None
            async with on_redirect("https://a.example/private"):
                raise AssertionError("must not fetch the disallowed hop")

    svc, _, _, _ = make({}, {}, settings, robots=PathRobots())
    svc._static = Redirecting(FetchMethod.STATIC, {})
    with pytest.raises(ServiceError) as ei:
        await svc.fetch(FetchRequest(url="https://a.example/x"))
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED


async def test_browser_final_url_checked_against_robots(settings: Settings) -> None:
    class DenyOther:
        async def check(self, url: str, em: Emitter | None = None) -> None:
            if "other.example" in url:
                raise ServiceError.of(ErrorCode.ROBOTS_DISALLOWED, "no", retryable=False)

    url = "https://app.example/"
    rendered = replace(
        html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300),
        final_url="https://other.example/landing",
    )
    svc, _, _, _ = make({}, {url: rendered}, settings, robots=DenyOther())
    with pytest.raises(ServiceError) as ei:
        await svc.fetch(FetchRequest(url=url, mode=FetchMode.BROWSER))
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED
