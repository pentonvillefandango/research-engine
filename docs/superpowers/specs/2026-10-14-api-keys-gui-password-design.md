# Design: GUI password and per-app API keys (v1.1.0)

Date: 2026-10-14. Status: approved by the owner. Feature IDs: **V1-23** (GUI password), **V1-24** (named API keys and usage logging). Decisions: ADR-0027 (GUI password), ADR-0028 (named API keys and request log).

## Why

- The owner wants to sign in to the GUI with a simple password, not the API key.
- One API key is shared by several external apps, so there is no way to tell which app made which call, or to cut off one app without breaking the others.

## V1-23: GUI password

- `research-engine gui set-password` prompts twice (no echo) for a password (minimum 12 characters), hashes it with `hashlib.scrypt` (random 16-byte salt, n=2**14, r=8, p=1), and writes `GUI_PASSWORD_HASH=scrypt:<n>:<r>:<p>:<salt-b64url>:<hash-b64url>` into `.env` (colons, not `$`: Docker Compose would try to expand `$` in `.env` values), replacing any existing line. It keeps the file's mode 600 and prints neither the password nor the hash.
- Config: `Settings.gui_password_hash: SecretStr | None` (optional; empty means unset). A malformed value fails startup with a clear message. Compose passes it as `GUI_PASSWORD_HASH: ${GUI_PASSWORD_HASH:-}`.
- Login page:
  - hash set → one password field; only the password logs in (the API key does not);
  - hash not set → the API-key field, exactly as today.
- Verification is constant-time (`hmac.compare_digest` on the derived hash) and runs via `asyncio.to_thread`.
- Unchanged: the signed 12-hour `re_session` cookie, the Origin check on `POST /login`, and the per-IP login rate limiter.

## V1-24: named API keys

- Format: `re_` + 43 URL-safe characters (`secrets.token_urlsafe(32)`). Shown once at creation.
- Table `api_key`: `id` (int pk), `name` (1–64 chars, unique among active keys), `prefix` (first 8 characters after `re_`, for display), `key_hash` (SHA-256 hex of the full key, unique), `created_at`, `revoked_at` (nullable).
- Plain SHA-256 is enough: the keys carry 256 bits of randomness, so they can't be brute-forced.
- The `.env` `API_KEY` remains valid as the built-in caller **`env`**, shown as "env (built-in)". It can't be revoked from the GUI.
- `KeyStore` (in `research_engine/keys/store.py`):
  - `create(name) -> (row, plaintext)`, `revoke(id)`, `list()`;
  - `match(supplied) -> Caller | None`, a lookup in an in-memory `dict[sha256 digest, Caller]` of active keys, reloaded after every create or revoke and at startup.
  - The built-in env key is checked first with `key_matches`.
- Key management is GUI-only (session cookie). It is not open to an API key, so no REST or MCP route manages keys:
  - `GET /keys`;
  - `POST /keys` (name) → the page shows the new key once;
  - `POST /keys/{id}/revoke`.
  - The `/keys` routes reject requests authenticated by `X-API-Key` with 403.

## V1-24: request log

- Table `api_request`: `id`, `ts`, `caller_kind` (`key` | `env` | `gui`), `key_id` (nullable), `caller_name`, `method`, `path`, `tool` (MCP tool name, nullable), `status`, `duration_ms`, `client_ip`. Index on `(ts)` and `(key_id, ts)`.
- What is logged: every authenticated request to `/v1` or `/mcp`, whether it passed or failed downstream. Unauthenticated 401s are not logged per key; they are already in the app log.
- What is not logged: request bodies, query strings, search terms, URLs fetched.
- Caller: the auth middleware puts a `Caller` into `scope["state"]["caller"]`. A logging wrapper measures status and duration and enqueues one row.
- MCP tool name: the logging middleware reads the request body as it streams through (first 64 KiB only) and takes `params.name` from a JSON-RPC `tools/call`; otherwise `null`. Not a contextvar: the MCP SDK may run the tool in another task's context.
- Writer (`research_engine/keys/usage.py`):
  - an `asyncio.Queue` with a cap of 10,000, flushed in batches every second and at shutdown;
  - when the queue is full, the row is dropped and a counter rises, which is logged as a warning at the next flush;
  - a failed write never fails a request.
- Retention: rows older than 30 days are deleted at startup and then every 24 hours by the same background task.
- Client IP: the same value that login lockout uses (see ADR-0026 for the X-Forwarded-For caveat).

## GUI

- A nav link "Keys" leads to `/keys`, which shows:
  - a create form (name);
  - a table of keys: name, prefix, created, status, requests in the last 24 hours and 30 days, last used, last IP; built-in env first, revoked keys greyed out;
  - a recent requests table (htmx partial `/keys/requests`) filterable by caller and "errors only" (status ≥ 400), 50 per page, newest first.
- The new key appears once in a highlighted box with a copy button and the warning "you won't see this again".
- All values are rendered through the normal autoescaped templates. Key names are user input and are escaped.

## Errors and edge cases

- Duplicate active name → form error, no key created.
- Revoking an already-revoked key or an unknown id → 404 page.
- A key used after revocation → 401 straight away, because the in-memory set is reloaded on revoke. There is a single app process (one uvicorn worker).

## Docs

README config table (`GUI_PASSWORD_HASH`), `.env.example` (commented placeholder), `docs/OPERATIONS.md` (set the password, manage keys, a new section on the request log), `docs/USING.md` (ask the operator for a per-app key), ADR-0027 and ADR-0028, and `schemas/` if any public model changes (none are expected).

## Testing

TDD. Unit tests cover:
- password hashing and verification, the CLI writing `.env` (in a temporary directory), and login in both modes;
- key create, hash-only storage, revoke, and the middleware accepting a DB key, rejecting a revoked one and still accepting the env key;
- `/keys` refusing `X-API-Key` and requiring the Origin check on POST;
- usage rows written with the right caller, the MCP tool name, pruning, and drop on overflow.

`make test` passes, then deploy and a live check: create a key, call `/v1/search` with it, see it on `/keys`, revoke it, get 401.
