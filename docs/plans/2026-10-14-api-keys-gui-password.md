# GUI password and per-app API keys: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** an optional password for GUI sign-on, plus named, revocable API keys whose every `/v1` and `/mcp` request is logged and shown on a new GUI page, `/keys`.

**Architecture:** a new package `research_engine.keys` holds four modules:
- `password.py`: scrypt hashing;
- `store.py`: `KeyStore`, with SQLite rows and an in-memory hash map of active keys;
- `usage.py`: `UsageLog`, a batched request-log writer with stats queries;
- `middleware.py`: `UsageMiddleware`.

`ApiKeyMiddleware` asks `KeyStore.match()` for each request and puts a `Caller` into `scope["state"]["caller"]`. `UsageMiddleware` wraps the auth middleware and records one row per authenticated API request. The GUI page is in a new router module, `gui/keys.py`.

**Tech Stack:** Python 3.13, FastAPI/Starlette (pure ASGI middleware), SQLModel on aiosqlite, Jinja2 and htmx, `hashlib.scrypt`, pytest with pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-14-api-keys-gui-password-design.md`

## Global Constraints

- Feature IDs go in every commit: V1-23 (password), V1-24 (keys and logging). Use conventional commit prefixes and end each message with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- TDD: write the failing test first and watch it fail before writing code.
- `make test` (ruff check, ruff format --check, pyright strict and pytest) must pass at the end of every task.
- Never store or log plaintext keys or passwords. Keys are stored as SHA-256 hex; the password as `scrypt:<n>:<r>:<p>:<salt>:<hash>` (base64url, no padding). Use no `$` in that format.
- Hash format: `scrypt:16384:8:1:<22-char salt b64url>:<43-char hash b64url>`, with a 16-byte salt and a 32-byte derived key.
- Key format: `"re_" + secrets.token_urlsafe(32)`. The display prefix is the 8 characters after `re_`.
- The `.env` `API_KEY` stays valid as caller kind `env` (name `env (built-in)`) and can't be revoked in the GUI.
- `/keys*` routes accept only a GUI session caller. A request carrying `X-API-Key` gets 403, even with a valid key.
- The request log never stores bodies, query strings, search terms or fetched URLs. Only these fields are stored: ts, caller_kind, key_id, caller_name, method, path, tool, status, duration_ms, client_ip.
- Request-log retention is 30 days (`REQUEST_LOG_RETENTION_DAYS`, default 30).
- A failure in usage logging must never fail a request.
- Store timestamps as naive UTC, as `jobs/store.py` does (`_naive_utc` / `_now`).
- Docs move with code (`tests/test_docs.py`): `.env.example` keys go in the README config table, and new Settings fields go in `compose.yaml` (`tests/test_compose_policy.py`).
- Co-tenancy: never run `docker compose up` against project `research-engine`; use `make deploy`.

**User decisions (already made):**
- Password: set by the CLI `research-engine gui set-password`, which writes only a scrypt hash, `GUI_PASSWORD_HASH`, to `.env`.
- When a password hash is set, the login page accepts only the password. When none is set, it accepts the `.env` API key as today.
- The existing `.env` `API_KEY` is kept as a built-in key ("env (built-in)") with its own usage, and it can't be revoked in the GUI.
- Every authenticated request is logged and kept 30 days. The Keys page shows per-key totals, last used and a filterable recent-requests table.
- Keys are managed from the GUI only, with no REST or MCP endpoint. There's no delete or rename in this version.

---

## File map

| File | Responsibility |
|---|---|
| `packages/research_engine/src/research_engine/keys/__init__.py` | package marker |
| `keys/password.py` | `hash_password`, `verify_password`, `check_format` |
| `keys/models.py` | `CallerKind`, `Caller` |
| `keys/store.py` | `KeyStore`, `KeyNameError` |
| `keys/usage.py` | `RequestRecord`, `UsageLog`, `KeyStats` |
| `keys/middleware.py` | `UsageMiddleware` (also sniffs the MCP tool name) |
| `keys/envfile.py` | `set_env_value(path, name, value)`, an atomic `.env` line update |
| `store/tables.py` | add `ApiKeyRow`, `ApiRequestRow` |
| `config.py` | `gui_password_hash`, `request_log_retention_days` |
| `api/deps.py` | `Services.keys`, `Services.usage` |
| `app.py`, `testing.py` | wire `KeyStore`/`UsageLog`; middleware order; start and stop |
| `api/auth.py` | match through `KeyStore`; set `scope["state"]["caller"]` |
| `maintenance.py` | prune the request log |
| `cli.py` | `gui set-password` |
| `gui/routes.py`, `templates/login.html` | password login |
| `gui/keys.py`, `templates/keys.html`, `templates/_requests_table.html`, `static/keys.js`, `templates/base.html` | the Keys page |
| `compose.yaml`, `.env.example`, `README.md`, `docs/OPERATIONS.md`, `docs/USING.md`, `docs/adr/0027-*.md`, `docs/adr/0028-*.md` | docs and config |

All paths below are relative to `packages/research_engine/src/research_engine/` unless they start with `tests/`, `docs/` or a repo-root file.

---

### Task 1: Password hashing and config (V1-23)

**Goal:** a scrypt password hash format plus the `GUI_PASSWORD_HASH` setting, validated at startup and wired through Compose and the docs.

**Files:**
- Create: `keys/__init__.py`, `keys/password.py`
- Modify: `config.py`, `compose.yaml`, `.env.example`, `README.md` (config table)
- Test: `tests/service/unit/test_password.py`, `tests/service/unit/test_config.py`

**Acceptance Criteria:**
- [ ] `hash_password("correct horse battery")` returns a string matching `^scrypt:16384:8:1:[A-Za-z0-9_-]{22}:[A-Za-z0-9_-]{43}$`, and two calls give different salts.
- [ ] `verify_password` returns True for the right password and False for a wrong one, a malformed hash or an empty password.
- [ ] `Settings(GUI_PASSWORD_HASH="")` leaves `gui_password_hash` as None. A malformed value raises a ValidationError naming `GUI_PASSWORD_HASH` without echoing the value.
- [ ] `compose.yaml` maps `GUI_PASSWORD_HASH: ${GUI_PASSWORD_HASH:-}` and `REQUEST_LOG_RETENTION_DAYS: ${REQUEST_LOG_RETENTION_DAYS:-30}`. `.env.example` has a commented `# GUI_PASSWORD_HASH=` line and `REQUEST_LOG_RETENTION_DAYS=30`. The README config table lists both.

**Verify:** `uv run pytest tests/service/unit/test_password.py tests/service/unit/test_config.py tests/test_compose_policy.py tests/test_docs.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write failing tests** in `tests/service/unit/test_password.py`:

```python
import re

import pytest
from research_engine.keys.password import check_format, hash_password, verify_password

PATTERN = re.compile(r"^scrypt:16384:8:1:[A-Za-z0-9_-]{22}:[A-Za-z0-9_-]{43}$")


def test_hash_format_and_random_salt() -> None:
    a, b = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert PATTERN.match(a) and PATTERN.match(b)
    assert a != b


def test_verify() -> None:
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong horse battery", h)
    assert not verify_password("", h)
    assert not verify_password("correct horse battery", "scrypt:1:2")
    assert not verify_password("correct horse battery", "")


@pytest.mark.parametrize("bad", ["x", "scrypt:16384:8:1:abc", "bcrypt:1:1:1:a:b", "scrypt:a:8:1:x:y"])
def test_check_format_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match="GUI_PASSWORD_HASH"):
        check_format(bad)
```

In `tests/service/unit/test_config.py`, add tests following that file's existing pattern (it builds `Settings` from env through `settings_env`/`monkeypatch`):

```python
def test_gui_password_hash_empty_is_none(settings_env, monkeypatch) -> None:
    from research_engine.config import Settings
    monkeypatch.setenv("GUI_PASSWORD_HASH", "")
    assert Settings().gui_password_hash is None  # type: ignore[call-arg]


def test_gui_password_hash_valid(settings_env, monkeypatch) -> None:
    from research_engine.config import Settings
    from research_engine.keys.password import hash_password
    h = hash_password("correct horse battery")
    monkeypatch.setenv("GUI_PASSWORD_HASH", h)
    s = Settings()  # type: ignore[call-arg]
    assert s.gui_password_hash is not None and s.gui_password_hash.get_secret_value() == h


