# ADR-0027: GUI password sign-on

## Status

Accepted (2026-10-14). Amends ADR-0021. Source: V1-23.

## Context

The owner wants to sign in to the GUI with a password, not by pasting the API key.

## Decision

An optional scrypt hash, `GUI_PASSWORD_HASH`, lives in `.env` and is written by `research-engine gui set-password`. The hash format is `scrypt:<n>:<r>:<p>:<salt>:<hash>` (base64url, no `$`). When the hash is set, `/login` asks for and accepts only the password; the API key no longer opens the GUI. Without a hash, the key login of ADR-0021 is unchanged. The session cookie, the Origin/Referer check and the login rate limiter are unchanged. The scrypt check runs in a worker thread so it does not block the event loop.

## Consequences

The API key still works for REST and MCP but not for the GUI once a password is set. Losing the password means re-running the CLI and `make deploy`. A single shared password still means no per-user identity.
