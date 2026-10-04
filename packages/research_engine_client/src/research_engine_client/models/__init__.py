"""Public, versioned data contracts for the Research Engine (REQUIREMENTS §4)."""

from pydantic import BaseModel

from .common import SCHEMA_VERSION, Envelope, ErrorCode, ErrorDetail, Meta
from .document import (
    Document,
    DocumentFormat,
    EscalationReason,
    FetchMethod,
    FetchMode,
    FetchRequest,
    Link,
    Provenance,
    Quality,
    StructuredData,
    Table,
)
from .events import Event, EventKind, EventLevel
from .health import DependencyHealth, DependencyState, HealthReport, VersionInfo
from .jobs import (
    BatchFetchRequest,
    BatchFetchResult,
    FailedUrl,
    FetchRequestOptions,
    Job,
    JobDetail,
    JobProgress,
    JobStatus,
    JobType,
    RankedDocument,
    SearchReadRequest,
    SearchReadResult,
)
from .search import (
    EngineInfo,
    EnginesResponse,
    Infobox,
    InfoboxLink,
    IntentPreset,
    SearchDepth,
    SearchIntent,
    SearchRequest,
    SearchResponse,
    SearchResult,
    TimeRange,
    UnresponsiveEngine,
)

_PLAIN: list[type[BaseModel]] = [
    ErrorDetail,
    Meta,
    SearchRequest,
    SearchResult,
    SearchResponse,
    Infobox,
    UnresponsiveEngine,
    EnginesResponse,
    FetchRequest,
    Document,
    Table,
    StructuredData,
    Provenance,
    Quality,
    Link,
    Job,
    JobDetail,
    BatchFetchRequest,
    SearchReadRequest,
    BatchFetchResult,
    SearchReadResult,
    Event,
    HealthReport,
    VersionInfo,
]
_ENVELOPED: list[type[BaseModel]] = [
    SearchResponse,
    Document,
    Job,
    JobDetail,
    EnginesResponse,
    HealthReport,
    VersionInfo,
]


def _envelope_name(inner: type[BaseModel]) -> str:
    return f"Envelope_{inner.__name__}_"


ALL_MODELS: dict[str, type[BaseModel]] = {m.__name__: m for m in _PLAIN} | {
    # `m` is a runtime variable, which a static type checker cannot accept as a type argument.
    _envelope_name(m): Envelope[m]  # pyright: ignore[reportInvalidTypeArguments]
    for m in _ENVELOPED
}

__all__ = [
    "ALL_MODELS",
    "SCHEMA_VERSION",
    "BatchFetchRequest",
    "BatchFetchResult",
    "DependencyHealth",
    "DependencyState",
    "Document",
    "DocumentFormat",
    "EngineInfo",
    "EnginesResponse",
    "Envelope",
    "ErrorCode",
    "ErrorDetail",
    "EscalationReason",
    "Event",
    "EventKind",
    "EventLevel",
    "FailedUrl",
    "FetchMethod",
    "FetchMode",
    "FetchRequest",
    "FetchRequestOptions",
    "HealthReport",
    "Infobox",
    "InfoboxLink",
    "IntentPreset",
    "Job",
    "JobDetail",
    "JobProgress",
    "JobStatus",
    "JobType",
    "Link",
    "Meta",
    "Provenance",
    "Quality",
    "RankedDocument",
    "SearchDepth",
    "SearchIntent",
    "SearchReadRequest",
    "SearchReadResult",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "StructuredData",
    "Table",
    "TimeRange",
    "UnresponsiveEngine",
    "VersionInfo",
]
