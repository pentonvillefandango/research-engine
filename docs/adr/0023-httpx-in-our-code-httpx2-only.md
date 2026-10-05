# ADR-0023: httpx in our code; httpx2 only inside the MCP SDK

## Status

Accepted (2026-10-04). Source: checked 2026-10-04.

## Context

MCP Python SDK 2.x and `openai-agents` depend on `httpx2`, a renamed pydantic-maintained fork of httpx. `respx`, our HTTP mocking library, supports only `httpx`.

## Decision

Our service and client code use `httpx` 0.28.x for every outbound call, mocked with respx in unit tests. `httpx2` is allowed only as a transitive dependency of the MCP SDK and `openai-agents`. MCP is tested end to end against a real local server, not with respx.

## Consequences

Two HTTP libraries are installed, and we revisit this when respx supports httpx2 or httpx is retired.

## Dependency notes (accepted by the owner, step 7.2)

- **websockets is held at 16.1.1.** The dev dependency `openai-agents` (used only by `examples/` and its tests) caps `websockets`, and uv resolves one version for the whole workspace, so the lock went from 17.2 to 16.1.1 and the runtime image gets 16.1.1 too (via `uvicorn[standard]`). The service never serves or opens a WebSocket (the auth middleware refuses WebSocket scopes), so this has no runtime effect. Dependabot will surface the update when the cap is lifted.
- **Extra MCP runtime dependencies.** The MCP SDK brings these into the runtime image: `httpx2` and `httpcore2` (its HTTP stack), `pyjwt[crypto]` with `cryptography` and `cffi` (its OAuth support, unused here), `truststore` (via `httpx2`), and `sse-starlette` (its streamable HTTP transport). `httpx2-jsfetch` is in the lock but installed only on Emscripten (`sys_platform == 'emscripten'`), so not in our image. None of them is called by our code; our outbound HTTP stays on `httpx`.
