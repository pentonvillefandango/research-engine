# ADR-0021: GUI authentication with a session cookie

## Status

Accepted (2026-10-04). Source: B2.

## Context

REST and MCP require an API key header. The GUI's test console must call the same public REST endpoints from a browser, so the browser needs credentials, without exposing the API key to page JavaScript.

## Decision

A login page asks for the API key once and sets a signed (itsdangerous), HttpOnly, `SameSite=Strict` cookie with a 12-hour lifetime. The auth middleware accepts either `X-API-Key` or that cookie. Cookie-authenticated state-changing requests must also pass an Origin/Referer same-host check. Login attempts are rate-limited per client. Caddy additionally restricts the site to `LAB_SUBNET`.

## Consequences

The key never lives in browser storage or JavaScript, and copy-as snippets use `$RESEARCH_ENGINE_API_KEY` placeholders. A single shared key means there's no per-user identity; that's acceptable for the lab and can be revisited later.
