"""Search orchestration: presets, depth, broader retry, events, cache (V1-01..V1-03)."""

import asyncio

from research_engine_client.models import (
    EventKind,
    SearchDepth,
    SearchIntent,
    SearchRequest,
    SearchResponse,
    UnresponsiveEngine,
)

from research_engine.adapters.search import RawSearchPage, SearchProvider
from research_engine.cache.base import Cache, cache_key
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter, EventSink

from .ranking import merge_and_score

PAGES_FOR_DEPTH = {SearchDepth.QUICK: 1, SearchDepth.STANDARD: 2, SearchDepth.DEEP: 3}


class SearchService:
    def __init__(
        self,
        provider: SearchProvider,
        intents: IntentRegistry,
        cache: Cache,
        events: EventSink,
        settings: Settings,
    ) -> None:
        self._provider = provider
        self._intents = intents
        self._cache = cache
        self._events = events
        self._settings = settings

    async def search(
        self, req: SearchRequest, *, job_id: str | None = None
    ) -> tuple[SearchResponse, bool]:
        em = Emitter(self._events, job_id)
        key = cache_key("search", req)
        if req.use_cache and (cached := await self._cache.get(key)) is not None:
            await em.info(EventKind.CACHE_HIT, f"search cache hit: {req.query}", query=req.query)
            return SearchResponse.model_validate_json(cached), True

        preset = self._intents.get(req.intent)
        engines = list(req.engines) if req.engines else preset.engines
        await em.info(
            EventKind.SEARCH_STARTED,
            f"search: {req.query}",
            query=req.query,
            intent=req.intent.value,
            engines=engines,
        )
        pages, failures = await self._fetch_pages(
            req, preset.categories, engines, range(1, PAGES_FOR_DEPTH[req.depth] + 1)
        )
        if not pages:
            raise failures[0]
        results = merge_and_score(pages, req.max_results)

        threshold = min(self._settings.search_min_results, req.max_results)
        if len(results) < threshold and req.intent is not SearchIntent.GENERAL and not req.engines:
            await em.info(
                EventKind.SEARCH_RETRY_BROADER,
                f"only {len(results)} results; retrying broader",
                found=len(results),
                threshold=threshold,
            )
            general = self._intents.get(SearchIntent.GENERAL)
            more, more_fail = await self._fetch_pages(req, general.categories, general.engines, [1])
            pages += more
            failures += more_fail
            results = merge_and_score(pages, req.max_results)

        unresponsive = self._unresponsive(pages, failures)
        for u in unresponsive:
            await em.warning(
                EventKind.SEARCH_ENGINE_FAILED,
                f"engine {u.engine} failed: {u.error}",
                engine=u.engine,
                error=u.error,
            )
        resp = SearchResponse(
            query=req.query,
            results=results,
            suggestions=list(dict.fromkeys(s for p in pages for s in p.suggestions)),
            infoboxes=[i for p in pages for i in p.infoboxes],
            unresponsive_engines=unresponsive,
        )
        if not failures:  # never cache a degraded response for the full TTL
            await self._cache.set(
                key, resp.model_dump_json().encode(), self._settings.cache_ttl_search_s
            )
        await em.info(
            EventKind.SEARCH_DONE,
            f"{len(results)} results for: {req.query}",
            results=len(results),
            engines=len({e for r in results for e in r.engines}),
        )
        return resp, False

    async def _fetch_pages(
        self,
        req: SearchRequest,
        categories: list[str],
        engines: list[str],
        pagenos: range | list[int],
    ) -> tuple[list[RawSearchPage], list[ServiceError]]:
        outcomes = await asyncio.gather(
            *(
                self._provider.search(
                    req.query,
                    categories=categories,
                    engines=engines,
                    language=req.language,
                    time_range=req.time_range,
                    pageno=p,
                )
                for p in pagenos
            ),
            return_exceptions=True,
        )
        pages = [o for o in outcomes if isinstance(o, RawSearchPage)]
        failures = [o for o in outcomes if isinstance(o, ServiceError)]
        for o in outcomes:
            if isinstance(o, BaseException) and not isinstance(o, ServiceError):
                raise o
        return pages, failures

    @staticmethod
    def _unresponsive(
        pages: list[RawSearchPage], failures: list[ServiceError]
    ) -> list[UnresponsiveEngine]:
        seen: dict[str, UnresponsiveEngine] = {}
        for p in pages:
            for u in p.unresponsive:
                seen.setdefault(u.engine, u)
        if failures:
            seen.setdefault(
                "searxng", UnresponsiveEngine(engine="searxng", error=failures[0].detail.message)
            )
        return sorted(seen.values(), key=lambda u: u.engine)
