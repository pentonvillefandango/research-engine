# ADR-0006: uv-based Python 3.12+ workspace with src layout

## Status

Accepted (2026-10-04). Source: D6.

## Context

The project needs reproducible builds and a client package that agents can import separately from the service.

## Decision

Use a uv workspace with two members: `research_engine` (the service) and `research_engine_client` (the shared Pydantic models and async client). Python ≥3.12, `src/` layout, and `uv.lock` committed. The runtime image uses Python 3.13 (design addendum B4).

## Consequences

Locked, reproducible environments in development, CI and Docker (`uv sync --locked`). Agents depend only on the light client package (pydantic and httpx).
