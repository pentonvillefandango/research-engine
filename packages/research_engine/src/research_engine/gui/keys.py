"""Keys page (V1-24, ADR-0028): create, revoke and watch usage of named API keys.
GUI-session only: a request authenticated by X-API-Key gets 403, so a leaked app key cannot
mint more keys."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from research_engine.api.deps import Services, get_services
from research_engine.gui.routes import templates
from research_engine.keys.models import Caller, CallerKind
from research_engine.keys.store import KeyNameError
from research_engine.keys.usage import caller_id

router = APIRouter(include_in_schema=False)
PAGE = 50
_NO_STORE = {"Cache-Control": "no-store"}
_MAX_ID = 2**63 - 1
_CALLER_FILTER = re.compile(r"env|gui|key:[0-9]{1,9}")


def require_gui(request: Request) -> None:
    caller: Caller | None = getattr(request.state, "caller", None)
    if "x-api-key" in request.headers or caller is None or caller.kind is not CallerKind.GUI:
        raise HTTPException(403, "the Keys page needs a GUI login, not an API key")


async def _page(
    request: Request,
    svc: Services,
    *,
    new_key: str | None = None,
    new_name: str | None = None,
    error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    keys = await svc.keys.list_keys()
    stats = await svc.usage.key_stats()
    rows = await svc.usage.recent(limit=PAGE)
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "active_keys": [k for k in keys if k.revoked_at is None],
            "revoked_keys": [k for k in keys if k.revoked_at is not None],
            "stats": stats,
            "env_stats": stats.get("env"),
            "rows": rows,
            "new_key": new_key,
            "new_name": new_name,
            "error": error,
            "caller": None,
            "errors_only": False,
            "page": PAGE,
            "caller_id": caller_id,
        },
        status_code=status,
        headers=_NO_STORE,
    )


@router.get("/keys", response_class=HTMLResponse, dependencies=[Depends(require_gui)])
async def keys_page(
    request: Request, svc: Annotated[Services, Depends(get_services)]
) -> HTMLResponse:
    return await _page(request, svc)


@router.post("/keys", response_class=HTMLResponse, dependencies=[Depends(require_gui)])
async def create_key(
    request: Request,
    svc: Annotated[Services, Depends(get_services)],
    name: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        row, key = await svc.keys.create(name)
    except KeyNameError as exc:
        return await _page(request, svc, error=str(exc), status=400)
    return await _page(request, svc, new_key=key, new_name=row.name)


@router.post("/keys/{key_id}/revoke", dependencies=[Depends(require_gui)])
async def revoke_key(
    key_id: Annotated[int, Path(le=_MAX_ID)], svc: Annotated[Services, Depends(get_services)]
) -> Response:
    if not await svc.keys.revoke(key_id):
        raise HTTPException(404, "no active key with that id")
    return RedirectResponse("/keys", status_code=303)


@router.get("/keys/requests", response_class=HTMLResponse, dependencies=[Depends(require_gui)])
async def requests_partial(
    request: Request,
    svc: Annotated[Services, Depends(get_services)],
    caller: Annotated[str, Query(max_length=32)] = "",
    errors: Annotated[bool, Query()] = False,
    before: Annotated[int | None, Query(ge=1, le=_MAX_ID)] = None,
) -> HTMLResponse:
    if caller and not _CALLER_FILTER.fullmatch(caller):
        raise HTTPException(400, "caller must be env, gui or key:<id>")
    rows = await svc.usage.recent(
        caller=caller or None, errors_only=errors, before_id=before, limit=PAGE
    )
    return templates.TemplateResponse(
        request,
        "_requests_table.html",
        {"rows": rows, "caller": caller, "errors_only": errors, "page": PAGE},
        headers=_NO_STORE,
    )
