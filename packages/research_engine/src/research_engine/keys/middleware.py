"""Logs one row per authenticated /v1 or /mcp request (V1-24). Wraps ApiKeyMiddleware and reads
the ``Caller`` it put in ``scope["state"]``. For POST /mcp it peeks at the JSON-RPC body as it
streams through (first 64 KiB, never stored) to name the tool of a ``tools/call``."""

import contextlib
import json
import time
from datetime import UTC, datetime
from typing import Any, cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_engine.api.auth import is_api_path, is_key_only_path
from research_engine.keys.models import Caller
from research_engine.keys.usage import RequestRecord

SNIFF_LIMIT = 64 * 1024
MAX_TOOL = 64
MAX_PATH = 256


def _tool(body: bytes) -> str | None:
    """The tool name of a single JSON-RPC ``tools/call``; ``None`` for anything else."""
    try:
        msg: object = json.loads(body)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    m = cast(dict[str, Any], msg)
    if m.get("method") != "tools/call":
        return None
    params: object = m.get("params")
    name: object = cast(dict[str, Any], params).get("name") if isinstance(params, dict) else None
    return name[:MAX_TOOL] if isinstance(name, str) else None


class UsageMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_api_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        body = bytearray()
        sniff = is_key_only_path(scope["path"]) and scope["method"] == "POST"

        async def receive_wrapper() -> Message:
            message = await receive()
            if message["type"] == "http.request" and len(body) < SNIFF_LIMIT:
                chunk: bytes = message.get("body", b"")
                body.extend(chunk[: SNIFF_LIMIT - len(body)])
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_wrapper if sniff else receive, send_wrapper)
        finally:
            # Usage logging must never fail (or delay) a request: record() only queues.
            with contextlib.suppress(Exception):
                self._record(scope, status, started, _tool(bytes(body)) if sniff else None)

    @staticmethod
    def _record(scope: Scope, status: int, started: float, tool: str | None) -> None:
        caller: Caller | None = scope.get("state", {}).get("caller")
        if caller is None:  # unauthenticated: auth answered 401/403 itself
            return
        client = scope.get("client")
        scope["app"].state.services.usage.record(
            RequestRecord(
                ts=datetime.now(UTC),
                caller_kind=caller.kind.value,
                key_id=caller.key_id,
                caller_name=caller.name,
                method=scope["method"],
                path=scope["path"][:MAX_PATH],
                tool=tool,
                status=status,
                duration_ms=int((time.perf_counter() - started) * 1000),
                client_ip=client[0] if client else "unknown",
            )
        )
