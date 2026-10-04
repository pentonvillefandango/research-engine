# ADR-0011: Deploy as one Docker Compose project

## Status

Accepted (2026-10-04). Source: D11.

## Context

Several services (app, SearXNG, Crawl4AI, plus optional extras later) must be deployed and managed as one unit.

## Decision

Run one Compose project named `research-engine`. Optional services sit behind Compose profiles.

## Consequences

There's a single lifecycle for up, down and pull. Profiles keep optional services off unless they're enabled.
