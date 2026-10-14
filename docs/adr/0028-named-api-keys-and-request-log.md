# ADR-0028: Named API keys and a request log

## Status

Accepted (2026-10-14). Amends ADR-0021. Source: V1-24.

## Context

One shared API key means the owner cannot tell which app or agent made a call, cannot cut off one caller without breaking the rest, and cannot see how the service is used.

## Decision

- **Named keys.** Keys are created and revoked on the GUI page `/keys`. A key is `re_` plus `secrets.token_urlsafe(32)` and is shown once. Only its SHA-256 hex digest is stored, with the name and an 8-character display prefix. A fast hash is enough because the key is 256 random bits, so guessing or brute force is not a realistic attack, and it keeps the per-request check cheap. Keeping no plaintext means a copy of the database or a backup does not leak usable keys.
- **In-memory match.** Active keys are mirrored in a map of hash to key, updated on create and revoke, so the auth check does no database read. This assumes one uvicorn worker, which is how the service runs. More workers would need a shared source of truth.
- **GUI-only management.** `/keys*` accepts only a GUI session. A request with `X-API-Key` gets 403, even with a valid key, so a leaked key cannot mint or revoke keys. There is no REST or MCP endpoint, no delete and no rename.
- **Built-in env key.** The `.env` `API_KEY` stays valid as the caller `env (built-in)`. It cannot be revoked in the GUI. `make smoke`, `make health` and the ops scripts depend on it, and it is rotated with the existing runbook.
- **Request log.** Every authenticated `/v1` and `/mcp` request writes a row to `api_request`: ts (request start, naive UTC), caller kind, key id, caller name, method, path, MCP tool name (from `tools/call`), status, duration in ms and client IP. Bodies, query strings, search terms and fetched URLs are never stored. GUI-session calls to `/v1` are logged as `GUI session`; GUI page views are not logged. Rows are written in batches about every second, and a logging failure never fails a request.
- **Retention.** Rows older than `REQUEST_LOG_RETENTION_DAYS` (default 30) are deleted by the hourly maintenance loop. The table is in the main SQLite database, so backups include it.

## Consequences

Each app can have its own key, be revoked on its own and be audited. This replaces the "single shared key means no per-user identity" consequence of ADR-0021: calls are now attributed per key, though still not per person. The built-in key is still shared, and the log adds a small write load and some metadata about callers (paths and IPs) to the database and its backups. A lost key must be replaced, not recovered.
