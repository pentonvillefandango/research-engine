# ADR-0007: GUI is FastAPI + HTMX + server-sent events

## Status

Accepted (2026-10-04). Source: D7.

## Context

The service needs a live activity view and a test console, without a JavaScript build toolchain.

## Decision

Render the GUI server-side with Jinja2. Use htmx 2.x and its SSE extension (vendored and integrity-checked) and FastAPI's native SSE support. A small amount of vanilla JS lives in static files under a strict CSP.

## Consequences

Everything stays in Python, with no npm build. Interactivity is deliberately modest. We stay on htmx 2.x, because htmx 4 changed the SSE extension.
