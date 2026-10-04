# ADR-0009: SQLite (WAL) for jobs, events, cache and results in V1

## Status

Accepted (2026-10-04). Source: D9.

## Context

V1 needs durable jobs, an event log and a cache. An extra database service would add operational weight.

## Decision

Use SQLite in WAL mode, via SQLModel and aiosqlite, on a named Docker volume.

## Consequences

No extra service is needed, and backup is a single file (SQLite's online backup API). There's a single-writer limit, which is acceptable at V1 scale. A move to a server database stays possible behind the store modules.
