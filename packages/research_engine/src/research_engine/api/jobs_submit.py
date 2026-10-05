"""Job-submitting endpoints: POST /v1/fetch/batch (V1-07), POST /v1/search_read (V1-08)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request, Response, status
from fastapi.security import APIKeyHeader
from research_engine_client.models import (
    BatchFetchRequest,
    Envelope,
    Job,
    JobType,
    SearchReadRequest,
)

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


SEARCH_READ_EXAMPLE = {
    "search": {"query": "compare open-source vector databases", "intent": "technical"},
    "top_n": 5,
}


@router.post("/search_read", response_model=Envelope[Job], status_code=status.HTTP_202_ACCEPTED)
async def search_read(
    request: Request,
    response: Response,
    req: Annotated[
        SearchReadRequest, Body(openapi_examples={"technical": {"value": SEARCH_READ_EXAMPLE}})
    ],
    services: Annotated[Services, Depends(get_services)],
) -> Envelope[Job]:
    """Search, then read the top N results as a background job.

    Returns the queued job at once (202, with a ``Location`` header); poll
    ``GET /v1/jobs/{id}?wait=N`` for the result. Pages that fail to load are replaced by
    lower-ranked results (at most ``2 x top_n`` pages are tried). Documents are sorted by search
    rank. A search failure fails the job; unreadable pages leave the job ``partial``, possibly
    with no documents.
    """
    job = await services.jobs.submit(JobType.SEARCH_READ, req, session_id=req.session_id)
    response.headers["Location"] = f"/v1/jobs/{job.id}"
    return ok(request, job)
