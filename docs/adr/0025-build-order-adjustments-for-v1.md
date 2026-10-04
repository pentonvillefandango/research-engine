# ADR-0025: Build-order adjustments for V1

## Status

Accepted (2026-10-04). Source: design addendum §4.

## Context

REQUIREMENTS §11 builds search (step 2) and fetch (step 3) before the SQLite store, jobs, cache and events (step 4). But search and fetch must already emit events and report cache hits, and their tests should use real upstream response shapes.

## Decision

Step 2 introduces the `EventSink` and `Cache` protocols with in-memory implementations; step 4 swaps in SQLite versions behind the same interfaces. Step 2 also adds a minimal Compose file (SearXNG and Crawl4AI only) to record real fixtures and run integration tests, and step 8 completes it.

## Consequences

No rework of steps 2–3 in step 4. Fixtures reflect the 2026 APIs. The step numbers are unchanged.
