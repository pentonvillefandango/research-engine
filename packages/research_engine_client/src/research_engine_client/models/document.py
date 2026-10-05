"""Fetch request and Document models (V1-04..V1-06)."""

from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field

from ._base import Model, RequestModel, UtcDatetime


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    return value


HttpUrlStr = Annotated[str, Field(max_length=4096), AfterValidator(_http_url)]


class FetchMode(StrEnum):
    AUTO = "auto"
    STATIC = "static"
    BROWSER = "browser"


class DocumentFormat(StrEnum):
    MARKDOWN = "markdown"
    HTML = "html"


class FetchMethod(StrEnum):
    STATIC = "static"
    BROWSER = "browser"
    API = "api"
    ARCHIVE = "archive"


class EscalationReason(StrEnum):
    STATIC_FAILED = "static_failed"
    THIN_CONTENT = "thin_content"
    JS_RENDERED = "js_rendered"
    FORCED = "forced"


class FetchRequest(RequestModel):
    url: HttpUrlStr
    mode: FetchMode = FetchMode.AUTO
    formats: tuple[DocumentFormat, ...] = (DocumentFormat.MARKDOWN,)
    use_cache: bool = True
    timeout_s: int = Field(default=60, ge=1, le=300)


class Link(Model):
    url: str
    text: str
    external: bool


class Table(Model):
    caption: str | None = None
    headers: list[str] = Field(default_factory=list[str])
    rows: list[list[str]] = Field(default_factory=list[list[str]])
    source_selector: str | None = None


class StructuredData(Model):
    json_ld: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    microdata: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    opengraph: dict[str, Any] = Field(default_factory=dict[str, Any])


class Provenance(Model):
    url: str
    fetched_at: UtcDatetime
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    method: FetchMethod
    job_id: str | None = None


class Quality(Model):
    word_count: int = Field(ge=0)
    text_html_ratio: float = Field(ge=0, le=1)
    has_title: bool
    escalation_reason: EscalationReason | None = None


class Document(Model):
    url: str
    final_url: str
    status: int
    title: str | None = None
    author: str | None = None
    published_at: UtcDatetime | None = None
    language: str | None = None
    markdown: str
    html: str | None = Field(default=None, description="Raw HTML, only when 'html' is in formats")
    word_count: int = Field(ge=0)
    links: list[Link] = Field(default_factory=list[Link])
    tables: list[Table] = Field(default_factory=list[Table])
    structured_data: StructuredData = Field(default_factory=StructuredData)
    provenance: Provenance
    quality: Quality
    warnings: list[str] = Field(default_factory=list[str])
