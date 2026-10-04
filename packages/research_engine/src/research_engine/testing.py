"""Deterministic fakes for tests, examples and the GUI demo mode. No network."""

from research_engine_client.models import TimeRange

from research_engine.adapters.search import RawHit, RawSearchPage
from research_engine.api.deps import Services
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.search import SearchService


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


def build_test_services(settings: Settings) -> Services:
    intents = IntentRegistry.load(settings.intents_file)
    events, cache = InMemoryEventBus(), InMemoryCache()
    return Services(
        settings=settings,
        intents=intents,
        events=events,
        cache=cache,
        search=SearchService(FakeSearchProvider(), intents, cache, events, settings),
    )
