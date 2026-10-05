"""GET /health, GET /version, GET /v1/schemas (V1-14, V1-18, §4, §10)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.security import APIKeyHeader
from research_engine_client.models import (
    ALL_MODELS,
    SCHEMA_VERSION,
    DependencyHealth,
    DependencyState,
    Envelope,
    ErrorCode,
    HealthReport,
    VersionInfo,
)
from research_engine_client.schemas import render_schemas
from sqlalchemy import text

from research_engine import __version__
from research_engine.errors import ServiceError

from .deps import Services, get_services
from .envelope import error_responses, ok

# No auth: Docker and the GUI poll these.
router = APIRouter(tags=["ops"])

_api_key_doc = APIKeyHeader(name="X-API-Key", auto_error=False, description="Service API key")
schemas_router = APIRouter(
    prefix="/v1/schemas",
    tags=["schemas"],
    dependencies=[Depends(_api_key_doc)],
    responses=error_responses(401, 404, 422, 500),
)

# Rendered once at import; the models do not change at runtime.
_SCHEMAS: dict[str, str] = render_schemas()

_DEPENDENCY_STATE_KEY = "health_state"


class _HealthCache:
    """Last dependency results plus a single-flight lock: concurrent callers share one run."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.checked_at: float | None = None
        self.deps: dict[str, DependencyHealth] = {}


async def _timed(check: Callable[[], Awaitable[bool]], timeout_s: float) -> DependencyHealth:
    t0 = time.perf_counter()
    try:
        up = await asyncio.wait_for(check(), timeout_s)
    except TimeoutError:
        return DependencyHealth(
            state=DependencyState.DOWN, latency_ms=int(timeout_s * 1000), detail="timeout"
        )
    except Exception as exc:
        # Health must never raise. Only the type name is reported: the message may hold
        # internal hostnames and this endpoint is unauthenticated.
        return DependencyHealth(state=DependencyState.DOWN, detail=type(exc).__name__)
    return DependencyHealth(
        state=DependencyState.UP if up else DependencyState.DOWN,
        latency_ms=int((time.perf_counter() - t0) * 1000),
    )


async def _cache_health(services: Services) -> DependencyHealth:
    """Cache detail, under the same timeout. A cache problem degrades ``cache`` only; it
    never marks the database down. Only ``timeout`` / ``unavailable`` is reported."""

    async def detail() -> str:
        entries, _ = await services.cache.size()
        return f"hit_rate={services.cache.stats().hit_rate:.2f} entries={entries}"

    try:
        text_ = await asyncio.wait_for(detail(), services.health_timeout_s)
    except TimeoutError:
        return DependencyHealth(state=DependencyState.DEGRADED, detail="timeout")
    except Exception:
        return DependencyHealth(state=DependencyState.DEGRADED, detail="unavailable")
    return DependencyHealth(state=DependencyState.UP, detail=text_)


async def _check_dependencies(services: Services) -> dict[str, DependencyHealth]:
    async def db() -> bool:
        async with services.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    checks = {**services.health_checks, "database": db}
    results = await asyncio.gather(*(_timed(c, services.health_timeout_s) for c in checks.values()))
    deps = dict(zip(checks, results, strict=True))
    deps["cache"] = await _cache_health(services)
    return deps


async def _dependencies(services: Services) -> dict[str, DependencyHealth]:
    state = services.extra.setdefault(_DEPENDENCY_STATE_KEY, _HealthCache())
    async with state.lock:
        now = time.monotonic()
        if state.checked_at is None or now - state.checked_at >= services.health_ttl_s:
            state.deps = await _check_dependencies(services)
            state.checked_at = time.monotonic()
        return state.deps


def _overall(deps: dict[str, DependencyHealth]) -> DependencyState:
    if deps["database"].state is DependencyState.DOWN:
        return DependencyState.DOWN
    if any(deps[k].state is DependencyState.DOWN for k in ("searxng", "crawl4ai") if k in deps):
        return DependencyState.DEGRADED
    return DependencyState.UP


@router.get(
    "/health",
    response_model=Envelope[HealthReport],
    responses={503: {"model": Envelope[HealthReport], "description": "Database is down."}},
)
async def health(
    request: Request, response: Response, services: Annotated[Services, Depends(get_services)]
) -> Envelope[HealthReport]:
    """Dependency states and overall status; HTTP 503 only when the database is down.

    Results (database included) are reused for up to 5 s so polling cannot amplify load on
    dependencies; a dependency change can therefore show up to 5 s late.
    """
    deps = await _dependencies(services)
    overall = _overall(deps)
    if overall is DependencyState.DOWN:
        response.status_code = 503
    return ok(
        request,
        HealthReport(
            status=overall,
            version=__version__,
            git_sha=services.settings.git_sha,
            dependencies=deps,
        ),
    )


@router.get("/version", response_model=Envelope[VersionInfo])
async def version(
    request: Request, services: Annotated[Services, Depends(get_services)]
) -> Envelope[VersionInfo]:
    """Service version, git SHA and the response schema version."""
    return ok(
        request,
        VersionInfo(
            version=__version__, git_sha=services.settings.git_sha, schema_version=SCHEMA_VERSION
        ),
    )


@schemas_router.get("", response_model=Envelope[list[str]])
async def list_schemas(request: Request) -> Envelope[list[str]]:
    """Names of all published JSON Schemas."""
    return ok(request, sorted(_SCHEMAS))


@schemas_router.get(
    "/{name}",
    response_class=Response,
    responses={
        200: {
            "description": "The raw JSON Schema (not an envelope).",
            "content": {"application/schema+json": {"schema": {"type": "object"}}},
        }
    },
)
async def get_schema(name: Annotated[str, Path(description="Model name")]) -> Response:
    """The raw JSON Schema for one model, as ``application/schema+json`` (no envelope)."""
    if name not in ALL_MODELS or name not in _SCHEMAS:
        raise ServiceError.of(
            ErrorCode.NOT_FOUND, "no such schema", retryable=False, http_status=404
        )
    return Response(content=_SCHEMAS[name], media_type="application/schema+json")
