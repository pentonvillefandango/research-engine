from dataclasses import dataclass, field
from pathlib import Path

import pytest
from research_engine.adapters.search import RawHit, RawSearchPage
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.errors import ServiceError
from research_engine.pipeline.search import SearchService
from research_engine_client.models import (
    ErrorCode,
    Event,
    EventKind,
    SearchDepth,
    SearchIntent,
    SearchRequest,
    TimeRange,
    UnresponsiveEngine,
)

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class Call:
    categories: list[str]
    engines: list[str]
    pageno: int


@dataclass
class FakeProvider:
    per_call_hits: int = 6
    fail_pages: set[int] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)

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
        self.calls.append(Call(categories, engines, pageno))
        if pageno in self.fail_pages:
            raise ServiceError.of(
                ErrorCode.UPSTREAM_TIMEOUT, "slow", retryable=True, source="searxng"
            )
        tag = "g" if not engines else engines[0]
        hits = [
            RawHit(
                url=f"https://{tag}{pageno}-{i}.example/",
                title="t",
                content="c",
                engines=[tag],
                positions=[i + (pageno - 1) * 10],
                published=None,
            )
            for i in range(1, self.per_call_hits + 1)
        ]
        return RawSearchPage(
            hits=hits,
            suggestions=["s"],
            infoboxes=[],
            unresponsive=[UnresponsiveEngine(engine="bing", error="captcha")],
        )

    async def health(self) -> bool:
        return True


class Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


Parts = tuple[FakeProvider, Sink, InMemoryCache, SearchService]


@pytest.fixture
def svc_parts(settings_env: None) -> Parts:
    provider, sink, cache = FakeProvider(), Sink(), InMemoryCache()
    svc = SearchService(
        provider,
        IntentRegistry.load(ROOT / "config/intents.yaml"),
        cache,
        sink,
        Settings(),  # type: ignore[call-arg]
    )
    return provider, sink, cache, svc


async def test_depth_pages_and_preset(svc_parts: Parts) -> None:
    provider, sink, _, svc = svc_parts
    resp, hit = await svc.search(
        SearchRequest(query="q", intent=SearchIntent.TECHNICAL, depth=SearchDepth.STANDARD),
        job_id="j1",
    )
    preset = IntentRegistry.load(ROOT / "config/intents.yaml").get(SearchIntent.TECHNICAL)
    assert not hit and sorted(c.pageno for c in provider.calls) == [1, 2]
    assert all(c.engines == preset.engines for c in provider.calls)
    assert all(c.categories == preset.categories for c in provider.calls)
    assert len(resp.results) == 12
    kinds = [e.kind for e in sink.events]
    assert kinds[0] is EventKind.SEARCH_STARTED and kinds[-1] is EventKind.SEARCH_DONE
    assert all(e.job_id == "j1" for e in sink.events)
    assert [u.engine for u in resp.unresponsive_engines] == ["bing"]
    assert kinds.count(EventKind.SEARCH_ENGINE_FAILED) == 1
    done = sink.events[-1].data
    assert done == {"results": 12, "engines": 1}


async def test_depth_page_counts(svc_parts: Parts) -> None:
    provider, _, _, svc = svc_parts
    await svc.search(SearchRequest(query="q", depth=SearchDepth.DEEP))
    assert sorted(c.pageno for c in provider.calls) == [1, 2, 3]


async def test_broader_retry_when_thin(svc_parts: Parts) -> None:
    provider, sink, _, svc = svc_parts
    provider.per_call_hits = 2
    resp, _ = await svc.search(
        SearchRequest(query="q", intent=SearchIntent.NEWS, depth=SearchDepth.QUICK)
    )
    kinds = [e.kind for e in sink.events]
    assert kinds.count(EventKind.SEARCH_RETRY_BROADER) == 1
    assert provider.calls[-1].engines == [] and provider.calls[-1].categories == ["general"]
    assert len(resp.results) == 4


async def test_no_retry_when_engines_overridden(svc_parts: Parts) -> None:
    provider, sink, _, svc = svc_parts
    provider.per_call_hits = 1
    await svc.search(
        SearchRequest(query="q", intent=SearchIntent.NEWS, engines=("x",), depth=SearchDepth.QUICK)
    )
    assert EventKind.SEARCH_RETRY_BROADER not in [e.kind for e in sink.events]
    assert provider.calls[0].engines == ["x"]


async def test_no_retry_for_general_intent(svc_parts: Parts) -> None:
    provider, sink, _, svc = svc_parts
    provider.per_call_hits = 1
    await svc.search(SearchRequest(query="q", depth=SearchDepth.QUICK))
    assert EventKind.SEARCH_RETRY_BROADER not in [e.kind for e in sink.events]
    assert len(provider.calls) == 1


async def test_partial_page_failure(svc_parts: Parts) -> None:
    provider, _, _, svc = svc_parts
    provider.fail_pages = {2}
    resp, _ = await svc.search(SearchRequest(query="q", depth=SearchDepth.STANDARD))
    assert len(resp.results) == 6
    assert "searxng" in [u.engine for u in resp.unresponsive_engines]


async def test_all_pages_fail_raises(svc_parts: Parts) -> None:
    provider, _, _, svc = svc_parts
    provider.fail_pages = {1, 2, 3}
    with pytest.raises(ServiceError):
        await svc.search(SearchRequest(query="q", depth=SearchDepth.DEEP))


async def test_cache_hit_and_bypass(svc_parts: Parts) -> None:
    provider, sink, _, svc = svc_parts
    req = SearchRequest(query="q", depth=SearchDepth.QUICK)
    await svc.search(req)
    n = len(provider.calls)
    _, hit = await svc.search(req)
    assert hit and len(provider.calls) == n
    assert EventKind.CACHE_HIT in [e.kind for e in sink.events]
    _, hit2 = await svc.search(req.model_copy(update={"use_cache": False}))
    assert not hit2 and len(provider.calls) == n + 1
