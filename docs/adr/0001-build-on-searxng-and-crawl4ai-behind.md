# ADR-0001: Build on SearXNG and Crawl4AI behind our own FastAPI service

## Status

Accepted (2026-10-04). Source: D1.

## Context

Research agents need multi-engine web search and reliable page reading. Building these from scratch is a large, ongoing effort, while mature open-source engines already exist.

## Decision

Use SearXNG for metasearch and Crawl4AI for headless-browser fetching. Wrap both behind our own FastAPI service, which owns orchestration, ranking, schemas, events and the GUI. Each engine sits behind an adapter protocol (`SearchProvider`, `Fetcher`).

## Consequences

Infrastructure cost stays at zero, and we control the contracts agents depend on. We have to track two upstream projects' APIs (pinned images, recorded fixtures, integration tests). Either engine can be swapped without touching the rest.
