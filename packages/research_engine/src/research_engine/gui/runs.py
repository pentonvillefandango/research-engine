"""Past test-console runs (V1-21): record one, list the newest.

A run stores only what the "Past runs" list and one-click re-run need: the kind, the request
body the console sent, the job id (for search_read), the outcome and the time taken. Never
keys, headers or response bodies. The table is pruned to the newest ``MAX_ROWS`` on every
insert so it cannot grow without bound.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import TestRunRow

__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_ROWS",
    "Run",
    "RunIn",
    "RunKind",
    "RunStatus",
    "TestRunRow",
    "recent",
    "record",
]

MAX_ROWS = 500
MAX_REQUEST_BYTES = 64 * 1024
"""Cap on the stored request JSON (compact, UTF-8)."""
MAX_TOOK_MS = 24 * 3600 * 1000


class RunKind(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"
    SEARCH_READ = "search_read"


class RunStatus(StrEnum):
    """``done``/``failed`` for synchronous calls; a job's terminal status for search_read,
    plus ``timeout`` when the console gave up waiting."""

    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


class RunIn(BaseModel):
    """``POST /gui/runs`` body. ``extra="forbid"``: nothing but these fields is ever stored."""

    model_config = ConfigDict(extra="forbid")

    kind: RunKind
    request: dict[str, Any]
    job_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{1,64}$")
    status: RunStatus
    took_ms: int = Field(ge=0, le=MAX_TOOK_MS)

    @field_validator("request")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(_compact(value).encode()) > MAX_REQUEST_BYTES:
            raise ValueError(f"request JSON is larger than {MAX_REQUEST_BYTES} bytes")
        return value


@dataclass(frozen=True)
class Run:
    id: int
    kind: RunKind
    request: dict[str, Any]
    request_json: str
    job_id: str | None
    status: RunStatus
    took_ms: int
    created_at: datetime

    @property
    def summary(self) -> str:
        """The query or URL, for the list."""
        req = self.request
        if self.kind is RunKind.SEARCH_READ:
            search = req.get("search")
            req = search if isinstance(search, dict) else {}
        value = req.get("url" if self.kind is RunKind.FETCH else "query")
        return value if isinstance(value, str) else ""


def _to_run(row: TestRunRow) -> Run:
    return Run(
        id=row.id or 0,  # always set on a row read back from the database
        kind=RunKind(row.kind),
        request=json.loads(row.request_json),
        request_json=row.request_json,
        job_id=row.job_id,
        status=RunStatus(row.status),
        took_ms=row.took_ms,
        created_at=row.created_at.replace(tzinfo=UTC),
    )


async def record(engine: AsyncEngine, run: RunIn) -> int:
    """Insert one run, prune to the newest ``MAX_ROWS``, return the new id."""
    row = TestRunRow(
        kind=run.kind.value,
        request_json=_compact(run.request),
        job_id=run.job_id,
        status=run.status.value,
        took_ms=run.took_ms,
        created_at=datetime.now(UTC).replace(tzinfo=None),  # naive UTC, like every table
    )
    # The id of the newest row that falls outside the window; NULL (nothing deleted) while
    # there are at most MAX_ROWS rows.
    cutoff = (
        select(TestRunRow.id)
        .order_by(col(TestRunRow.id).desc())
        .offset(MAX_ROWS)
        .limit(1)
        .scalar_subquery()
    )
    async with AsyncSession(engine, expire_on_commit=False) as s:
        s.add(row)
        await s.flush()
        await s.exec(delete(TestRunRow).where(col(TestRunRow.id) <= cutoff))
        await s.commit()
    if row.id is None:  # pragma: no cover - flush always assigns the autoincrement id
        raise RuntimeError("test_run insert returned no id")
    return row.id


async def recent(engine: AsyncEngine, n: int = 20) -> list[Run]:
    """The newest ``n`` runs, newest first."""
    stmt = select(TestRunRow).order_by(col(TestRunRow.id).desc()).limit(max(0, n))
    async with AsyncSession(engine) as s:
        rows = (await s.exec(stmt)).all()
    return [_to_run(r) for r in rows]