def test_gui_password_hash_malformed(settings_env, monkeypatch) -> None:
    from pydantic import ValidationError
    from research_engine.config import Settings
    monkeypatch.setenv("GUI_PASSWORD_HASH", "plaintext-oops")
    with pytest.raises(ValidationError) as e:
        Settings()  # type: ignore[call-arg]
    assert "GUI_PASSWORD_HASH" in str(e.value) and "plaintext-oops" not in str(e.value)
```

- [ ] **Step 2: Run them and watch them fail.** Run `uv run pytest tests/service/unit/test_password.py tests/service/unit/test_config.py -q`. Expected: ImportError or AttributeError.

- [ ] **Step 3: Implement `keys/password.py`**

```python
"""GUI password hashing (V1-23, ADR-0027): scrypt, stored as
``scrypt:<n>:<r>:<p>:<salt b64url>:<hash b64url>`` (no ``$``: Compose would interpolate it)."""

import base64
import hashlib
import hmac
import secrets

N, R, P = 2**14, 8, 1
SALT_BYTES = 16
DKLEN = 32
MIN_LENGTH = 12
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=_MAXMEM)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    return f"scrypt:{N}:{R}:{P}:{_b64(salt)}:{_b64(_derive(password, salt, N, R, P))}"


def check_format(stored: str) -> tuple[int, int, int, bytes, bytes]:
    """Parse ``stored``; ValueError (naming GUI_PASSWORD_HASH, never the value) if malformed."""
    err = ValueError(
        "GUI_PASSWORD_HASH is not a valid hash; set it with: research-engine gui set-password"
    )
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise err
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except ValueError as exc:
        raise err from exc
    if n < 2 or n & (n - 1) or r < 1 or p < 1 or len(salt) < 8 or len(digest) != DKLEN:
        raise err
    return n, r, p, salt, digest


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check; False for an empty password or a malformed hash. CPU-bound:
    callers on the event loop use ``asyncio.to_thread``."""
    if not password:
        return False
    try:
        n, r, p, salt, digest = check_format(stored)
    except ValueError:
        return False
    return hmac.compare_digest(_derive(password, salt, n, r, p), digest)
```

`keys/__init__.py` holds one docstring line: `"""Named API keys, request log and GUI password (V1-23, V1-24)."""`

- [ ] **Step 4: Implement config.** In `config.py`, under `# core / identity`, add:

```python
    gui_password_hash: SecretStr | None = None
```

Under `# store / jobs / events`, add:

```python
    request_log_retention_days: int = Field(default=30, ge=1)
```

Add this validator to `Settings`:

```python
    @field_validator("gui_password_hash", mode="before")
    @classmethod
    def _password_hash(cls, v: object) -> object:
        """Empty means no password (key login); anything else must be a well-formed hash."""
        raw = v.get_secret_value() if isinstance(v, SecretStr) else v
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if not isinstance(raw, str):
            raise ValueError("GUI_PASSWORD_HASH must be a string")
        check_format(raw.strip())
        return raw.strip()
```

Import `from research_engine.keys.password import check_format`. `keys/password.py` imports nothing from `research_engine`, so this creates no import cycle.

- [ ] **Step 5: Update Compose, `.env.example` and the README.**
  - In `compose.yaml`, under the app's `# core / identity` add `GUI_PASSWORD_HASH: ${GUI_PASSWORD_HASH:-}`, and under the store/events block add `REQUEST_LOG_RETENTION_DAYS: ${REQUEST_LOG_RETENTION_DAYS:-30}`.
  - In `.env.example`, add a commented block:

    ```
    # Optional GUI password (V1-23). Set with: uv run research-engine gui set-password
    # GUI_PASSWORD_HASH=
    REQUEST_LOG_RETENTION_DAYS=30
    ```

    Then check how `test_readme_config_table_lists_every_env_example_key` parses keys: does it count commented `# KEY=` lines? If it does, list the key; if it doesn't, still list `GUI_PASSWORD_HASH` in the README table.
  - In the README config table, add two rows:
    - `GUI_PASSWORD_HASH`: optional scrypt hash of the GUI password, set by `research-engine gui set-password`; when empty, the GUI login asks for `API_KEY`;
    - `REQUEST_LOG_RETENTION_DAYS`: days of per-request API usage to keep (default 30).

- [ ] **Step 6: Run the Verify command** and `uv run pyright`. Everything should pass.

- [ ] **Step 7: Commit:** `feat(auth): scrypt GUI password hash setting (V1-23)`

---

### Task 2: `research-engine gui set-password` CLI (V1-23)

**Goal:** a CLI command that prompts for a password twice and writes its hash into `.env` atomically, keeping the file's mode.

**Files:**
- Create: `keys/envfile.py`
- Modify: `cli.py`
- Test: `tests/service/unit/test_set_password_cli.py`

**Acceptance Criteria:**
- [ ] `set_env_value(path, "GUI_PASSWORD_HASH", h)` replaces an existing `GUI_PASSWORD_HASH=` line, or a commented `# GUI_PASSWORD_HASH=` line, in place; otherwise it appends. Other lines are byte-for-byte unchanged, and the file mode (0600) is preserved.
- [ ] `research-engine gui set-password --env-file P`:
  - mismatched entries → exit 2, file unchanged;
  - password shorter than 12 → exit 2, file unchanged;
  - success → exit 0, prints one JSON line `{"ok": true, "command": "gui set-password", "env_file": "...", "next": "make deploy"}`, never the password or hash; the stored hash verifies against the password.
- [ ] A missing env file → exit 2 with `"ok": false`.

**Verify:** `uv run pytest tests/service/unit/test_set_password_cli.py -q` → all pass

**Steps:**

- [ ] **Step 1: Failing tests.** Read `cli.py` first to see how `main(argv)` emits JSON lines, and reuse that pattern. The tests inject the prompt through a module attribute, `research_engine.cli.getpass`, so they patch it:

```python
import json
import os
import stat
from pathlib import Path

import pytest
from research_engine import cli
from research_engine.keys.envfile import set_env_value
from research_engine.keys.password import verify_password


def _env(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(body)
    p.chmod(0o600)
    return p


def test_set_env_value_replaces_and_keeps_mode(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\nGUI_PASSWORD_HASH=old\nB=2\n")
    set_env_value(p, "GUI_PASSWORD_HASH", "new")
    assert p.read_text() == "A=1\nGUI_PASSWORD_HASH=new\nB=2\n"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_set_env_value_uncomments_or_appends(tmp_path: Path) -> None:
    p = _env(tmp_path, "A=1\n# GUI_PASSWORD_HASH=\n")
    set_env_value(p, "GUI_PASSWORD_HASH", "h")
    assert p.read_text() == "A=1\nGUI_PASSWORD_HASH=h\n"
    q = _env(tmp_path, "A=1")  # no trailing newline
    set_env_value(q, "GUI_PASSWORD_HASH", "h")
    assert q.read_text() == "A=1\nGUI_PASSWORD_HASH=h\n"


def _prompts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    it = iter(answers)
    monkeypatch.setattr(cli, "getpass", lambda prompt="": next(it))


def test_cli_success(tmp_path, monkeypatch, capsys) -> None:
    p = _env(tmp_path, "API_KEY=x\n")
    _prompts(monkeypatch, "correct horse battery", "correct horse battery")
    assert cli.main(["gui", "set-password", "--env-file", str(p)]) == 0
    out = capsys.readouterr().out
    line = json.loads(out.strip().splitlines()[-1])
    assert line["ok"] is True and line["command"] == "gui set-password"
    stored = [l for l in p.read_text().splitlines() if l.startswith("GUI_PASSWORD_HASH=")][0]
    h = stored.split("=", 1)[1]
    assert verify_password("correct horse battery", h)
    assert h not in out and "correct horse battery" not in out


@pytest.mark.parametrize("answers", [("correct horse battery", "different horse"), ("short", "short")])
def test_cli_rejects(tmp_path, monkeypatch, capsys, answers) -> None:
    p = _env(tmp_path, "API_KEY=x\n")
    _prompts(monkeypatch, *answers)
    assert cli.main(["gui", "set-password", "--env-file", str(p)]) == 2
    assert p.read_text() == "API_KEY=x\n"


def test_cli_missing_env_file(tmp_path, monkeypatch) -> None:
    _prompts(monkeypatch, "correct horse battery", "correct horse battery")
    assert cli.main(["gui", "set-password", "--env-file", str(tmp_path / "nope")]) == 2
```

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement `keys/envfile.py`**

```python
"""Atomic single-key update of a dotenv file, keeping its mode (V1-23)."""

import os
import re
import tempfile
from pathlib import Path


def set_env_value(path: Path, name: str, value: str) -> None:
    text = path.read_text()
    lines = text.splitlines()
    pattern = re.compile(rf"^\s*(#\s*)?{re.escape(name)}=")
    new = f"{name}={value}"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new
            break
    else:
        lines.append(new)
    mode = os.stat(path).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
```

The regex replaces only the first matching line (live or commented). That's enough: `.env` has at most one.

- [ ] **Step 4: Add the `gui set-password` subcommand to `cli.py`.**
  - Add `from getpass import getpass` at module level, so the tests can patch `cli.getpass`.
  - Parser: `gui = sub.add_parser("gui", help="GUI settings")`; `gui_sub = gui.add_subparsers(dest="gui_cmd", required=True)`; `setpw = gui_sub.add_parser("set-password", help="hash a GUI password into .env (one JSON line)")`; `setpw.add_argument("--env-file", type=Path, default=Path(".env"))`.
  - Handler `_set_password(env_file: Path) -> int`:
    1. If the file is missing, emit `{"ok": false, "command": "gui set-password", "error": "env file not found: <path>"}` and return 2.
    2. `pw = getpass("New GUI password: ")`, then `again = getpass("Repeat: ")`.
    3. If they differ, emit `ok:false` with error `passwords do not match` and return 2. If `len(pw) < MIN_LENGTH`, emit error `password must be at least 12 characters` and return 2.
    4. Otherwise call `set_env_value(env_file, "GUI_PASSWORD_HASH", hash_password(pw))`, emit `{"ok": true, "command": "gui set-password", "env_file": str(env_file), "next": "make deploy"}` and return 0.
  - Emit JSON the same way the other commands in `cli.py` do: `print(json.dumps(..., sort_keys=True))`.

- [ ] **Step 5: Run the Verify command, then `make test`.**

- [ ] **Step 6: Commit:** `feat(cli): gui set-password writes a scrypt hash to .env (V1-23)`

---

### Task 3: Password login (V1-23)

**Goal:** when `GUI_PASSWORD_HASH` is set, `/login` asks for and accepts only the password. Without it, the key login is unchanged.

**Files:**
- Modify: `app.py` (`app.state.gui_password_hash`), `gui/routes.py` (`login_form`, `login`, `_login_page`), `gui/templates/login.html`
- Create: `docs/adr/0027-gui-password-sign-on.md`
- Test: `tests/service/unit/test_gui_password_login.py`

**Acceptance Criteria:**
- [ ] With a hash set:
  - `GET /login` shows `name="password"` and the label "Password", and no `name="api_key"`;
  - `POST /login` with the right password → 303 plus the `re_session` cookie;
  - a wrong password → 401 "Invalid password";
  - posting `api_key=<the env key>` → 401;
  - five failures → 429 (same limiter).
- [ ] Without a hash, the existing `tests/service/unit/test_gui_auth.py` passes unchanged.
- [ ] The password check runs through `asyncio.to_thread(verify_password, ...)`.
- [ ] ADR-0027 exists with the sections `tests/test_docs.py::test_every_adr_has_the_required_sections` requires (copy the headings of ADR-0021), and amends ADR-0021.

**Verify:** `uv run pytest tests/service/unit/test_gui_password_login.py tests/service/unit/test_gui_auth.py tests/test_docs.py -q` → all pass

**Steps:**

- [ ] **Step 1: Failing tests.** They need an app with a hash set, so add a fixture in the test file that mirrors `tests/conftest.py::app` and sets the env first:

```python
import httpx
import pytest
from research_engine.gui.session import COOKIE
from research_engine.keys.password import hash_password

from tests.conftest import TEST_API_KEY, running_lifespan

ORIGIN = {"Origin": "http://research.localhost"}
PW = "correct horse battery"


@pytest.fixture
async def pw_app(settings_env, monkeypatch):
    monkeypatch.setenv("GUI_PASSWORD_HASH", hash_password(PW))
    from research_engine.app import create_app
    from research_engine.config import Settings
    from research_engine.testing import build_test_services

    settings = Settings()  # type: ignore[call-arg]
    application = create_app(settings, services=build_test_services(settings))
    async with running_lifespan(application):
        yield application


def anon(app, ip: str = "10.0.0.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5000)),
        base_url="http://research.localhost",
    )


async def test_form_shows_password_field(pw_app) -> None:
    async with anon(pw_app) as c:
        html = (await c.get("/login")).text
    assert 'name="password"' in html and 'name="api_key"' not in html


async def test_password_logs_in(pw_app) -> None:
    async with anon(pw_app) as c:
        r = await c.post("/login", data={"password": PW, "next": "/"}, headers=ORIGIN)
        assert r.status_code == 303 and COOKIE in c.cookies
        assert (await c.get("/")).status_code == 200


async def test_wrong_password_and_key_refused(pw_app) -> None:
    async with anon(pw_app) as c:
        r = await c.post("/login", data={"password": "nope nope nope", "next": "/"}, headers=ORIGIN)
        assert r.status_code == 401 and "Invalid password" in r.text
        r = await c.post("/login", data={"api_key": TEST_API_KEY, "next": "/"}, headers=ORIGIN)
        assert r.status_code == 401


async def test_password_rate_limited(pw_app) -> None:
    async with anon(pw_app, ip="10.0.0.77") as c:
        for _ in range(5):
            await c.post("/login", data={"password": "bad bad bad bad", "next": "/"}, headers=ORIGIN)
        r = await c.post("/login", data={"password": PW, "next": "/"}, headers=ORIGIN)
        assert r.status_code == 429
```

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement.**
  - In `app.py`, next to `app.state.api_key`, set `app.state.gui_password_hash = settings.gui_password_hash.get_secret_value() if settings.gui_password_hash else None`.
  - In `gui/routes.py`:
    - `_login_page` passes `"password_mode": request.app.state.gui_password_hash is not None` into the template context.
    - `login` gains `password: Annotated[str, Form()] = ""`. After the limiter check:

```python
    pw_hash: str | None = request.app.state.gui_password_hash
    if pw_hash is not None:
        ok = await asyncio.to_thread(verify_password, password, pw_hash)
        error = "Invalid password"
    else:
        ok = auth.key_matches(api_key, request.app.state.api_key)
        error = "Invalid key"
    if not ok:
        limiter.fail(client)
        return _login_page(request, target, error, 401)
```

    - Import `verify_password` from `research_engine.keys.password`.
  - In `login.html`, wrap the label and input in `{% if password_mode %}`: show the `password` field (label "Password", `autocomplete="current-password"`) and the hint "Use the GUI password set with research-engine gui set-password". Otherwise show the existing `api_key` field and its hint.

- [ ] **Step 4: Write ADR-0027,** "GUI password sign-on". Copy the section headings from ADR-0021.
  - **Status:** Accepted (2026-10-14).
  - **Context:** the owner wants a password, not the API key, for the GUI.
  - **Decision:**
    - optional scrypt hash in `.env`, set by the CLI;
    - when set, only the password logs in;
    - the cookie, CSRF and limiter are unchanged.
  - **Consequences:**
    - the API key no longer opens the GUI when a password is set;
    - losing the password means re-running the CLI and `make deploy`;
    - amends ADR-0021.
  - Add the new ADR to `docs/adr/README.md` if it has an index.

- [ ] **Step 5: Run the Verify command, then `make test`.**

- [ ] **Step 6: Commit:** `feat(gui): optional password sign-on (V1-23)`

---

### Task 4: Key and request tables, `KeyStore` (V1-24)

**Goal:** persistent named API keys, stored as hashes only, with create, revoke, list and a synchronous in-memory `match`. Wired into `Services`.

**Files:**
- Create: `keys/models.py`, `keys/store.py`
- Modify: `store/tables.py`, `api/deps.py`, `app.py` (`build_services`, lifespan), `testing.py` (`build_test_services`)
- Test: `tests/service/unit/test_key_store.py`

**Acceptance Criteria:**
- [ ] `ApiKeyRow` (table `api_key`) and `ApiRequestRow` (table `api_request`) are created by `init_db`. `api_key` has a partial unique index on `name WHERE revoked_at IS NULL` and a unique `key_hash`. `api_request` has indexes on `ts` and `(key_id, ts)`.
- [ ] `KeyStore.create("newsdesk")` returns `(row, plaintext)`:
  - `plaintext` matches `^re_[A-Za-z0-9_-]{43}$`;
  - `row.prefix == plaintext[3:11]`;
  - `row.key_hash == sha256(plaintext).hexdigest()`;
  - the plaintext appears in no column.
- [ ] A duplicate active name raises `KeyNameError`. The same name is allowed after the first key is revoked. A blank name or one over 64 characters raises `KeyNameError`.
- [ ] `match(plaintext)` → `Caller(KEY, "newsdesk", id)`; after `revoke(id)` → None. `match(env key)` → `Caller(ENV, "env (built-in)", None)`. `match(b"")` → None.
- [ ] `revoke` returns False for an unknown or already-revoked id.
- [ ] `Services` has `keys: KeyStore` and `usage: UsageLog`. The lifespan calls `await svc.keys.load()` after `init_db`.

**Verify:** `uv run pytest tests/service/unit/test_key_store.py -q` → all pass

**Steps:**

- [ ] **Step 1: Failing tests** (`tests/service/unit/test_key_store.py`):

```python
import hashlib
import re

import pytest
from research_engine.keys.models import Caller, CallerKind
from research_engine.keys.store import KeyNameError, KeyStore
from research_engine.store.db import create_engine_for, init_db
from sqlalchemy import text

ENV_KEY = "env-key-" + "9" * 32


@pytest.fixture
async def store():
    engine = create_engine_for(":memory:")
    await init_db(engine)
    s = KeyStore(engine, ENV_KEY.encode())
    await s.load()
    yield s
    await engine.dispose()


async def test_create_stores_hash_only(store: KeyStore) -> None:
    row, key = await store.create("newsdesk")
    assert re.fullmatch(r"re_[A-Za-z0-9_-]{43}", key)
    assert row.prefix == key[3:11]
    assert row.key_hash == hashlib.sha256(key.encode()).hexdigest()
    async with store.engine.connect() as conn:
        dump = (await conn.execute(text("SELECT * FROM api_key"))).all()
    assert key not in repr(dump)


async def test_match_and_revoke(store: KeyStore) -> None:
    row, key = await store.create("newsdesk")
    assert store.match(key.encode()) == Caller(CallerKind.KEY, "newsdesk", row.id)
    assert await store.revoke(row.id) is True
    assert store.match(key.encode()) is None
    assert await store.revoke(row.id) is False
    assert await store.revoke(999) is False


async def test_env_key_and_empty(store: KeyStore) -> None:
    assert store.match(ENV_KEY.encode()) == Caller(CallerKind.ENV, "env (built-in)", None)
    assert store.match(b"") is None
    assert store.match(b"re_nope") is None


async def test_names(store: KeyStore) -> None:
    row, _ = await store.create("app")
    with pytest.raises(KeyNameError):
        await store.create("app")
    await store.revoke(row.id)
    await store.create("app")  # reusable once revoked
    for bad in ("", "   ", "x" * 65):
        with pytest.raises(KeyNameError):
            await store.create(bad)


async def test_load_restores_active_keys(store: KeyStore) -> None:
    _, key = await store.create("a")
    fresh = KeyStore(store.engine, ENV_KEY.encode())
    await fresh.load()
    assert fresh.match(key.encode()) is not None
```

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Tables.** Add to `store/tables.py`, following the existing `JobRow` style (naive-UTC datetimes; `__tablename__` with the same `type: ignore` comment):

```python
class ApiKeyRow(SQLModel, table=True):
    __tablename__ = "api_key"  # type: ignore[assignment]  # SQLModel types __tablename__ as declared_attr
    __table_args__ = (
        Index("ux_api_key_active_name", "name", unique=True, sqlite_where=text("revoked_at IS NULL")),
    )
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=64)
    prefix: str
    key_hash: str = Field(unique=True)
    created_at: datetime
    revoked_at: datetime | None = None


class ApiRequestRow(SQLModel, table=True):
    __tablename__ = "api_request"  # type: ignore[assignment]  # SQLModel types __tablename__ as declared_attr
    __table_args__ = (Index("ix_api_request_key_ts", "key_id", "ts"),)
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    caller_kind: str
    key_id: int | None = None
    caller_name: str
    method: str
    path: str
    tool: str | None = None
    status: int
    duration_ms: int
    client_ip: str
```

Import `Index`, `text` from `sqlalchemy` and `datetime` as needed. Match the file's existing imports.

- [ ] **Step 4: `keys/models.py`**

```python
"""Who made a request (V1-24)."""

from dataclasses import dataclass
from enum import StrEnum


class CallerKind(StrEnum):
    KEY = "key"
    ENV = "env"
    GUI = "gui"


@dataclass(frozen=True, slots=True)
class Caller:
    kind: CallerKind
    name: str
    key_id: int | None


ENV_CALLER = Caller(CallerKind.ENV, "env (built-in)", None)
GUI_CALLER = Caller(CallerKind.GUI, "GUI session", None)
```

- [ ] **Step 5: `keys/store.py`**

```python
"""Named API keys (V1-24, ADR-0028): SHA-256 hashes in SQLite, active ones mirrored in memory
so the auth middleware matches without a database round trip. One app process (one uvicorn
worker), so the in-memory map is authoritative after every create/revoke."""

import hashlib
import secrets
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.api.auth import key_matches
from research_engine.keys.models import ENV_CALLER, Caller, CallerKind
from research_engine.store.tables import ApiKeyRow

MAX_NAME = 64


class KeyNameError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _digest(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()


class KeyStore:
    def __init__(self, engine: AsyncEngine, env_key: bytes) -> None:
        self._engine = engine
        self._env_key = env_key
        self._active: dict[bytes, Caller] = {}

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def load(self) -> None:
        async with AsyncSession(self._engine) as s:
            rows = (await s.exec(select(ApiKeyRow).where(col(ApiKeyRow.revoked_at).is_(None)))).all()
        self._active = {
            bytes.fromhex(r.key_hash): Caller(CallerKind.KEY, r.name, r.id) for r in rows
        }

    def match(self, supplied: bytes) -> Caller | None:
        if not supplied:
            return None
        if key_matches(supplied, self._env_key):
            return ENV_CALLER
        return self._active.get(_digest(supplied))

    async def create(self, name: str) -> tuple[ApiKeyRow, str]:
        name = name.strip()
        if not name or len(name) > MAX_NAME or not name.isprintable():
            raise KeyNameError(f"name must be 1-{MAX_NAME} printable characters")
        key = "re_" + secrets.token_urlsafe(32)
        row = ApiKeyRow(
            name=name, prefix=key[3:11], key_hash=_digest(key.encode()).hex(), created_at=_now()
        )
        try:
            async with AsyncSession(self._engine, expire_on_commit=False) as s:
                s.add(row)
                await s.commit()
                await s.refresh(row)
        except IntegrityError as exc:
            raise KeyNameError(f"an active key named {name!r} already exists") from exc
        await self.load()
        return row, key

    async def revoke(self, key_id: int) -> bool:
        async with AsyncSession(self._engine) as s:
            row = await s.get(ApiKeyRow, key_id)
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = _now()
            s.add(row)
            await s.commit()
        await self.load()
        return True

    async def list_keys(self) -> list[ApiKeyRow]:
        async with AsyncSession(self._engine) as s:
            stmt = select(ApiKeyRow).order_by(
                col(ApiKeyRow.revoked_at).is_not(None), col(ApiKeyRow.created_at).desc()
            )
            return list((await s.exec(stmt)).all())
```

`key_matches` is imported from `api/auth.py`. If that makes an import cycle once Task 5 makes `auth.py` import `keys.models`, move `key_matches` into `keys/models.py` and re-export it from `auth.py`. Run `uv run python -c "import research_engine.app"` to check.

- [ ] **Step 6: Wiring.**
  - `api/deps.py`: add `keys: KeyStore` and `usage: UsageLog` fields to `Services`, placed after `job_store` and before the defaulted fields. `UsageLog` comes from Task 6, so for now create `keys/usage.py` with a minimal `class UsageLog: def __init__(self, engine: AsyncEngine) -> None: self._engine = engine`, which Task 6 fills in.
  - `app.py` `build_services`: `keys = KeyStore(engine, settings.api_key.get_secret_value().encode())`, `usage = UsageLog(engine)`, then pass both.
  - `testing.py` `build_test_services`: the same.
  - The lifespan: right after `await init_db(svc.engine)`, add `await svc.keys.load()`.

- [ ] **Step 7: Run the Verify command, then `make test`.** The existing tests must still pass.

- [ ] **Step 8: Commit:** `feat(keys): named API key store with hash-only storage (V1-24)`

---

### Task 5: Auth middleware uses `KeyStore` and records the caller (V1-24)

**Goal:** `/v1` and `/mcp` accept any active named key as well as the `.env` key, reject revoked ones, and every authenticated request carries `scope["state"]["caller"]`.

**Files:**
- Modify: `api/auth.py`, `app.py` (middleware construction)
- Test: `tests/service/unit/test_auth_named_keys.py`

**Acceptance Criteria:**
- [ ] A key created with `services.keys.create` gets 200 on `POST /v1/search` and on an MCP `tools/list`. After `revoke`, both get 401.
- [ ] The `.env` key still works, and all existing `tests/service/unit/test_auth.py`, `test_gui_auth.py` and `test_mcp.py` tests pass unchanged.
- [ ] Inside a route, `request.state.caller` is the matching `Caller`: `KEY` with its name, `ENV`, or `GUI` for the session cookie.
- [ ] `ApiKeyMiddleware.__init__` no longer takes `api_key`. It takes `keys: Callable[[], KeyStore]`, called per request, because `Services` exists only after the lifespan starts.

**Verify:** `uv run pytest tests/service/unit/test_auth_named_keys.py tests/service/unit/test_auth.py tests/service/unit/test_gui_auth.py tests/service/unit/test_mcp.py -q` → all pass

**Steps:**

- [ ] **Step 1: Failing tests.** Read `tests/service/unit/test_mcp.py` to copy how it posts an MCP `tools/list` (headers, JSON-RPC body), and reuse that helper. For the caller check, add a throwaway route to the test app:

```python
import httpx
from fastapi import Request
from research_engine.keys.models import CallerKind

from tests.conftest import TEST_API_KEY


def raw(app, key: str | None) -> httpx.AsyncClient:
    headers = {"X-API-Key": key} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://research.localhost", headers=headers
    )


async def test_named_key_then_revoked(app) -> None:
    row, key = await app.state.services.keys.create("newsdesk")
    async with raw(app, key) as c:
        assert (await c.post("/v1/search", json={"query": "x", "depth": "quick"})).status_code == 200
        await app.state.services.keys.revoke(row.id)
        assert (await c.post("/v1/search", json={"query": "x", "depth": "quick"})).status_code == 401


async def test_caller_in_state(app) -> None:
    @app.get("/v1/_whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"kind": request.state.caller.kind, "name": request.state.caller.name}

    _, key = await app.state.services.keys.create("laptop")
    async with raw(app, key) as c:
        assert (await c.get("/v1/_whoami")).json() == {"kind": CallerKind.KEY, "name": "laptop"}
    async with raw(app, TEST_API_KEY) as c:
        assert (await c.get("/v1/_whoami")).json()["kind"] == CallerKind.ENV
```

Also add an MCP case: a named key can `tools/list`, and after revocation gets 401. Add a GUI case: log in through `/login` as `test_gui_auth._login` does, call `/v1/_whoami` with the cookie, and expect `gui`.

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement** in `api/auth.py`:
  - The constructor becomes `__init__(self, app, keys: Callable[[], KeyStore], codec, site_host)`.
  - `_has_key` becomes `_caller(scope) -> Caller | None`, which returns `self._keys().match(headers.get(b"x-api-key", b""))`.
  - In `__call__`: `caller = self._caller(scope)`; if there is one, `scope.setdefault("state", {})["caller"] = caller` and pass through.
  - On the session branch, set `scope["state"]["caller"] = GUI_CALLER` before passing through.
  - Remove `self._key`.
  - Keep `key_matches` exported, since the GUI key login still uses it.
  - Update the module docstring: step 2 now says "a valid key: the `.env` `API_KEY` or an active named key (V1-24)".
  - Use the `TYPE_CHECKING` import for `KeyStore` if needed to avoid a cycle.
- In `app.py`, `app.add_middleware(ApiKeyMiddleware, keys=lambda: app.state.services.keys, codec=codec, site_host=settings.site_host)`.

- [ ] **Step 4: Run the Verify command, then `make test`.**

- [ ] **Step 5: Commit:** `feat(auth): accept named API keys and record the caller (V1-24)`

---

### Task 6: Request log writer, middleware, pruning (V1-24)

**Goal:** every authenticated `/v1` or `/mcp` request becomes one `api_request` row, written in batches off the request path, pruned after the retention period, and queryable for stats.

**Files:**
- Modify: `keys/usage.py` (fill in), `app.py` (start and stop `UsageLog`, add `UsageMiddleware`), `maintenance.py`
- Create: `keys/middleware.py`
- Test: `tests/service/unit/test_usage_log.py`, `tests/service/unit/test_maintenance.py` (add a case)

**Acceptance Criteria:**
- [ ] After `POST /v1/search` with a named key and `await services.usage.flush()`, exactly one row exists: `caller_kind="key"`, the right `key_id` and `caller_name`, `method="POST"`, `path="/v1/search"`, `status=200`, `duration_ms >= 0`, `client_ip` the test client IP.
- [ ] An MCP `tools/call` of `web_search` logs `path="/mcp"` and `tool="web_search"`. `tools/list` logs `tool=None`.
- [ ] Unauthenticated 401s, `/health` and GUI page requests (`/`, `/keys`) are not logged. GUI-cookie `/v1` calls are logged with `caller_kind="gui"`.
- [ ] Rows store no query or body text: the search query string `"zebra-unique-term"` appears nowhere in a `SELECT *` dump.
- [ ] With `max_queue=2`, a third `record` before a flush is dropped, and `dropped == 1`. A failing flush (engine disposed) logs a warning and doesn't raise to the request.
- [ ] `prune(30)` deletes rows older than 30 days and keeps newer ones. `maintenance.run_once` returns a `requests_pruned` count.
- [ ] `key_stats()` returns, per caller (`"env"`, `"gui"`, `"key:<id>"`), the 24-hour count, 30-day count, `last_used` and `last_ip`.
- [ ] `recent(caller=None|"env"|"gui"|"key:<id>", errors_only=bool, before_id=int|None, limit=50)` returns rows newest first. `errors_only` keeps rows with `status >= 400`.
- [ ] Rows still queued at shutdown are flushed (the lifespan `stop()` runs before the engine is disposed).

**Verify:** `uv run pytest tests/service/unit/test_usage_log.py tests/service/unit/test_maintenance.py -q` → all pass

**Steps:**

- [ ] **Step 1: Failing tests** (`tests/service/unit/test_usage_log.py`). They use the `app` fixture and `app.state.services`:

```python
from datetime import UTC, datetime, timedelta

import httpx
from research_engine.keys.usage import RequestRecord, UsageLog
from sqlalchemy import text

from tests.conftest import TEST_API_KEY


def raw(app, key: str | None, ip: str = "10.1.2.3") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 4000)),
        base_url="http://research.localhost",
        headers={"X-API-Key": key} if key else {},
    )


async def rows(app) -> list[dict]:
    await app.state.services.usage.flush()
    async with app.state.services.engine.connect() as conn:
        res = await conn.execute(text("SELECT * FROM api_request ORDER BY id"))
        return [dict(r._mapping) for r in res]


async def test_named_key_request_logged(app) -> None:
    row, key = await app.state.services.keys.create("newsdesk")
    async with raw(app, key) as c:
        await c.post("/v1/search", json={"query": "zebra-unique-term", "depth": "quick"})
    [r] = await rows(app)
    assert (r["caller_kind"], r["key_id"], r["caller_name"]) == ("key", row.id, "newsdesk")
    assert (r["method"], r["path"], r["status"], r["client_ip"]) == ("POST", "/v1/search", 200, "10.1.2.3")
    assert "zebra-unique-term" not in repr(r)


async def test_unauthenticated_and_open_not_logged(app) -> None:
    async with raw(app, None) as c:
        await c.post("/v1/search", json={"query": "x"})
        await c.get("/health")
    assert await rows(app) == []


async def test_overflow_drops(app) -> None:
    log = UsageLog(app.state.services.engine, max_queue=2)
    rec = RequestRecord(
        ts=datetime.now(UTC), caller_kind="env", key_id=None, caller_name="env (built-in)",
        method="GET", path="/v1/x", tool=None, status=200, duration_ms=1, client_ip="1.1.1.1",
    )
    for _ in range(3):
        log.record(rec)
    assert log.dropped == 1


async def test_prune(app) -> None:
    usage = app.state.services.usage
    old = datetime.now(UTC) - timedelta(days=31)
    base = dict(caller_kind="env", key_id=None, caller_name="env (built-in)", method="GET",
                path="/v1/x", tool=None, status=200, duration_ms=1, client_ip="1.1.1.1")
    usage.record(RequestRecord(ts=old, **base))
    usage.record(RequestRecord(ts=datetime.now(UTC), **base))
    await usage.flush()
    assert await usage.prune(30) == 1
    assert len(await rows(app)) == 1
```

Add the remaining cases from the Acceptance Criteria:
- MCP `tools/call web_search` with `tool="web_search"`, using the `test_mcp.py` helper to build the JSON-RPC body;
- GUI-cookie `/v1` with `caller_kind="gui"`, and `GET /` not logged;
- `key_stats` counts;
- `recent` filters: `errors_only` by forcing a 422 with `json={"query": ""}`, and `caller="key:<id>"`;
- a failing flush: dispose a private engine, record, `await flush()`, and expect no exception.

For `maintenance`, extend the existing test so it asserts `"requests_pruned"` is in `run_once`'s result.

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement `keys/usage.py`**

```python
"""Per-request API usage log (V1-24, ADR-0028). ``record`` is sync and never blocks or raises;
a background task flushes batches every second. Stats and paging queries back the Keys page."""

import asyncio
import contextlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, func, insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from research_engine.store.tables import ApiRequestRow

_log = structlog.get_logger("research_engine.usage")
MAX_QUEUE = 10_000
FLUSH_INTERVAL_S = 1.0


def _naive(dt: datetime) -> datetime:
    return dt if dt.tzinfo is None else dt.astimezone(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class RequestRecord:
    ts: datetime
    caller_kind: str
    key_id: int | None
    caller_name: str
    method: str
    path: str
    tool: str | None
    status: int
    duration_ms: int
    client_ip: str


@dataclass(frozen=True, slots=True)
class KeyStats:
    last_24h: int
    last_30d: int
    last_used: datetime | None
    last_ip: str | None


def caller_id(kind: str, key_id: int | None) -> str:
    return f"key:{key_id}" if kind == "key" else kind


class UsageLog:
    def __init__(
        self, engine: AsyncEngine, *, max_queue: int = MAX_QUEUE, interval_s: float = FLUSH_INTERVAL_S
    ) -> None:
        self._engine = engine
        self._queue: asyncio.Queue[RequestRecord] = asyncio.Queue(maxsize=max_queue)
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0

    def record(self, rec: RequestRecord) -> None:
        try:
            self._queue.put_nowait(rec)
        except asyncio.QueueFull:
            self.dropped += 1

    async def flush(self) -> int:
        batch: list[RequestRecord] = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if self.dropped:
            _log.warning("request log dropped rows", dropped=self.dropped)
            self.dropped = 0
        if not batch:
            return 0
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(ApiRequestRow),
                    [{**asdict(r), "ts": _naive(r.ts)} for r in batch],
                )
        except Exception:
            _log.exception("request log flush failed", rows=len(batch))
            return 0
        return len(batch)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="usage-log")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

    async def prune(self, days: int) -> int:
        cutoff = _naive(datetime.now(UTC) - timedelta(days=days))
        async with self._engine.begin() as conn:
            res = await conn.execute(delete(ApiRequestRow).where(col(ApiRequestRow.ts) < cutoff))
        return res.rowcount or 0

    async def key_stats(self) -> dict[str, KeyStats]:
        now = _naive(datetime.now(UTC))
        day, month = now - timedelta(days=1), now - timedelta(days=30)
        r = ApiRequestRow
        stmt = select(
            r.caller_kind,
            r.key_id,
            func.sum((col(r.ts) >= day).cast(Integer)),
            func.count(),
            func.max(r.ts),
        ).where(col(r.ts) >= month).group_by(r.caller_kind, r.key_id)
        out: dict[str, KeyStats] = {}
        async with AsyncSession(self._engine) as s:
            for kind, key_id, d, m, last in (await s.exec(stmt)).all():
                ip = (
                    await s.exec(
                        select(r.client_ip)
                        .where(r.caller_kind == kind, r.key_id == key_id if key_id is not None else col(r.key_id).is_(None))
                        .order_by(col(r.id).desc())
                        .limit(1)
                    )
                ).first()
                out[caller_id(kind, key_id)] = KeyStats(int(d or 0), int(m), last.replace(tzinfo=UTC) if last else None, ip)
        return out

    async def recent(
        self, *, caller: str | None = None, errors_only: bool = False, before_id: int | None = None, limit: int = 50
    ) -> list[ApiRequestRow]:
        r = ApiRequestRow
        stmt = select(r).order_by(col(r.id).desc()).limit(limit)
        if caller == "env" or caller == "gui":
            stmt = stmt.where(r.caller_kind == caller)
        elif caller and caller.startswith("key:") and caller[4:].isdigit():
            stmt = stmt.where(r.caller_kind == "key", r.key_id == int(caller[4:]))
        if errors_only:
            stmt = stmt.where(col(r.status) >= 400)
        if before_id is not None:
            stmt = stmt.where(col(r.id) < before_id)
        async with AsyncSession(self._engine) as s:
            return list((await s.exec(stmt)).all())
```

Import `Integer` from `sqlalchemy`. Pyright strict may need small type adjustments in `key_stats`, such as an explicit `cast(...)` on columns. Keep the behaviour the same.

- [ ] **Step 4: Implement `keys/middleware.py`**

```python
"""Logs one row per authenticated /v1 or /mcp request (V1-24). Wraps ApiKeyMiddleware and reads
the ``Caller`` it put in ``scope["state"]``. For POST /mcp it peeks at the JSON-RPC body as it
streams through (first 64 KiB, never stored) to name the tool of a ``tools/call``."""

import json
import time
from datetime import UTC, datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_engine.api.auth import is_api_path
from research_engine.keys.models import Caller
from research_engine.keys.usage import RequestRecord

SNIFF_LIMIT = 64 * 1024


def _tool(body: bytes) -> str | None:
    try:
        msg = json.loads(body)
    except ValueError:
        return None
    if isinstance(msg, dict) and msg.get("method") == "tools/call":
        params = msg.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        return name[:64] if isinstance(name, str) else None
    return None


class UsageMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_api_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        body = bytearray()
        sniff = scope["path"].startswith("/mcp") and scope["method"] == "POST"

        async def receive_wrapper() -> Message:
            message = await receive()
            if sniff and message["type"] == "http.request" and len(body) < SNIFF_LIMIT:
                body.extend(message.get("body", b"")[: SNIFF_LIMIT - len(body)])
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_wrapper if sniff else receive, send_wrapper)
        finally:
            caller: Caller | None = scope.get("state", {}).get("caller")
            if caller is not None:
                try:
                    client = scope.get("client")
                    scope["app"].state.services.usage.record(
                        RequestRecord(
                            ts=datetime.now(UTC),
                            caller_kind=caller.kind.value,
                            key_id=caller.key_id,
                            caller_name=caller.name,
                            method=scope["method"],
                            path=scope["path"][:256],
                            tool=_tool(bytes(body)) if sniff else None,
                            status=status,
                            duration_ms=int((time.perf_counter() - started) * 1000),
                            client_ip=client[0] if client else "unknown",
                        )
                    )
                except Exception:  # logging must never fail a request
                    pass
```

Ruff will flag the bare `except Exception: pass` (S110 or BLE001). Use `contextlib.suppress(Exception)` instead, with a comment giving the reason. `_tool` truncates the tool name to 64 characters.

- [ ] **Step 5: Wiring in `app.py`.**
  - Lifespan: after `await svc.keys.load()`, call `svc.usage.start()`.
  - `_shutdown`: add `("stop usage log", services.usage.stop)` immediately before `("dispose engine", ...)`.
  - Middleware: add `app.add_middleware(UsageMiddleware)` immediately **after** the `ApiKeyMiddleware` line, so it wraps auth. Update the order comment: "...then the catch-all, then usage logging (it reads the caller auth sets), then auth."
- `maintenance.run_once`: add `requests = await services.usage.prune(services.settings.request_log_retention_days)` and return `"requests_pruned": requests`.

- [ ] **Step 6: Run the Verify command, then `make test`.**

- [ ] **Step 7: Commit:** `feat(keys): per-request API usage log with 30-day retention (V1-24)`

---

### Task 7: Keys page in the GUI (V1-24)

**Goal:** `/keys` lists keys with usage, creates keys (shown once), revokes keys, and shows a filterable, paged recent-requests table. It's reachable from the nav and only from a GUI session.

**Files:**
- Create: `gui/keys.py`, `gui/templates/keys.html`, `gui/templates/_requests_table.html`, `gui/static/keys.js`
- Modify: `app.py` (include the router), `gui/templates/base.html` (nav link)
- Test: `tests/service/unit/test_gui_keys.py`

**Acceptance Criteria:**
- [ ] `GET /keys` with a GUI session → 200 and `Cache-Control: no-store`. The page lists "env (built-in)" first, then active keys, then revoked keys (with the class `revoked`), each showing name, prefix, created, status, 24h, 30d, last used and last IP.
- [ ] `GET /keys` or `POST /keys` with only `X-API-Key` (the env key or a named key) → 403. Without auth → 303 to `/login`.
- [ ] `POST /keys` (name, Origin same-site) → 200, the page shows the full new key exactly once in `<code id="new-key">`, and the key works on `/v1/search`.
  - A duplicate name → 400 with the error message, and no key created.
  - A cross-origin POST → 403 (middleware).
- [ ] `POST /keys/{id}/revoke` → 303 to `/keys`; the key then gets 401. Unknown or already-revoked → 404.
- [ ] `GET /keys/requests?caller=key:<id>&errors=1&before=<id>` returns the `_requests_table.html` partial: newest first, 50 per page, with a "Older" link carrying `before=<last id>` when a full page came back.
- [ ] A key name containing `<script>` is rendered escaped.
- [ ] `base.html` nav has `<a href="/keys">Keys</a>`.
- [ ] `keys.js` (no inline script; CSP is `script-src 'self'`) copies `#new-key` text to the clipboard on clicking `button#copy-new-key`, falling back to selecting the text as `try.js` does.

**Verify:** `uv run pytest tests/service/unit/test_gui_keys.py -q` → all pass

**Steps:**

- [ ] **Step 1: Failing tests** (`tests/service/unit/test_gui_keys.py`). Reuse the `anon`, `_login` and `ORIGIN` helpers by importing them from `tests.service.unit.test_gui_auth`:

```python
import re

import httpx
from tests.conftest import TEST_API_KEY
from tests.service.unit.test_gui_auth import ORIGIN, _login, anon


async def gui(app) -> httpx.AsyncClient:
    c = anon(app)
    assert (await _login(c)).status_code == 303
    return c


async def test_page_lists_env_first(app) -> None:
    async with await gui(app) as c:
        r = await c.get("/keys")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "env (built-in)" in r.text


async def test_api_key_cannot_manage_keys(app) -> None:
    _, named = await app.state.services.keys.create("app")
    for key in (TEST_API_KEY, named):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://research.localhost",
            headers={"X-API-Key": key, **ORIGIN},
        ) as c:
            assert (await c.get("/keys")).status_code == 403
            assert (await c.post("/keys", data={"name": "evil"})).status_code == 403


async def test_create_shows_key_once_and_it_works(app) -> None:
    async with await gui(app) as c:
        r = await c.post("/keys", data={"name": "newsdesk"}, headers=ORIGIN)
        assert r.status_code == 200
        key = re.search(r'<code id="new-key">(re_[A-Za-z0-9_-]{43})</code>', r.text).group(1)
        again = await c.get("/keys")
        assert key not in again.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://research.localhost",
        headers={"X-API-Key": key},
    ) as api:
        assert (await api.post("/v1/search", json={"query": "x", "depth": "quick"})).status_code == 200


async def test_duplicate_and_escape(app) -> None:
    async with await gui(app) as c:
        await c.post("/keys", data={"name": "<script>x</script>"}, headers=ORIGIN)
        r = await c.post("/keys", data={"name": "<script>x</script>"}, headers=ORIGIN)
        assert r.status_code == 400
        page = (await c.get("/keys")).text
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page


async def test_revoke(app) -> None:
    row, key = await app.state.services.keys.create("tmp")
    async with await gui(app) as c:
        r = await c.post(f"/keys/{row.id}/revoke", headers=ORIGIN)
        assert r.status_code == 303 and r.headers["location"] == "/keys"
        assert (await c.post(f"/keys/{row.id}/revoke", headers=ORIGIN)).status_code == 404
        assert (await c.post("/keys/9999/revoke", headers=ORIGIN)).status_code == 404
```

Add tests for the remaining Acceptance Criteria: the requests partial (filter by `caller=key:<id>`, `errors=1`, newest first, the "Older" link when there are ≥ 50 rows, driven by recording 51 `RequestRecord`s and flushing), cross-origin POST → 403, unauthenticated → 303, and the nav link.

- [ ] **Step 2: Run them and watch them fail.**

- [ ] **Step 3: Implement `gui/keys.py`**

```python
"""Keys page (V1-24, ADR-0028): create, revoke and watch usage of named API keys.
GUI-session only: a request authenticated by X-API-Key gets 403, so a leaked app key cannot
mint more keys."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from research_engine.api.deps import Services, get_services
from research_engine.gui.routes import templates
from research_engine.keys.models import Caller, CallerKind
from research_engine.keys.store import KeyNameError
from research_engine.keys.usage import caller_id

