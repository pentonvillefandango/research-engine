"""Tiered fetch orchestration (V1-04..V1-06, V1-10, V1-11).

cache -> robots -> [slot: static fetch] -> extract -> ([slot: browser]) -> Document, all inside one
budget of ``min(timeout_s, PAGE_TIMEOUT_S)`` (static gets a share in auto mode, the browser the
remainder).

- Robots and the per-domain slot apply to the requested URL before any network fetch; every
  static request (redirect hops, retries) is robots-checked and sent under its domain's slot.
  The browser's final URL is robots-checked after the fact (Chromium follows redirects
  internally).
- The slot is released after the static stage and re-acquired for the browser stage, so the
  browser request is spaced by the crawl-delay.
- Each fetcher SSRF-checks every hop itself.
- Extractors are synchronous and always run in a worker thread (Global Constraint 6).
"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from research_engine_client.models import (
    Document,
    DocumentFormat,
    ErrorCode,
    EscalationReason,
    EventKind,
    FetchMethod,
    FetchMode,
    FetchRequest,
    Provenance,
    Quality,
)

from research_engine.adapters.extract import Extracted, HtmlExtractor, PdfExtractor
from research_engine.adapters.fetch import Fetcher, HopHook, RawPage
from research_engine.cache.base import Cache, cache_key
from research_engine.config import Settings
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter, EventSink
from research_engine.safety.limiter import DomainLimiter, limiter_key

from .heuristics import looks_js_rendered
from .urls import fetch_cache_url

# Policy and safety refusals: a browser would be refused for the same reason.
NO_ESCALATE = frozenset(
    {
        ErrorCode.SSRF_BLOCKED,
        ErrorCode.ROBOTS_DISALLOWED,
        ErrorCode.CONTENT_TYPE_NOT_ALLOWED,
        ErrorCode.RESPONSE_TOO_LARGE,
    }
)
# A static failure escalates only if a browser could plausibly succeed: transient errors
# (5xx, network, timeouts) and bot/auth walls. 404, 410 and other definitive 4xx are final.
BOT_WALL_STATUSES = frozenset({401, 403, 429, 999})
STATIC_SHARE = 1 / 3  # auto mode: the static stage gets this share of timeout_s ...
STATIC_FLOOR_S = 10.0  # ... but at least this, and never more than two thirds
BROWSER_RESERVE_SHARE = 0.1  # kept back from the browser for extraction ...
BROWSER_RESERVE_MAX_S = 2.0  # ... up to this much
BROWSER_FAILED_PREFIX = "browser escalation failed: "
GONE_STATUSES = frozenset({404, 410})  # a browser result with these raises like a static one
# Notes about Trafilatura's markdown, moot once the browser's markdown replaces it.
_MARKDOWN_ONLY_WARNINGS = ("trafilatura ",)


def should_escalate(exc: ServiceError) -> bool:
    if exc.detail.code in NO_ESCALATE:
        return False
    status = exc.upstream_status
    if status in BOT_WALL_STATUSES:
        return True
    if status is not None and 400 <= status < 500:
        return False
    return exc.detail.retryable


def static_budget(timeout_s: float) -> float:
    return min(max(STATIC_FLOOR_S, timeout_s * STATIC_SHARE), timeout_s * 2 / 3)


def page_cache_key(req: FetchRequest) -> str:
    """Conservative URL form; format order, ``timeout_s`` and ``use_cache`` don't matter."""
    payload = req.model_dump(mode="json", exclude={"timeout_s", "use_cache"})
    payload["url"] = fetch_cache_url(req.url)
    payload["formats"] = sorted(set(payload["formats"]))
    return cache_key("fetch", json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _cacheable(doc: Document) -> bool:
    """Error pages and transient browser failures are not pinned for the page TTL."""
    return doc.status < 400 and not any(w.startswith(BROWSER_FAILED_PREFIX) for w in doc.warnings)


def _timeout(url: str, why: str) -> ServiceError:
    return ServiceError.of(
        ErrorCode.UPSTREAM_TIMEOUT, f"{why}: {url}", retryable=True, source=url, http_status=504
    )


class _HopSlot:
    """The single DomainLimiter slot a static stage holds; moves with cross-domain hops."""

    def __init__(self, limiter: DomainLimiter) -> None:
        self._limiter = limiter
        self._key: str | None = None
        self._cm: AbstractAsyncContextManager[None] | None = None

    async def move_to(self, url: str) -> None:
        key = limiter_key(url)
        if key == self._key:
            return
        await self.release()  # never wait for one slot while holding another
        cm = self._limiter.slot(url)
        await cm.__aenter__()  # if this is cancelled, the limiter's own cleanup runs
        self._cm, self._key = cm, key

    async def release(self) -> None:
        cm, self._cm, self._key = self._cm, None, None
        if cm is not None:
            await cm.__aexit__(None, None, None)


class RobotsChecker(Protocol):
    """What FetchService needs from ``safety.robots.RobotsPolicy``."""

    async def check(self, url: str, em: Emitter | None = None) -> None: ...


class FetchService:
    def __init__(
        self,
        static: Fetcher,
        browser: Fetcher,
        html: HtmlExtractor,
        pdf: PdfExtractor,
        robots: RobotsChecker,
        limiter: DomainLimiter,
        cache: Cache,
        events: EventSink,
        settings: Settings,
    ) -> None:
        self._static, self._browser, self._html, self._pdf = static, browser, html, pdf
        self._robots, self._limiter, self._cache = robots, limiter, cache
        self._events, self._settings = events, settings

    async def browser_health(self) -> bool:
        """Is the browser-rendering backend reachable (for ``/health``)."""
        return await self._browser.health()

    async def fetch(self, req: FetchRequest, *, job_id: str | None = None) -> tuple[Document, bool]:
        em = Emitter(self._events, job_id)
        key = page_cache_key(req)
        if req.use_cache and (cached := await self._cache.get(key)) is not None:
            await em.info(EventKind.CACHE_HIT, f"page cache hit: {req.url}", url=req.url)
            doc = Document.model_validate_json(cached)
            return doc.model_copy(update={"url": req.url}), True

        req = self._capped(req)
        await em.info(EventKind.FETCH_STARTED, f"fetch {req.url}", url=req.url, mode=req.mode.value)
        deadline = asyncio.get_running_loop().time() + req.timeout_s
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    await self._robots.check(req.url, em)
                    doc = await self._run(req, em, job_id, deadline)
            except TimeoutError as exc:
                raise _timeout(req.url, f"fetch exceeded its {req.timeout_s}s budget") from exc
        except ServiceError as exc:
            await em.error(
                EventKind.FETCH_FAILED,
                f"fetch failed: {exc.detail.message}",
                url=req.url,
                code=exc.detail.code.value,
            )
            raise
        if _cacheable(doc):
            await self._cache.set(
                key, doc.model_dump_json().encode(), self._settings.cache_ttl_page_s
            )
        method = doc.provenance.method.value
        await em.info(
            EventKind.FETCH_DONE,
            f"fetched {req.url} ({doc.word_count} words, {method})",
            url=req.url,
            words=doc.word_count,
            method=method,
        )
        return doc, False

    def _capped(self, req: FetchRequest) -> FetchRequest:
        """PAGE_TIMEOUT_S is the operator's upper bound on any single page fetch: the
        effective budget is ``min(req.timeout_s, page_timeout_s)`` (REST and job pages)."""
        cap = self._settings.page_timeout_s
        return req if req.timeout_s <= cap else req.model_copy(update={"timeout_s": cap})

    async def _run(
        self, req: FetchRequest, em: Emitter, job_id: str | None, deadline: float
    ) -> Document:
        if req.mode is FetchMode.BROWSER:
            return await self._browser_doc(req, em, job_id, EscalationReason.FORCED, [], deadline)
        budget = req.timeout_s if req.mode is FetchMode.STATIC else static_budget(req.timeout_s)
        try:
            raw = await self._static_stage(req, em, budget)
        except ServiceError as exc:
            if req.mode is FetchMode.STATIC or not should_escalate(exc):
                raise
            return await self._browser_doc(
                req,
                em,
                job_id,
                EscalationReason.STATIC_FAILED,
                [f"static fetch failed: {exc.detail.message}"],
                deadline,
            )
        if raw.content_type == "application/pdf":
            ex = await asyncio.to_thread(self._pdf.extract, raw.body, raw.final_url)
            return self._document(req, raw, ex, job_id, None, [])
        ex = await asyncio.to_thread(self._html.extract, raw.html or "", raw.final_url)
        await em.debug(
            EventKind.FETCH_STATIC_DONE,
            f"static: {ex.word_count} words",
            url=req.url,
            words=ex.word_count,
        )
        static_doc = self._document(req, raw, ex, job_id, None, [])
        if req.mode is FetchMode.STATIC:
            return static_doc
        threshold = self._settings.thin_word_threshold
        reason = None
        if looks_js_rendered(raw.html or "", word_count=ex.word_count, threshold=threshold):
            reason = EscalationReason.JS_RENDERED
        elif ex.word_count < threshold:
            reason = EscalationReason.THIN_CONTENT
        if reason is None:
            return static_doc
        try:
            return await self._browser_doc(req, em, job_id, reason, [], deadline)
        except ServiceError as exc:  # includes running out of budget
            if exc.detail.code in NO_ESCALATE:
                raise
            static_doc.warnings.append(f"{BROWSER_FAILED_PREFIX}{exc.detail.message}")
            return static_doc

    async def _static_stage(self, req: FetchRequest, em: Emitter, budget: float) -> RawPage:
        """Politeness for the static stage: exactly one domain slot at a time.

        The fetcher calls the hop hook around every request it sends (each redirect hop and
        the first request of each retry attempt), so each request goes out holding its own
        domain's slot. The slot follows the requests: a cross-domain move releases the current
        slot before waiting for the next one (never hold-and-wait, so crossing redirects can't
        deadlock); a same-domain request keeps it. Every exit path (result, error, deadline,
        cancellation) releases whatever slot is current.
        """
        slot = _HopSlot(self._limiter)
        try:
            await slot.move_to(req.url)
            try:
                async with asyncio.timeout(budget):
                    return await self._static.fetch(
                        req.url, timeout_s=budget, on_hop=self._hop_hook(em, slot)
                    )
            except TimeoutError as exc:
                raise _timeout(req.url, f"static fetch exceeded {budget:g}s") from exc
        finally:
            await slot.release()

    def _hop_hook(self, em: Emitter, slot: "_HopSlot") -> HopHook:
        @asynccontextmanager
        async def hook(url: str) -> AsyncIterator[None]:
            await self._robots.check(url, em)  # cached per origin: no refetch on same origin
            await slot.move_to(url)
            yield  # the slot stays current after the hop; the stage releases it at the end

        return hook

    async def _browser_doc(
        self,
        req: FetchRequest,
        em: Emitter,
        job_id: str | None,
        reason: EscalationReason,
        warnings: list[str],
        deadline: float,
    ) -> Document:
        await em.info(
            EventKind.FETCH_ESCALATED,
            f"escalating to browser ({reason.value})",
            url=req.url,
            reason=reason.value,
        )
        raw = await self._browser_stage(req, deadline)
        if raw.status in GONE_STATUSES:  # same outcome as a static 404/410
            raise ServiceError.of(
                ErrorCode.FETCH_FAILED,
                f"HTTP {raw.status} from {raw.final_url}",
                retryable=False,
                source=raw.final_url,
                upstream_status=raw.status,
            )
        if raw.final_url != req.url:  # Chromium followed redirects we could not vet per hop
            await self._robots.check(raw.final_url, em)
        ex = await asyncio.to_thread(self._html.extract, raw.html or "", raw.final_url)
        if raw.markdown and raw.markdown.strip():
            ex = replace(
                ex,
                markdown=raw.markdown,
                word_count=len(raw.markdown.split()),
                warnings=[w for w in ex.warnings if not w.startswith(_MARKDOWN_ONLY_WARNINGS)],
            )
        await em.info(
            EventKind.FETCH_BROWSER_DONE,
            f"browser: {ex.word_count} words",
            url=req.url,
            words=ex.word_count,
        )
        notes = [*warnings, f"escalated to browser: {reason.value}"]
        return self._document(req, raw, ex, job_id, reason, notes)

    async def _browser_stage(self, req: FetchRequest, deadline: float) -> RawPage:
        """Re-acquire the domain slot (spaced by crawl-delay) and use the remaining budget,
        keeping a small reserve for extraction."""
        loop = asyncio.get_running_loop()
        stop = deadline - min(BROWSER_RESERVE_MAX_S, req.timeout_s * BROWSER_RESERVE_SHARE)
        try:
            async with asyncio.timeout_at(stop):
                async with self._limiter.slot(req.url):
                    remaining = stop - loop.time()
                    if remaining <= 0:
                        raise TimeoutError
                    return await self._browser.fetch(req.url, timeout_s=remaining)
        except TimeoutError as exc:
            raise _timeout(req.url, "browser ran out of the fetch budget") from exc

    def _document(
        self,
        req: FetchRequest,
        raw: RawPage,
        ex: Extracted,
        job_id: str | None,
        reason: EscalationReason | None,
        notes: list[str],
    ) -> Document:
        digest = hashlib.sha256(ex.markdown.encode()).hexdigest()
        ratio = min(1.0, ex.text_len / ex.html_len) if ex.html_len else 0.0
        method = FetchMethod.STATIC if raw.method is FetchMethod.API else raw.method
        return Document(
            url=req.url,
            final_url=raw.final_url,
            status=raw.status,
            title=ex.title,
            author=ex.author,
            published_at=ex.published_at,
            language=ex.language,
            markdown=ex.markdown,
            html=raw.html if DocumentFormat.HTML in req.formats else None,
            word_count=ex.word_count,
            links=ex.links,
            tables=ex.tables,
            structured_data=ex.structured_data,
            provenance=Provenance(
                url=raw.final_url,
                fetched_at=datetime.now(UTC),
                content_hash=f"sha256:{digest}",
                method=method,
                job_id=job_id,
            ),
            quality=Quality(
                word_count=ex.word_count,
                text_html_ratio=round(ratio, 4),
                has_title=bool(ex.title),
                escalation_reason=reason,
            ),
            warnings=[*ex.warnings, *notes],
        )
