# ADR-0004: Model spend via a LiteLLM model selector with per-task routing and a budget cap

## Status

Accepted (2026-10-04). Source: D4.

## Context

From V2, AI features (extraction, query expansion, claim extraction, planning, verification) will call language models. Different tasks suit different models.

## Decision

Route all model calls through one LiteLLM gateway (the model selector). Each task has its own configurable default model, any request can override it, and a monthly budget cap stops calls once it's reached. API keys come from environment variables only.

## Consequences

Spend is visible and capped, and models can be switched without code changes. LiteLLM becomes a V2 dependency. Not applicable in V1 (ADR-0005).
