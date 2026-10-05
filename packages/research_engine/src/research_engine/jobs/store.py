"""Durable job rows <-> public Job/JobDetail models (V1-09).

Timestamps are stored as naive UTC (see ``store/tables.py``); every write goes through
``_naive_utc``/``_now`` and every read re-attaches ``UTC`` in ``to_job``.
"""

import json
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, TypeAdapter
from research_engine_client.models import (
    ErrorCode,
    ErrorDetail,
    Job,
    JobDetail,
    JobProgress,
    JobStatus,
    JobType,
)
from research_engine_client.models.jobs import JobResult
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import JobRow

_RESULT: TypeAdapter[JobResult] = TypeAdapter(JobResult)
_ERRORS = TypeAdapter(list[ErrorDetail])


def _naive_utc(dt: datetime) -> datetime:
    """Normalise to naive UTC for storage; a naive input is taken to be UTC already."""
    return dt if dt.tzinfo is None else dt.astimezone(UTC).replace(tzinfo=None)


def _now() -> datetime:
    return _naive_utc(datetime.now(UTC))


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None else None


def to_job(row: JobRow) -> Job:
    return Job(
        id=row.id,
        type=JobType(row.type),
        status=JobStatus(row.status),
        progress=JobProgress(
            done=row.progress_done, total=row.progress_total, current=row.progress_current
        ),
        parent_id=row.parent_id,
        session_id=row.session_id,
        request=json.loads(row.request_json),
        result_ref=f"/v1/jobs/{row.id}" if row.result_json else None,
        errors=_ERRORS.validate_json(row.errors_json),
        created_at=row.created_at.replace(tzinfo=UTC),
        started_at=_aware(row.started_at),
        finished_at=_aware(row.finished_at),
    )


class JobStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create(self, type_: JobType, request: BaseModel, *, session_id: str | None) -> Job:
        row = JobRow(
            id=uuid.uuid4().hex,
            type=type_.value,
            status=JobStatus.QUEUED.value,
            request_json=request.model_dump_json(),
            session_id=session_id,
            created_at=_now(),
        )
        async with AsyncSession(self._engine, expire_on_commit=False) as s:
            s.add(row)
            await s.commit()
        return to_job(row)

    async def row(self, job_id: str) -> JobRow | None:
        async with AsyncSession(self._engine) as s:
            return await s.get(JobRow, job_id)

    async def detail(self, job_id: str) -> JobDetail | None:
        r = await self.row(job_id)
        if r is None:
            return None
        result = _RESULT.validate_json(r.result_json) if r.result_json else None
        return JobDetail(job=to_job(r), result=result)

    async def _update(self, job_id: str, *where: object, **values: object) -> bool:
        """``UPDATE job SET ... WHERE id=? AND <where>``; True when exactly one row changed."""
        stmt = update(JobRow).where(col(JobRow.id) == job_id, *where).values(**values)  # type: ignore[arg-type]
        async with AsyncSession(self._engine) as s:
            res = await s.exec(stmt)
            await s.commit()
            return (res.rowcount or 0) == 1

    async def claim(self, job_id: str) -> bool:
        """Atomically move ``queued`` -> ``running``; False if someone else got there first."""
        return await self._update(
            job_id,
            col(JobRow.status) == JobStatus.QUEUED.value,
            status=JobStatus.RUNNING.value,
            started_at=_now(),
        )

    async def set_progress(self, job_id: str, done: int, total: int, current: str | None) -> None:
        """Only a running job's progress moves; late calls from an abandoned handler are no-ops."""
        await self._update(
            job_id,
            col(JobRow.status) == JobStatus.RUNNING.value,
            progress_done=done,
            progress_total=total,
            progress_current=current,
        )

    async def finish(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: BaseModel | None,
        errors: list[ErrorDetail],
    ) -> bool:
        """Move a ``running`` job to a terminal ``status``; False if it was not running."""
        return await self._update(
            job_id,
            col(JobRow.status) == JobStatus.RUNNING.value,
            status=status.value,
            finished_at=_now(),
            progress_current=None,
            result_json=result.model_dump_json() if result is not None else None,
            errors_json=_ERRORS.dump_json(errors).decode(),
        )

    async def cancel_if_queued(self, job_id: str) -> bool:
        err = ErrorDetail(
            code=ErrorCode.JOB_CANCELLED, message="cancelled before start", retryable=False
        )
        return await self._update(
            job_id,
            col(JobRow.status) == JobStatus.QUEUED.value,
            status=JobStatus.CANCELLED.value,
            finished_at=_now(),
            errors_json=_ERRORS.dump_json([err]).decode(),
        )

    async def ids_with_status(self, status: JobStatus) -> list[str]:
        stmt = (
            select(JobRow.id)
            .where(JobRow.status == status.value)
            .order_by(col(JobRow.created_at), col(JobRow.id))
        )
        async with AsyncSession(self._engine) as s:
            return list((await s.exec(stmt)).all())

    async def recent(self, limit: int = 50) -> list[Job]:
        """Newest first."""
        stmt = (
            select(JobRow)
            .order_by(col(JobRow.created_at).desc(), col(JobRow.id).desc())
            .limit(max(0, limit))
        )
        async with AsyncSession(self._engine) as s:
            rows = (await s.exec(stmt)).all()
        return [to_job(r) for r in rows]

    async def count_since(self, since: datetime) -> int:
        """Jobs created at or after ``since`` (health strip: jobs today)."""
        stmt = select(func.count()).where(col(JobRow.created_at) >= _naive_utc(since))
        async with AsyncSession(self._engine) as s:
            return int((await s.exec(stmt)).one())

    async def force_status(self, job_id: str, status: JobStatus) -> None:
        """Test helper: set a status without any transition checks."""
        await self._update(job_id, status=status.value)
