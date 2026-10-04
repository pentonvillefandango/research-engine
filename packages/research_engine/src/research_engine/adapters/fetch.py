"""Fetcher seam (V1-04). Concrete: StaticFetcher, Crawl4AIFetcher."""

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


class Fetcher(Protocol):
    async def fetch(self, url: str, *, timeout_s: float) -> RawPage: ...
    async def health(self) -> bool: ...
