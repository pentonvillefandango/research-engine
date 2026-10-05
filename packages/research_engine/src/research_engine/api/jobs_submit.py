"""Job-submitting endpoints: POST /v1/fetch/batch (V1-07), POST /v1/search_read (V1-08)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request, Response, status
from fastapi.security import APIKeyHeader
from research_engine_client.models import BatchFetchRequest, Envelope, Job, JobType

from .deps import Services, get_services
from .envelope import error_responses, ok

# Documentation only; ApiKeyMiddleware is the enforcer.
_api_key_doc = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service API key")

router = APIRouter(
    prefix="/v1",
    tags=["jobs"],
    dependencies=[Depends(_api_key_doc)],
    responses=error_responses(401, 422, 500),
)

BATCH_EXAMPLE = {
    "urls": [
        "https://docs.python.org/3/library/asyncio-task.html",
        "https://www.rfc-editor.org/rfc/rfc9110.html",
    ],
    "mode": "auto",
}


@router.post("/fetch/batch", response_model=Envelope[Job], status_code=status.HTTP_202_ACCEPTED)
async def fetch_batch(
    request: Request,
    response: Response,
    req: Annotated[BatchFetchRequest, Body(openapi_examples={"two": {"value": BATCH_EXAMPLE}})],
    services: Annotated[Services, Depends(get_services)],
) -> Envelope[Job]:
    """Fetch up to 50 URLs as a background job.

    Returns the queued job at once (202, with a ``Location`` header); poll
    ``GET /v1/jobs/{id}?wait=N`` for the result. Documents come back in input order; pages that
    fail are listed in ``failed`` and the job ends ``partial``. If every URL fails the job ends
    ``failed``.
    """
    job = await services.jobs.submit(JobType.FETCH_BATCH, req, session_id=req.session_id)
    response.headers["Location"] = f"/v1/jobs/{job.id}"
    return ok(request, job)
