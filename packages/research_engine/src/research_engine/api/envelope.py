"""Envelope helpers and exception handlers: every response is Envelope[T]."""

import time
import uuid
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from research_engine_client.models import Envelope, ErrorCode, ErrorDetail, Meta
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_engine.errors import ServiceError

_log = structlog.get_logger("research_engine.api")


class RequestContextMiddleware:
    """Assigns request_id + start time; echoes X-Request-ID."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        scope["state"]["started"] = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class UnhandledErrorMiddleware:
    """Turns any uncaught exception into a 500 envelope and logs the traceback.

    Starlette runs an ``Exception`` handler inside ``ServerErrorMiddleware``, which sends the
    response and then re-raises (so servers can log it). That puts the handler outside
    ``RequestContextMiddleware`` (no X-Request-ID header) and makes the error propagate to the
    ASGI server/test transport. Catching here, below RequestContext, avoids both.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_tracking)
        except Exception:
            _log.exception("unhandled error", path=scope["path"])
            if started:
                raise
            response = error_response(
                Request(scope),
                500,
                ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR, message="internal error", retryable=False
                ),
            )
            await response(scope, receive, send)


def _meta(request: Request, cache_hit: bool = False) -> Meta:
    started = getattr(request.state, "started", time.perf_counter())
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    return Meta(
        request_id=rid,
        took_ms=max(0, int((time.perf_counter() - started) * 1000)),
        cache_hit=cache_hit,
    )


def ok[T](request: Request, data: T, *, cache_hit: bool = False) -> Envelope[T]:
    return Envelope[T](data=data, meta=_meta(request, cache_hit))


def error_response(request: Request, status: int, *errors: ErrorDetail) -> JSONResponse:
    env = Envelope[None](data=None, meta=_meta(request), errors=list(errors))
    return JSONResponse(status_code=status, content=env.model_dump(mode="json"))


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return error_response(request, exc.http_status, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        errs: list[Any] = list(exc.errors())
        details = [
            ErrorDetail(
                code=ErrorCode.INVALID_REQUEST,
                message=f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}",
                retryable=False,
            )
            for e in errs
        ]
        return error_response(request, 422, *details)
