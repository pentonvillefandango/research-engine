# ADR-0014: AGPL components run as separate containers called over HTTP only

## Status

Accepted (2026-10-04). Source: D14.

## Context

SearXNG is AGPL-3.0, and the project is MIT-licensed.

## Decision

Run SearXNG in its own container. Talk to it only over HTTP, and never import or link its code.

## Consequences

There's a clean licence boundary, and MIT stays compatible with every component used.
