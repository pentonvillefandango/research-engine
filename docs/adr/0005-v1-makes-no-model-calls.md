# ADR-0005: V1 makes no model calls

## Status

Accepted (2026-10-04). Source: D5.

## Context

Search, fetch, and extraction of tables, JSON-LD and metadata are deterministic. The calling agent already does the reasoning.

## Decision

V1 contains no LLM or model calls. AI features start in V2. `openai-agents` appears only as a dev dependency, for tests and examples (ADR-0021 context, B6).

## Consequences

V1 is cheap, deterministic and easy to test. Schema-driven extraction, reranking and claims wait for V2. Code review enforces that no model SDK is added to the service package.
