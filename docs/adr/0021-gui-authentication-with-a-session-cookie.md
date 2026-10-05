# ADR-0021: GUI authentication with a session cookie

## Status

Accepted (2026-10-04). Source: B2.

## Context

REST and MCP require an API key header. The GUI's test console must call the same public REST endpoints from a browser, so the browser needs credentials, without exposing the API key to page JavaScript.

## Decision

A login page asks for the API key once and sets a signed (itsdangerous), HttpOnly, `SameSite=Strict` cookie with a 12-hour lifetime. On `/v1` the auth middleware accepts either `X-API-Key` or that cookie. `/mcp` (and `/mcp/`) is key-only: the session cookie is not accepted there, because MCP clients are agents that hold the key, and the browser has no use for it. `/mcp` also serves only POST (stateless mode; GET and DELETE are 405). Cookie-authenticated state-changing requests must also pass an Origin/Referer same-host check. Login attempts are rate-limited per client. Caddy additionally restricts the site to `LAB_SUBNET`.

## Consequences

The key never lives in browser storage or JavaScript, and copy-as snippets use `$RESEARCH_ENGINE_API_KEY` placeholders. A single shared key means there's no per-user identity; that's acceptable for the lab and can be revisited later.