router = APIRouter(include_in_schema=False)
PAGE = 50
_NO_STORE = {"Cache-Control": "no-store"}


def require_gui(request: Request) -> None:
    caller: Caller | None = getattr(request.state, "caller", None)
    if caller is None or caller.kind is not CallerKind.GUI:
        raise HTTPException(403, "the Keys page needs a GUI login, not an API key")


async def _page(
    request: Request, svc: Services, *, new_key: str | None = None, new_name: str | None = None,
    error: str | None = None, status: int = 200,
) -> HTMLResponse:
    keys = await svc.keys.list_keys()
    stats = await svc.usage.key_stats()
    rows = await svc.usage.recent(limit=PAGE)
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "keys": keys, "stats": stats, "env_stats": stats.get("env"), "gui_stats": stats.get("gui"),
            "rows": rows, "new_key": new_key, "new_name": new_name, "error": error,
            "caller": None, "errors_only": False, "page": PAGE, "caller_id": caller_id,
        },
        status_code=status,
        headers=_NO_STORE,
    )


@router.get("/keys", response_class=HTMLResponse, dependencies=[Depends(require_gui)])
async def keys_page(request: Request, svc: Annotated[Services, Depends(get_services)]) -> HTMLResponse:
    return await _page(request, svc)


@router.post("/keys", response_class=HTMLResponse, dependencies=[Depends(require_gui)])
async def create_key(
    request: Request, svc: Annotated[Services, Depends(get_services)], name: Annotated[str, Form()] = ""
) -> HTMLResponse:
    try:
        row, key = await svc.keys.create(name)
    except KeyNameError as exc:
        return await _page(request, svc, error=str(exc), status=400)
    return await _page(request, svc, new_key=key, new_name=row.name)


