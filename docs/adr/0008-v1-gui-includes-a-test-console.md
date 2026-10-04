# ADR-0008: V1 GUI includes a test console that calls the public API

## Status

Accepted (2026-10-04). Source: D8.

## Context

Non-developers should see the service's value without writing code, and the console should double as a smoke test.

## Decision

Add a "Try it" page whose forms call the same public REST endpoints agents use, with no private shortcuts. It includes demos, three result views, a live event trail, copy-as snippets and re-runnable history.

## Consequences

The console exercises the real API path, so it also detects regressions. `make smoke` reuses the demo set.
