"""POST /v1/search and GET /v1/engines (V1-01, V1-02, V1-18)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from fastapi.security import APIKeyHeader
from research_engine_client.models import (
    EngineInfo,
    EnginesResponse,
    Envelope,
    SearchIntent,
    SearchRequest,
    SearchResponse,
)

from .deps import Services, get_services
from .envelope import error_responses, ok

# Documentation only (drives the OpenAPI security scheme and the /docs "Authorize" button).
# ApiKeyMiddleware is the enforcer.
_api_key_doc = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service API key")

router = APIRouter(
    prefix="/v1",
    tags=["search"],
    dependencies=[Depends(_api_key_doc)],
    responses=error_responses(401, 404, 422, 500, 502, 504),
)

ENGINES_EXAMPLE = {
    "data": {
        "engines": [{"name": "github", "used_by": ["technical", "code"]}],
        "intents": {
            "technical": {
                "categories": ["general", "it"],
                "engines": ["github", "stackoverflow"],
                "description": "Developer documentation, Q&A and code.",
            }
        },
    },
    "meta": {
        "request_id": "0123456789abcdef0123456789abcdef",
        "schema_version": "1.0.0",
        "took_ms": 1,
        "cache_hit": False,
    },
    "errors": [],
}

SEARCH_EXAMPLE = {
    "query": "compare open-source vector databases",
    "intent": "technical",
    "max_results": 20,
    "depth": "standard",
}


@router.post("/search", response_model=Envelope[SearchResponse])
async def search(
    request: Request,
    req: Annotated[SearchRequest, Body(openapi_examples={"technical": {"value": SEARCH_EXAMPLE}})],
    services: Annotated[Services, Depends(get_services)],
) -> Envelope[SearchResponse]:
    resp, hit = await services.search.search(req)
    return ok(request, resp, cache_hit=hit)


@router.get(
    "/engines",
    response_model=Envelope[EnginesResponse],
    responses={200: {"content": {"application/json": {"example": ENGINES_EXAMPLE}}}},
)
async def engines(
    request: Request, services: Annotated[Services, Depends(get_services)]
) -> Envelope[EnginesResponse]:
    presets = services.intents.all()
    used: dict[str, list[SearchIntent]] = {}
    for intent, preset in presets.items():
        for e in preset.engines:
            used.setdefault(e, []).append(intent)
    data = EnginesResponse(
        engines=[EngineInfo(name=n, used_by=sorted(v)) for n, v in sorted(used.items())],
        intents=presets,
    )
    return ok(request, data)
