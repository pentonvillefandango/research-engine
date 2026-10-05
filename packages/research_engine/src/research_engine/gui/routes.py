"""GUI routes (login, logout, layout) and the GUI security-headers middleware (B2)."""

import hmac
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_engine.api.auth import is_api_path
from research_engine.gui.session import (
    COOKIE,
    MAX_AGE_S,
    LoginRateLimiter,
    SessionCodec,
    safe_next,
    same_origin,
)

GUI_DIR = Path(__file__).parent
templates = Jinja2Templates(
    env=Environment(loader=FileSystemLoader(GUI_DIR / "templates"), autoescape=True)
)
router = APIRouter(include_in_schema=False)

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
)
# FastAPI's Swagger UI / ReDoc pages need inline scripts and a CDN, so they get every header
# except the CSP (they show no untrusted content and run with no credentials beyond the key).
_CSP_EXEMPT = ("/docs", "/redoc")


class SecurityHeadersMiddleware:
    """Adds the CSP and hardening headers to every non-API HTTP response.

    Pure ASGI and header-only: it rewrites ``http.response.start`` and passes every body message
    straight through, so streaming (SSE) responses are never buffered.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_api_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        skip_csp = any(path == p or path.startswith(p + "/") for p in _CSP_EXEMPT)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name in present or (skip_csp and name == b"content-security-policy"):
                        continue
                    headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _client_ip(request: Request) -> str:
    # Behind Caddy, uvicorn --proxy-headers has already put the forwarded client IP here.
    return request.client.host if request.client else "unknown"


def _origin_ok(request: Request) -> bool:
    hosts = request.headers.getlist("host")
    return same_origin(
        request.headers.getlist("origin"),
        request.headers.getlist("referer"),
        hosts[0] if len(hosts) == 1 else "",
        request.app.state.site_host,
    )


def _secure(request: Request) -> bool:
    # Behind Caddy, uvicorn --proxy-headers maps X-Forwarded-Proto into the scheme.
    return request.url.scheme == "https"


def _login_page(request: Request, next_path: str, error: str | None, status: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_path, "error": error},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    return _login_page(request, safe_next(next), None, 200)


@router.post("/login")
async def login(
    request: Request,
    api_key: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    # /login is an open path, so the CSRF Origin check the middleware applies to cookie
    # requests has to happen here (login CSRF / key-guessing from a foreign page).
    if not _origin_ok(request):
        return PlainTextResponse("403 Forbidden: cross-origin request refused\n", status_code=403)
    target = safe_next(next)
    limiter: LoginRateLimiter = request.app.state.login_limiter
    client = _client_ip(request)
    if limiter.blocked(client):
        response = _login_page(request, target, "Too many failed attempts; try again later.", 429)
        response.headers["Retry-After"] = str(limiter.retry_after(client))
        return response
    expected: bytes = request.app.state.api_key
    if not hmac.compare_digest(api_key.encode(), expected):
        limiter.fail(client)
        return _login_page(request, target, "Invalid key", 401)
    limiter.reset(client)
    codec: SessionCodec = request.app.state.session_codec
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        codec.issue(),
        max_age=MAX_AGE_S,
        path="/",
        httponly=True,
        samesite="strict",
        secure=_secure(request),
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    # Not an open path: the auth middleware has already required a session (or key) and run
    # the Origin check for cookie requests.
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        COOKIE, path="/", httponly=True, samesite="strict", secure=_secure(request)
    )
    return response


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    # Placeholder until the dashboard (task 6.2) replaces it.
    return templates.TemplateResponse(request, "base.html", {})
