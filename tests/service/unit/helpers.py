"""Shared fakes for the job-handler tests."""

import asyncio
from datetime import UTC, datetime

from pydantic import BaseModel
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter
from research_engine.events.memory import InMemoryEventBus
from research_engine.jobs.context import JobContext
from research_engine_client.models import (
    Document,
    ErrorCode,
    FetchMethod,
    FetchRequest,
    Provenance,
    Quality,
)


def doc(url: str) -> Document:
    return Document(
        url=url,
        final_url=url,
        status=200,
        title="t",
        language="en",
        markdown="m",
        word_count=1,
        provenance=Provenance(
            url=url,
            fetched_at=datetime.now(UTC),
            content_hash="sha256:" + "0" * 64,
            method=FetchMethod.STATIC,
        ),
        quality=Quality(word_count=1, text_html_ratio=0.1, has_title=True),
    )


class FakeFetch:
    def __init__(
        self,
        fail: set[str] | None = None,
        delay: float = 0.01,
        saturate_at: int = 1,
        cleanup_s: float = 0.0,
        crash: set[str] | None = None,
    ) -> None:
        self.fail = fail or set()
        self.crash = crash or set()
        """URLs whose fetch raises a non-ServiceError (a bug in an extractor, say)."""
        self.cleanup_s = cleanup_s
        self.cleaned = 0
        """Fetches whose slow post-cancellation cleanup ran to completion."""
        self.saturated = asyncio.Event()
        """Set once ``saturate_at`` fetches are in flight at the same time."""
        self.saturate_at = saturate_at
        self.delay = delay
        self.inflight = 0
        self.peak = 0
        self.cancelled = 0
        self.calls: list[tuple[str, str | None]] = []

    async def fetch(self, req: FetchRequest, *, job_id: str | None = None) -> tuple[Document, bool]:
        self.calls.append((req.url, job_id))
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        if self.inflight >= self.saturate_at:
            self.saturated.set()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            if self.cleanup_s:
                await asyncio.sleep(self.cleanup_s)
            self.cleaned += 1
            raise
        finally:
            self.inflight -= 1
        if req.url in self.crash:
            raise ValueError("pypdf boom")
        if req.url in self.fail:
            raise ServiceError.of(ErrorCode.FETCH_FAILED, "nope", retryable=True, source=req.url)
        return doc(req.url), False


def make_ctx(req: BaseModel, progress: list[tuple[int, int, str | None]]) -> JobContext:
    async def persist(d: int, t: int, c: str | None) -> None:
        progress.append((d, t, c))

    return JobContext(
        job_id="J", request=req, emitter=Emitter(InMemoryEventBus(), "J"), _persist_progress=persist
    )
