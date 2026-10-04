"""Extractor protocols and the shared result type (Shared Interface Contract)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from research_engine_client.models import Link, StructuredData, Table


@dataclass
class Extracted:
    title: str | None
    author: str | None
    published_at: datetime | None
    language: str | None
    markdown: str
    word_count: int
    links: list[Link]
    tables: list[Table]
    structured_data: StructuredData
    html_len: int
    text_len: int


class HtmlExtractor(Protocol):
    def extract(self, html: str, base_url: str) -> Extracted:
        """Synchronous; callers run it via asyncio.to_thread."""
        ...


class PdfExtractor(Protocol):
    def extract(self, body: bytes, url: str) -> Extracted:
        """Synchronous; callers run it via asyncio.to_thread."""
        ...
