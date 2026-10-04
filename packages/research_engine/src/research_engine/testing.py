"""Deterministic fakes for tests, examples and the GUI demo mode. No network."""

from pathlib import Path

import httpx
from research_engine_client.models import ErrorCode, FetchMethod, TimeRange

from research_engine.adapters.fetch import HopHook, RawPage
from research_engine.adapters.html_extract import DefaultHtmlExtractor
from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.adapters.search import RawHit, RawSearchPage
from research_engine.api.deps import Services
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.fetch import FetchService
from research_engine.pipeline.search import SearchService
from research_engine.safety.limiter import DomainLimiter

# Fixture pages, relative to the repo root (the working directory, like ``intents_file``).
FIXTURE_PAGES = Path("tests/fixtures/pages")
BLOCKED_HOST = "fake-blocked.example"
# url -> fixture file served by the fake static / browser fetchers
STATIC_PAGES = {
    "https://blog.example/post": "article.html",
    "https://app.example/": "spa.html",
    "https://docs.example/sample.pdf": "sample.pdf",
}
BROWSER_PAGES = {
    "https://app.example/": "article.html",
}


class FakeSearchProvider:
    async def search(
        self,
        query: str,
        *,
        categories: list[str],
        engines: list[str],
        language: str,
        time_range: TimeRange | None,
        pageno: int,
    ) -> RawSearchPage:
        hits = [
            RawHit(
                url=f"https://result{i}.example/{pageno}",
                title=f"{query} {i}",
                content="snippet",
                engines=["a", "b"] if i % 2 else ["a"],
                positions=[i + (pageno - 1) * 10],
                published=None,
            )
            for i in range(1, 13)
        ]
        return RawSearchPage(hits=hits, suggestions=[], infoboxes=[], unresponsive=[])

    async def health(self) -> bool:
        return True


class FakePageFetcher:
    """Serves fixture pages by exact URL; ``fake-blocked.example`` is SSRF-blocked."""

    def __init__(
        self, method: FetchMethod, pages: dict[str, str], pages_dir: Path = FIXTURE_PAGES
    ) -> None:
        self._method = method
        self._pages = pages
        self._dir = pages_dir

    async def fetch(self, url: str, *, timeout_s: float, on_hop: HopHook | None = None) -> RawPage:
        if httpx.URL(url).host == BLOCKED_HOST:
            raise ServiceError.of(
                ErrorCode.SSRF_BLOCKED,
                f"blocked: {BLOCKED_HOST} resolves to non-public address 10.0.0.1",
                retryable=False,
                source=url,
                http_status=403,
            )
        name = self._pages.get(url)
        if name is None:
            raise ServiceError.of(
                ErrorCode.FETCH_FAILED,
                f"HTTP 404 from {url}",
                retryable=False,
                source=url,
                upstream_status=404,
            )
        body = (self._dir / name).read_bytes()
        is_pdf = name.endswith(".pdf")
        html = None if is_pdf else body.decode("utf-8")
        return RawPage(
            url=url,
            final_url=url,
            status=200,
            content_type="application/pdf" if is_pdf else "text/html",
            body=body,
            html=html,
            markdown=None,
            method=self._method,
        )

    async def health(self) -> bool:
        return True


class AllowAllRobots:
    async def check(self, url: str, em: Emitter | None = None) -> None:
        return None


def build_test_services(settings: Settings) -> Services:
    intents = IntentRegistry.load(settings.intents_file)
    events, cache = InMemoryEventBus(), InMemoryCache()
    fetch = FetchService(
        FakePageFetcher(FetchMethod.STATIC, STATIC_PAGES),
        FakePageFetcher(FetchMethod.BROWSER, BROWSER_PAGES),
        DefaultHtmlExtractor(),
        PypdfExtractor(),
        AllowAllRobots(),
        DomainLimiter(settings.domain_concurrency, 0),
        cache,
        events,
        settings,
    )
    return Services(
        settings=settings,
        intents=intents,
        events=events,
        cache=cache,
        search=SearchService(FakeSearchProvider(), intents, cache, events, settings),
        fetch=fetch,
    )
