"""Event model (V1-15)."""

from enum import StrEnum
from typing import Any

from pydantic import Field

from ._base import Model, UtcDatetime


class EventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventKind(StrEnum):
    SEARCH_STARTED = "search.started"
    SEARCH_ENGINE_FAILED = "search.engine_failed"
    SEARCH_RETRY_BROADER = "search.retry_broader"
    SEARCH_DONE = "search.done"
    FETCH_STARTED = "fetch.started"
    FETCH_STATIC_DONE = "fetch.static_done"
    FETCH_ESCALATED = "fetch.escalated"
    FETCH_BROWSER_DONE = "fetch.browser_done"
    FETCH_FAILED = "fetch.failed"
    FETCH_DONE = "fetch.done"
    ROBOTS_DISALLOWED = "robots.disallowed"
    ROBOTS_FETCHED = "robots.fetched"
    CACHE_HIT = "cache.hit"
    CACHE_MISS = "cache.miss"
    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_PROGRESS = "job.progress"
    JOB_DONE = "job.done"
    JOB_PARTIAL = "job.partial"
    JOB_FAILED = "job.failed"
    JOB_CANCELLED = "job.cancelled"
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_HEALTH = "system.health"


class Event(Model):
    id: int | None = Field(default=None, description="Store-assigned sequence number")
    ts: UtcDatetime
    job_id: str | None = None
    level: EventLevel
    kind: EventKind
    message: str
    data: dict[str, Any] = Field(default_factory=dict[str, Any])
