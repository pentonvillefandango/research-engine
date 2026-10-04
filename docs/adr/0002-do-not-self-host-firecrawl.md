# ADR-0002: Do not self-host Firecrawl

## Status

Accepted (2026-10-04). Source: D2.

## Context

Self-hosted Firecrawl was considered as an all-in-one alternative.

## Decision

Do not use self-hosted Firecrawl.

## Consequences

It's a heavier stack, and its anti-bot, `/agent` and `/browser` features are cloud-only anyway. We accept owning more orchestration code ourselves (see ADR-0001).
