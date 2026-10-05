"""Fetcher seam (V1-04). Concrete: StaticFetcher, Crawl4AIFetcher."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol

from research_engine_client.models import FetchMethod


@dataclass
class RawPage:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    html: str | None
    markdown: str | None
    method: FetchMethod
    redirects: list[str] = field(default_factory=list)


HopHook = Callable[[str], AbstractAsyncContextManager[None]]
"""Entered around every request a fetcher sends (with the checked URL), exited after it.

That includes the first request of each retry attempt as well as each redirect hop. The
orchestrator uses it to apply robots.txt and the per-domain slot to every request; raising
inside it aborts the fetch before that request is sent.
"""


class Fetcher(Protocol):
    async def fetch(
        self, url: str, *, timeout_s: float, on_hop: HopHook | None = None
    ) -> RawPage: ...
    async def health(self) -> bool: ...
