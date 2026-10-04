# ADR-0023: httpx in our code; httpx2 only inside the MCP SDK

## Status

Accepted (2026-10-04). Source: checked 2026-10-04.

## Context

MCP Python SDK 2.x and `openai-agents` depend on `httpx2`, a renamed pydantic-maintained fork of httpx. `respx`, our HTTP mocking library, supports only `httpx`.

## Decision

Our service and client code use `httpx` 0.28.x for every outbound call, mocked with respx in unit tests. `httpx2` is allowed only as a transitive dependency of the MCP SDK and `openai-agents`. MCP is tested end to end against a real local server, not with respx.

## Consequences

Two HTTP libraries are installed, and we revisit this when respx supports httpx2 or httpx is retired.
