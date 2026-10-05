import os

import httpx
import pytest
from research_engine.adapters.searxng import SearxngProvider
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.search import SearchService
from research_engine_client.models import SearchIntent, SearchRequest

pytestmark = pytest.mark.integration


async def test_technical_query_meets_acceptance(settings_env: None) -> None:
    url = os.environ["SEARXNG_LIVE_URL"]
    async with httpx.AsyncClient() as http:
        svc = SearchService(
            SearxngProvider(url, http, 30),
            IntentRegistry.load("config/intents.yaml"),
            InMemoryCache(),
            InMemoryEventBus(),
            Settings(),  # type: ignore[call-arg]
        )
        resp, _ = await svc.search(
            SearchRequest(
                query="python asyncio TaskGroup exception handling",
                intent=SearchIntent.TECHNICAL,
                use_cache=False,
            )
        )
    engines = {e for r in resp.results for e in r.engines}
    print(f"RESULTS={len(resp.results)} ENGINES={sorted(engines)}", resp.unresponsive_engines)
    assert len(resp.results) >= 10, resp.unresponsive_engines
    assert len(engines) >= 3, engines
