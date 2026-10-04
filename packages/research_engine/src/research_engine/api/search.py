"""POST /v1/search and GET /v1/engines (V1-01, V1-02, V1-18)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from research_engine_client.models import (
    EngineInfo,
    EnginesResponse,
    Envelope,
    SearchIntent,
    SearchRequest,
    SearchResponse,
)

from .deps import Services, get_services
from .envelope import ok

router = APIRouter(prefix="/v1", tags=["search"])

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


@router.get("/engines", response_model=Envelope[EnginesResponse])
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
