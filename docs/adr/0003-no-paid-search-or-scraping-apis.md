# ADR-0003: No paid search or scraping APIs

## Status

Accepted (2026-10-04). Source: D3.

## Context

Paid search and scraping APIs are convenient, but they add recurring cost and vendor lock-in.

## Decision

Never use paid search or scraping APIs. Search, fetching and storage use only self-hosted open-source components.

## Consequences

Infrastructure cost is zero. Result quality depends on SearXNG's public engines, which can rate-limit or block; this is mitigated by multi-engine merging, the broader-preset retry and resilience events (V1-03). The only optional paid exception is a VPN provider for V2 egress (ADR-0010).
