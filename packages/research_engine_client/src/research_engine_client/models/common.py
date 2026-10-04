"""Envelope, metadata and typed errors shared by every response."""

from enum import StrEnum

from pydantic import Field

from ._base import Model

SCHEMA_VERSION = "1.0.0"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    SSRF_BLOCKED = "ssrf_blocked"
    ROBOTS_DISALLOWED = "robots_disallowed"
    CONTENT_TYPE_NOT_ALLOWED = "content_type_not_allowed"
    RESPONSE_TOO_LARGE = "response_too_large"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_ERROR = "upstream_error"
    ENGINE_FAILED = "engine_failed"
    FETCH_FAILED = "fetch_failed"
    EXTRACTION_FAILED = "extraction_failed"
    JOB_CANCELLED = "job_cancelled"
    JOB_TIMEOUT = "job_timeout"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class ErrorDetail(Model):
    code: ErrorCode
    message: str
    retryable: bool
    source: str | None = Field(default=None, description="URL, engine or component that failed")


class Meta(Model):
    request_id: str
    schema_version: str = SCHEMA_VERSION
    took_ms: int = Field(ge=0)
    cache_hit: bool = False


class Envelope[T](Model):
    data: T | None = None
    meta: Meta
    errors: list[ErrorDetail] = Field(default_factory=list[ErrorDetail])
