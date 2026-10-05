"""GET/DELETE /v1/jobs/{id} (V1-09)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.security import APIKeyHeader
from research_engine_client.models import Envelope, ErrorCode, Job, JobDetail

from research_engine.errors import ServiceError

from .deps import Services, get_services
from .envelope import error_responses, ok

_api_key_doc = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service API key")

router = APIRouter(
    prefix="/v1/jobs",
    tags=["jobs"],
    dependencies=[Depends(_api_key_doc)],
    responses=error_responses(401, 404, 422, 500),
)

_ID_CHARS = frozenset("0123456789abcdef")

JOB_EXAMPLE = {
    "data": {
        "job": {
            "id": "0123456789abcdef0123456789abcdef",
            "type": "fetch_batch",
            "status": "done",
            "progress": {"done": 1, "total": 1, "current": None},
            "request": {"urls": ["https://a.example/"]},
            "errors": [],
            "created_at": "2026-10-05T12:00:00Z",
            "started_at": "2026-10-05T12:00:00Z",
            "finished_at": "2026-10-05T12:00:01Z",
        },
        "result": {"kind": "fetch_batch", "documents": [], "failed": []},
    },
    "meta": {
        "request_id": "0123456789abcdef0123456789abcdef",
        "schema_version": "1.0.0",
        "took_ms": 1,
        "cache_hit": False,
    },
    "errors": [],
}


def _not_found(job_id: str) -> ServiceError:
    return ServiceError.of(
        ErrorCode.NOT_FOUND, f"job {job_id[:64]} not found", retryable=False, http_status=404
    )


def valid_job_id(job_id: str) -> bool:
    """Job ids are lowercase hex (<= 64 chars); anything else cannot exist, so skip the DB."""
    return 1 <= len(job_id) <= 64 and _ID_CHARS.issuperset(job_id)


def _check_id(job_id: str) -> None:
    if not valid_job_id(job_id):
        raise _not_found(job_id)


@router.get(
    "/{job_id}",
    response_model=Envelope[JobDetail],
    responses={200: {"content": {"application/json": {"example": JOB_EXAMPLE}}}},
)
async def get_job(
    request: Request,
    job_id: Annotated[str, Path(description="Job id (lowercase hex)")],
    services: Annotated[Services, Depends(get_services)],
    wait: Annotated[
        float, Query(ge=0, le=60, description="Seconds to wait for completion (0-60)")
    ] = 0,
) -> Envelope[JobDetail]:
    """Job status, progress and (once finished) result.

    With ``wait=N`` the call long-polls: it returns as soon as the job is terminal, or after
    ``N`` seconds with the current state.
    """
    _check_id(job_id)
    detail = await (services.jobs.wait(job_id, wait) if wait else services.jobs.get(job_id))
    if detail is None:
        raise _not_found(job_id)
    return ok(request, detail)


@router.delete("/{job_id}", response_model=Envelope[Job])
async def cancel_job(
    request: Request,
    job_id: Annotated[str, Path(description="Job id (lowercase hex)")],
    services: Annotated[Services, Depends(get_services)],
) -> Envelope[Job]:
    """Cancel a queued or running job and return it.

    A job that already finished (done, failed or cancelled) is returned unchanged.
    """
    _check_id(job_id)
    job = await services.jobs.cancel(job_id)
    if job is None:
        raise _not_found(job_id)
    return ok(request, job)