@router.post("/keys/{key_id}/revoke", dependencies=[Depends(require_gui)])
async def revoke_key(key_id: int, svc: Annotated[Services, Depends(get_services)]) -> Response:
    if not await svc.keys.revoke(key_id):
        raise HTTPException(404, "no active key with that id")
    return RedirectResponse("/keys", status_code=303)


@router.get("/keys/requests", response_class=HTMLResponse, dependencies=[Depends(require_gui)])
async def requests_partial(
    request: Request,
    svc: Annotated[Services, Depends(get_services)],
    caller: Annotated[str | None, Query(max_length=32)] = None,
    errors: Annotated[bool, Query()] = False,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> HTMLResponse:
    rows = await svc.usage.recent(caller=caller or None, errors_only=errors, before_id=before, limit=PAGE)
    return templates.TemplateResponse(
        request, "_requests_table.html",
        {"rows": rows, "caller": caller, "errors_only": errors, "page": PAGE},
        headers=_NO_STORE,
    )
```

Check how `gui/routes.py` renders `HTTPException` for GUI paths: a plain page or an envelope. The tests assert only status codes. If 403 and 404 render with the GUI error page, keep that.

- [ ] **Step 4: Templates.**
  - `keys.html` extends `base.html` and has three sections:
    1. **Create a key:** `<form method="post" action="/keys">` with `<input name="name" maxlength="64" required>`; `{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}`.
    2. **New key box** (`{% if new_key %}`): "New key for {{ new_name }}. Copy it now: you won't see it again." `<code id="new-key">{{ new_key }}</code> <button type="button" id="copy-new-key">Copy</button>`.
    3. **Keys table:** rows for env (built-in, with the `env_stats` values), then each `k` in `keys`, using `stats.get(caller_id("key", k.id))`. Columns: name, `re_{{ k.prefix }}…`, created, status (active / revoked on date), 24h, 30d, last used, last IP. Active keys get an inline `<form method="post" action="/keys/{{ k.id }}/revoke"><button>Revoke</button></form>`. Revoked rows get `class="revoked"`.
  - Below the tables, a filter form `hx-get="/keys/requests" hx-target="#requests"` with a caller `<select>` (All, env, GUI session, each key as `key:<id>`) and an "Errors only" checkbox `name="errors" value="1"`. Then `<div id="requests">{% include "_requests_table.html" %}</div>`.
  - `_requests_table.html`: a table with time, caller, method, path, tool, status, ms and IP. Status ≥ 400 gets `class="error"`. When `rows|length == page`, add an "Older" link: `hx-get="/keys/requests?before={{ rows[-1].id }}{% if caller %}&caller={{ caller|urlencode }}{% endif %}{% if errors_only %}&errors=1{% endif %}" hx-target="#requests"`.
  - Check how htmx is loaded in `base.html` and on the dashboard (`static/vendor`), and follow the same pattern.
  - Load `keys.js` with `<script src="/static/keys.js" defer></script>` inside the `keys.html` block that `base.html` provides for extra scripts. If there is none, follow how `try.html` loads `try.js`.
  - Add minimal CSS to `app.css` for `.revoked { opacity: .55 }` and `#new-key` (monospace, word-break). Reuse the existing table and error classes.
- `keys.js`:

```javascript
"use strict";
document.addEventListener("DOMContentLoaded", function () {
  var button = document.getElementById("copy-new-key");
  var code = document.getElementById("new-key");
  if (!button || !code) return;
  button.addEventListener("click", function () {
    var text = code.textContent || "";
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { button.textContent = "Copied"; });
    } else {
      var range = document.createRange();
      range.selectNodeContents(code);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      button.textContent = "Selected: press Ctrl/Cmd+C";
    }
  });
});
```

- [ ] **Step 5: Wiring.** In `app.py`, `from research_engine.gui import keys as gui_keys` and `app.include_router(gui_keys.router)` after `gui_routes.router`. In `base.html`, add `<a href="/keys">Keys</a>` after "Try it".

- [ ] **Step 6: Run the Verify command, then `make test`.**

- [ ] **Step 7: Commit:** `feat(gui): Keys page to create, revoke and audit API keys (V1-24)`

---

### Task 8: Docs, ADR-0028 and the 1.1.0 version bump (V1-23, V1-24)

**Goal:** operators and agent builders know how to set the password and use per-app keys. The decision is recorded and the version is 1.1.0.

**Files:**
- Create: `docs/adr/0028-named-api-keys-and-request-log.md`
- Modify: `README.md`, `docs/OPERATIONS.md`, `docs/USING.md`, `docs/adr/README.md` (index, if present), `pyproject.toml`, `packages/research_engine/pyproject.toml`, `packages/research_engine_client/pyproject.toml`, both `__init__.py` `__version__`, `tests/test_workspace.py` (`VERSION`), `uv.lock` (via `uv lock`)
- Test: existing `tests/test_docs.py`, `tests/test_workspace.py`

**Acceptance Criteria:**
- [ ] `docs/OPERATIONS.md` has a section "GUI password and API keys" covering:
  - `uv run research-engine gui set-password` then `make deploy`;
  - creating and revoking keys at `/keys`;
  - the request log (what's stored, 30-day retention, `REQUEST_LOG_RETENTION_DAYS`);
  - that the `.env` `API_KEY` is the built-in key, used by `make smoke`, `make health` and the ops scripts, and is rotated with the existing runbook.
- [ ] `docs/USING.md` adds at most 60 words: ask the operator for a per-app key from the Keys page, then send it as `X-API-Key`. It stays ≤ 1,800 words.
- [ ] The README GUI/auth section mentions the password option and the Keys page.
- [ ] ADR-0028 has the required sections and records:
  - SHA-256 hash-only storage, with the reasons;
  - the in-memory match map (single worker);
  - GUI-only management;
  - the request-log fields and what's excluded;
  - 30-day retention;
  - the env key stays built-in;
  - that it amends ADR-0021's "single shared key" consequence.
- [ ] The version is 1.1.0 everywhere `1.0.1` was (the files listed above). `uv lock` updated. `schemas/` regenerated if `uv run research-engine schemas export --check` says it's stale.
- [ ] `make test` passes.

**Verify:** `make test` → ruff, format, pyright, pytest all pass; `uv run research-engine schemas export --check` exits 0

**Steps:**
- [ ] **Step 1:** Write ADR-0028, copying ADR-0021's section headings.
- [ ] **Step 2:** Update `OPERATIONS.md`, `USING.md` and `README.md` as the Acceptance Criteria say. Use only `research.example`-style hosts; no lab values.
- [ ] **Step 3:** Bump the version: `grep -rn '1\.0\.1' pyproject.toml packages/*/pyproject.toml packages/*/src/*/__init__.py tests/test_workspace.py`, change each to `1.1.0`, then run `uv lock`.
- [ ] **Step 4:** Run `uv run research-engine schemas export --check`. If it fails, run `uv run research-engine schemas export` and include `schemas/`.
- [ ] **Step 5:** Run `make test`.
- [ ] **Step 6: Commit:** `docs: GUI password, API keys and request log; v1.1.0 (V1-23, V1-24)`

---

### Task 9: Release and live verification (V1-23, V1-24)

**Goal:** merge, tag `v1.1.0`, deploy, and prove on the live host that a named key works, is logged, and stops working when revoked.

**Files:** none (operations only). Update `HANDOFF.local.md` at the end (git-ignored).

**Acceptance Criteria:**
- [ ] The PR `feat/api-keys-gui-password` → `main` is opened with `GH_TOKEN=$(gh auth token --user pentonvillefandango) gh pr create`, CI is green and the PR is merged. `main` is tagged `v1.1.0` and pushed.
- [ ] `make deploy` output has `"ok": true`, sandbox and smoke pass. `make version` shows `"version": "1.1.0"` and the merged SHA.
- [ ] Live check inside the app container (no secrets printed):
  - create a key named `release-check` through `KeyStore`, using a small Python snippet run with `docker compose -p research-engine exec app python -c ...` from `.deploy/current`, or through the GUI with curl and a session;
  - `POST /v1/search` with that key → 200;
  - an `api_request` row exists for `release-check`;
  - revoke it, then the same call → 401.
  - Pick the method that avoids printing the key: keep it in a shell variable, never echo it.
- [ ] `make health` → `"ok": true`.
- [ ] `HANDOFF.local.md` "Current state" and "Open items" are updated: v1.1.0 is live, and the owner still needs to run `set-password` and `make deploy`, then move each app to its own key.

**Verify:** `make health && make version` → ok true, version 1.1.0

**Steps:**
- [ ] **Step 1:** Push the branch and open the PR, with the body ending in the Claude Code attribution line. Wait for CI with a single `run_in_background` loop on `gh pr checks`. If the hosted runners don't pick up jobs, use the same SHA's push run as evidence.
- [ ] **Step 2:** Merge (`gh pr merge --merge`), `git switch main && git pull`, then `git tag -a v1.1.0 -m "v1.1.0: GUI password, named API keys, request log (V1-23, V1-24)" && git push origin v1.1.0`.
- [ ] **Step 3:** `make deploy`, `make version`, `make health`.
- [ ] **Step 4:** Run the live check from the Acceptance Criteria and capture its output, with the key redacted.
- [ ] **Step 5:** Update `HANDOFF.local.md`.
