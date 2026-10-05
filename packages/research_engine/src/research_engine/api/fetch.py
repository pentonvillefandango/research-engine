"""POST /v1/fetch (V1-04, V1-05, V1-06)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from fastapi.security import APIKeyHeader
from research_engine_client.models import Document, Envelope, FetchRequest

from .deps import Services, get_services
from .envelope import error_responses, ok

# Documentation only; ApiKeyMiddleware is the enforcer.
_api_key_doc = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service API key")

router = APIRouter(
    prefix="/v1",
    tags=["fetch"],
    dependencies=[Depends(_api_key_doc)],
    responses=error_responses(401, 403, 404, 413, 415, 422, 500, 502, 504),
)

FETCH_EXAMPLE = {"url": "https://docs.python.org/3/library/asyncio-task.html", "mode": "auto"}


@router.post("/fetch", response_model=Envelope[Document])
async def fetch(
    request: Request,
    req: Annotated[FetchRequest, Body(openapi_examples={"article": {"value": FETCH_EXAMPLE}})],
    services: Annotated[Services, Depends(get_services)],
) -> Envelope[Document]:
    """Fetch one URL as a clean Document: static first, escalating to the sandboxed browser."""
    doc, hit = await services.fetch.fetch(req)
    return ok(request, doc, cache_hit=hit)
