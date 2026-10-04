"""Tiered fetch orchestration (V1-04..V1-06, V1-10, V1-11).

cache -> robots -> politeness slot -> static fetch -> extract -> (escalate to browser) -> Document.
Robots and the per-domain slot are applied to the requested URL before any network fetch; each
fetcher SSRF-checks every hop itself. Extractors are synchronous and always run in a worker
thread (Global Constraint 6).
"""

import asyncio
import hashlib
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
from research_engine.adapters.fetch import Fetcher, RawPage
from research_engine.cache.base import Cache, cache_key
from research_engine.config import Settings
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter, EventSink
from research_engine.safety.limiter import DomainLimiter

from .heuristics import looks_js_rendered
from .urls import canonicalize_url

# Policy and safety refusals: a browser would be refused for the same reason.
NO_ESCALATE = frozenset(
    {
        ErrorCode.SSRF_BLOCKED,
        ErrorCode.ROBOTS_DISALLOWED,
        ErrorCode.CONTENT_TYPE_NOT_ALLOWED,
        ErrorCode.RESPONSE_TOO_LARGE,
    }
)
BROWSER_FAILED_PREFIX = "browser escalation failed: "
# Notes about Trafilatura's markdown, moot once the browser's markdown replaces it.
_MARKDOWN_ONLY_WARNINGS = ("trafilatura ",)


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

    async def fetch(self, req: FetchRequest, *, job_id: str | None = None) -> tuple[Document, bool]:
        em = Emitter(self._events, job_id)
        key = cache_key("fetch", req.model_copy(update={"url": canonicalize_url(req.url)}))
        if req.use_cache and (cached := await self._cache.get(key)) is not None:
            await em.info(EventKind.CACHE_HIT, f"page cache hit: {req.url}", url=req.url)
            return Document.model_validate_json(cached), True

        await em.info(EventKind.FETCH_STARTED, f"fetch {req.url}", url=req.url, mode=req.mode.value)
        try:
            await self._robots.check(req.url, em)
            async with self._limiter.slot(req.url):
                doc = await self._run(req, em, job_id)
        except ServiceError as exc:
            await em.error(
                EventKind.FETCH_FAILED,
                f"fetch failed: {exc.detail.message}",
                url=req.url,
                code=exc.detail.code.value,
            )
            raise
        if not any(w.startswith(BROWSER_FAILED_PREFIX) for w in doc.warnings):
            # A transient browser failure is not pinned in the cache for the page TTL.
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

    async def _run(self, req: FetchRequest, em: Emitter, job_id: str | None) -> Document:
        if req.mode is FetchMode.BROWSER:
            return await self._browser_doc(req, em, job_id, EscalationReason.FORCED, [])
        try:
            raw = await self._static.fetch(req.url, timeout_s=req.timeout_s)
        except ServiceError as exc:
            if req.mode is FetchMode.STATIC or exc.detail.code in NO_ESCALATE:
                raise
            return await self._browser_doc(
                req,
                em,
                job_id,
                EscalationReason.STATIC_FAILED,
                [f"static fetch failed: {exc.detail.message}"],
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
            return await self._browser_doc(req, em, job_id, reason, [])
        except ServiceError as exc:
            if exc.detail.code in NO_ESCALATE:
                raise
            static_doc.warnings.append(f"{BROWSER_FAILED_PREFIX}{exc.detail.message}")
            return static_doc

    async def _browser_doc(
        self,
        req: FetchRequest,
        em: Emitter,
        job_id: str | None,
        reason: EscalationReason,
        warnings: list[str],
    ) -> Document:
        await em.info(
            EventKind.FETCH_ESCALATED,
            f"escalating to browser ({reason.value})",
            url=req.url,
            reason=reason.value,
        )
        raw = await self._browser.fetch(req.url, timeout_s=req.timeout_s)
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
