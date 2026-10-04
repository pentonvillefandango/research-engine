"""Health and version models (V1-18, §10)."""

from enum import StrEnum

from pydantic import Field

from ._base import Model
from .common import SEMVER_PATTERN


class DependencyState(StrEnum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"


class DependencyHealth(Model):
    state: DependencyState
    latency_ms: int | None = None
    detail: str | None = None


class VersionInfo(Model):
    version: str
    git_sha: str
    schema_version: str = Field(pattern=SEMVER_PATTERN)


class HealthReport(Model):
    status: DependencyState
    version: str
    git_sha: str
    dependencies: dict[str, DependencyHealth] = Field(default_factory=dict[str, DependencyHealth])
