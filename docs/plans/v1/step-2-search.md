# Step 2: SearXNG adapter and `/v1/search`

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:** V1-01, V1-02, V1-03, V1-18 (`/v1/engines`, settings), plus the step-2 seams from design §4.

**Outcome:**
- A minimal Compose stack (`searxng` and `crawl4ai`) runs for fixtures and integration tests.
- Settings and the intent presets load from config.
- The in-memory event and cache seams are in place.
- URL canonicalisation and ranking are implemented.
- The SearXNG adapter, `SearchService` and the FastAPI app (envelope, API-key auth, `/v1/search`, `/v1/engines`) work.
- The integration test proves that a technical query returns at least 10 results from at least 3 engines.

Run the tasks in order. Tasks 2.3 and 2.4 touch disjoint files and may run in parallel.

---

### Task 2.1: Minimal Compose stack and recorded SearXNG fixtures

**Goal:** Pin and run `searxng` and `crawl4ai` in this project's own Compose project, configure SearXNG for internal JSON use, and record real responses as committed fixtures.

**Files:**
- Create: `compose.yaml` (minimal: `searxng`, `crawl4ai`; extended in step 8)
- Create: `compose.dev.yaml` (dev-only override that publishes each service on an **ephemeral 127.0.0.1 port**)
- Create: `deploy/searxng/settings.yml`
- Modify: `.env.example` (add `SEARXNG_SECRET`, `CRAWL4AI_API_TOKEN`)
- Create: `scripts/dev_urls.sh`, `scripts/record_fixtures.py`
- Create: `tests/fixtures/searxng/technical_page1.json`, `tests/fixtures/searxng/general_page1.json`, `tests/fixtures/searxng/engines_config.json`

**Acceptance Criteria:**
- [ ] `docker compose -f compose.yaml -f compose.dev.yaml up -d --wait` brings both services to `healthy`.
- [ ] `docker compose ps` shows the project `research-engine`, and no host port other than the `127.0.0.1:<ephemeral>` mappings.
- [ ] `curl "$SEARXNG_LIVE_URL/search?q=fastapi&format=json"` returns JSON (not a 403).
- [ ] The recorded fixtures exist and contain `results`, `unresponsive_engines` and `suggestions` keys. A grep finds no `/home/`, `toolbox` or private IPs in them.
- [ ] `engines_config.json` lists the engine names that are enabled. `config/intents.yaml` (Task 2.2) uses only those names.

**Verify:** `source <(scripts/dev_urls.sh) && curl -sf "$SEARXNG_LIVE_URL/search?q=fastapi&format=json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d["results"])>0)'` → `True`

**Steps:**

- [ ] **Step 1: Create `deploy/searxng/settings.yml`.** Engine names **must** be checked against the running instance in Step 5. Adjust the `keep_only` list to the real names before you finalise.

```yaml
# SearXNG settings for internal use by the Research Engine.
# Docs: https://docs.searxng.org/admin/settings/index.html
use_default_settings:
  engines:
    keep_only:
      # general web
      - duckduckgo
      - brave
      - bing
      - startpage
      - mojeek
      - qwant
      - wikipedia
      # technical
      - github
      - stackoverflow
      - mdn
      - pypi
      - npm
      - docker hub
      # academic
      - arxiv
      - google scholar
      - semantic scholar
      - crossref
      # news
      - bing news
      - duckduckgo news
general:
  instance_name: "research-engine-searxng"
server:
  # secret_key comes from SEARXNG_SECRET in the environment
  limiter: false          # internal use only; the limiter would need Valkey
  public_instance: false
  image_proxy: false
  method: "GET"
search:
  formats: [html, json]
  safe_search: 0
  default_lang: "en-GB"
outgoing:
  request_timeout: 6.0
  max_request_timeout: 15.0
  pool_connections: 100
  pool_maxsize: 20
```

- [ ] **Step 2: Create the minimal `compose.yaml`.** Step 8 adds `app`, limits and hardening.

```yaml
name: research-engine

services:
  searxng:
    image: searxng/searxng:2026.10.4-d48c4b555
    restart: unless-stopped
    environment:
      SEARXNG_SECRET: ${SEARXNG_SECRET:?set SEARXNG_SECRET in .env}
      SEARXNG_BASE_URL: http://searxng:8080/
    volumes:
      - ./deploy/searxng/settings.yml:/etc/searxng/settings.yml:ro
      - searxng-cache:/var/cache/searxng
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 20s
    networks: [internal]

  crawl4ai:
    image: unclecode/crawl4ai:0.9.4
    restart: unless-stopped
    shm_size: 1gb
    environment:
      CRAWL4AI_API_TOKEN: ${CRAWL4AI_API_TOKEN:?set CRAWL4AI_API_TOKEN in .env}
      CRAWL4AI_CHROMIUM_SANDBOX: "true"     # never --no-sandbox (§12, B3); verified in step 3
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://127.0.0.1:11235/health || exit 1"]
      interval: 20s
      timeout: 5s
      retries: 6
      start_period: 40s
    networks: [internal]

networks:
  internal: {}

volumes:
  searxng-cache: {}
```

Healthcheck commands depend on which tools exist in each image. Before committing, check with `docker compose exec searxng sh -c 'command -v wget curl'` and `docker compose exec crawl4ai sh -c 'command -v curl wget python'`, and switch to an available tool if needed. Crawl4AI might also need `CRAWL4AI_CHROMIUM_SANDBOX` to be handled through its config file. Step 3 Task 3.0 settles the sandbox; here it only needs to start.

- [ ] **Step 3: Create `compose.dev.yaml`.** It is for dev only, publishes on loopback only, and uses ephemeral ports.

```yaml
# Dev/test override: publish internal services on ephemeral 127.0.0.1 ports so
# fixture recording and integration tests can reach them. Never used by `make deploy`.
services:
  searxng:
    ports: ["127.0.0.1::8080"]
  crawl4ai:
    ports: ["127.0.0.1::11235"]
```

- [ ] **Step 4: Create `scripts/dev_urls.sh`**

```bash
#!/usr/bin/env bash
# Print export lines for the dev stack's ephemeral URLs. Usage: source <(scripts/dev_urls.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
dc() { docker compose -f compose.yaml -f compose.dev.yaml "$@"; }
echo "export SEARXNG_LIVE_URL=http://$(dc port searxng 8080)"
echo "export CRAWL4AI_LIVE_URL=http://$(dc port crawl4ai 11235)"
```

Make it executable with `chmod +x`. Add `SEARXNG_SECRET=change-me` and `CRAWL4AI_API_TOKEN=change-me` to `.env.example`, each with a comment saying "generate with openssl rand -hex 32".

- [ ] **Step 5: Start the stack and confirm the engine names.** Create a local `.env` from `.env.example` with real random values. It is git-ignored; confirm that with `git check-ignore .env`.

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --wait
source <(scripts/dev_urls.sh)
curl -sf "$SEARXNG_LIVE_URL/config" | python3 -c 'import sys,json;print(sorted(e["name"] for e in json.load(sys.stdin)["engines"] if e["enabled"]))'
```

Adjust `keep_only` so that every intended engine is enabled, then restart searxng with `docker compose ... up -d --wait searxng`.

- [ ] **Step 6: Create `scripts/record_fixtures.py`.** It uses only httpx and is run by hand.

```python
"""Record real upstream responses as test fixtures. Run against the dev stack:

    source <(scripts/dev_urls.sh) && uv run python scripts/record_fixtures.py searxng
"""

import json
import os
import sys
from pathlib import Path

import httpx

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def record_searxng() -> None:
    base = os.environ["SEARXNG_LIVE_URL"]
    out = FIX / "searxng"
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base, timeout=30) as c:
        cfg = c.get("/config").json()
        enabled = sorted(e["name"] for e in cfg["engines"] if e["enabled"])
        (out / "engines_config.json").write_text(json.dumps({"enabled": enabled}, indent=2) + "\n")
        for name, params in {
            "technical_page1": {"q": "python asyncio TaskGroup exception handling",
                                "engines": "github,stackoverflow,mdn,duckduckgo,brave,bing"},
            "general_page1": {"q": "compare open-source vector databases", "categories": "general"},
        }.items():
            r = c.get("/search", params={**params, "format": "json", "language": "en-GB", "pageno": 1})
            r.raise_for_status()
            (out / f"{name}.json").write_text(json.dumps(r.json(), indent=2, sort_keys=True) + "\n")
    print("recorded searxng fixtures in", out)


if __name__ == "__main__":
    {"searxng": record_searxng}[sys.argv[1]]()
```

Step 3 adds a `crawl4ai` recorder to the same dict.

- [ ] **Step 7: Record the fixtures and scrub them.** Run the script, then check for lab-specific data:

```bash
grep -rniE '/home/|toolbox|192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' tests/fixtures/searxng || echo clean
```

Expected: `clean`.

- [ ] **Step 8: Run Verify.** Expected: `True`.

- [ ] **Step 9: Commit**

```bash
git add compose.yaml compose.dev.yaml deploy/searxng/settings.yml .env.example scripts tests/fixtures/searxng
git commit -m "feat(search): minimal compose stack and recorded SearXNG fixtures (V1-01, V1-20)"
```

---

### Task 2.2: Settings and intent presets

**Goal:** Add the pydantic-settings `Settings`, covering every V1 setting so `config.py` isn't churned later, and an `IntentRegistry` loaded from `config/intents.yaml`.

**Files:**
- Modify: `packages/research_engine/pyproject.toml` (deps: `pydantic-settings`, `pyyaml`)
- Create: `packages/research_engine/src/research_engine/config.py`
- Create: `packages/research_engine/src/research_engine/config_files.py`
- Create: `config/intents.yaml`
- Modify: `.env.example` (every setting, documented)
- Test: `tests/service/unit/test_config.py`, `tests/conftest.py`

**Acceptance Criteria:**
- [ ] `Settings()` reads **only** process environment variables, never a `.env` file (`env_file=None`). Compose injects `.env`.
- [ ] `api_key`, `session_secret` and `crawl4ai_api_token` are `SecretStr` with no default. A missing value raises `ValidationError`.
- [ ] `ssrf_allow_hosts` is a comma-separated string. `settings.ssrf_allow_hosts_set` returns a `frozenset` of lowercase hosts.
- [ ] `IntentRegistry.load("config/intents.yaml")` has all 7 intents. Loading a file with an unknown intent or a missing intent raises `ValueError`.
- [ ] Every engine named in `config/intents.yaml` appears in `tests/fixtures/searxng/engines_config.json` → `enabled`. This is enforced by a test.
- [ ] `.env.example` lists every `Settings` field. A test parses `.env.example` keys and compares them with `Settings.model_fields`, ignoring Compose-only keys listed in the test.

**Verify:** `uv run pytest tests/service/unit/test_config.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_config.py
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine_client.models import SearchIntent

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ONLY = {"SEARXNG_SECRET", "LAB_SUBNET", "APP_PORT", "OPENAI_API_KEY",
                "APP_MEM_LIMIT", "APP_CPUS", "SEARXNG_MEM_LIMIT", "SEARXNG_CPUS",
                "CRAWL4AI_MEM_LIMIT", "CRAWL4AI_CPUS", "BACKUP_RETENTION_DAYS"}


def test_requires_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("API_KEY", "SESSION_SECRET", "CRAWL4AI_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_reads_env(settings_env: None) -> None:
    s = Settings()
    assert s.api_key.get_secret_value() == "test-key"
    assert s.site_host == "research.localhost"
    assert s.search_min_results == 10 and s.thin_word_threshold == 150


def test_allow_hosts_parsing(settings_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSRF_ALLOW_HOSTS", " Intranet.Example , docs.local ")
    assert Settings().ssrf_allow_hosts_set == frozenset({"intranet.example", "docs.local"})


def test_env_example_covers_settings() -> None:
    keys = {line.split("=", 1)[0] for line in (ROOT / ".env.example").read_text().splitlines()
            if line and not line.startswith("#") and "=" in line}
    fields = {name.upper() for name in Settings.model_fields}
    assert fields - keys == set(), "add missing settings to .env.example"
    assert keys - fields - COMPOSE_ONLY == set(), "unknown keys in .env.example"


def test_intents_load_all() -> None:
    reg = IntentRegistry.load(ROOT / "config" / "intents.yaml")
    assert set(reg.all()) == set(SearchIntent)
    assert reg.get(SearchIntent.TECHNICAL).engines


def test_intent_engines_exist_in_searxng() -> None:
    enabled = set(json.loads((ROOT / "tests/fixtures/searxng/engines_config.json").read_text())["enabled"])
    reg = IntentRegistry.load(ROOT / "config" / "intents.yaml")
    used = {e for p in reg.all().values() for e in p.engines}
    assert used <= enabled, f"engines not enabled in SearXNG: {sorted(used - enabled)}"


def test_intents_reject_unknown(tmp_path: Path) -> None:
    bad = tmp_path / "i.yaml"
    bad.write_text("intents:\n  nonsense: {categories: [general], engines: [], description: x}\n")
    with pytest.raises(ValueError):
        IntentRegistry.load(bad)
```

Add the shared fixtures to `tests/conftest.py`:

```python
"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest

TEST_ENV = {
    "API_KEY": "test-key",
    "SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "CRAWL4AI_API_TOKEN": "test-crawl-token",
    "SEARXNG_URL": "http://searxng.test:8080",
    "CRAWL4AI_URL": "http://crawl4ai.test:11235",
    "DB_PATH": ":memory:",
}


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    from research_engine.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Add dependencies.** `uv add --package research-engine "pydantic-settings>=2.15.0" "pyyaml>=6.0.3"` and `uv add --dev types-PyYAML`.

- [ ] **Step 4: Implement `config.py`**

```python
"""Service configuration from environment variables (V1-18). Compose injects .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # core / identity
    api_key: SecretStr
    session_secret: SecretStr
    site_host: str = "research.localhost"
    git_sha: str = "unknown"
    log_level: str = "INFO"
    user_agent: str = "ResearchEngine/0.1 (+https://github.com/pentonvillefandango/research-engine)"

    # upstreams
    searxng_url: str = "http://searxng:8080"
    crawl4ai_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: SecretStr

    # config files
    intents_file: Path = Path("config/intents.yaml")
    demos_file: Path = Path("config/demos.yaml")

    # search
    search_min_results: int = Field(default=10, ge=1)
    search_timeout_s: float = Field(default=30.0, gt=0)

    # fetch
    page_timeout_s: int = Field(default=60, ge=1)
    thin_word_threshold: int = Field(default=150, ge=0)
    max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    allowed_content_types: str = "text/html,application/xhtml+xml,application/pdf,text/plain"
    ssrf_allow_hosts: str = ""
    domain_concurrency: int = Field(default=2, ge=1)
    domain_delay_s: float = Field(default=1.0, ge=0)

    # cache
    cache_ttl_search_s: int = Field(default=3600, ge=0)
    cache_ttl_page_s: int = Field(default=86400, ge=0)

    # store / jobs / events
    db_path: str = "data/research-engine.sqlite"
    job_workers: int = Field(default=2, ge=1)
    job_fetch_concurrency: int = Field(default=5, ge=1)
    job_timeout_s: int = Field(default=900, ge=1)
    event_retention_days: int = Field(default=30, ge=1)

    @property
    def ssrf_allow_hosts_set(self) -> frozenset[str]:
        return frozenset(h.strip().lower() for h in self.ssrf_allow_hosts.split(",") if h.strip())

    @property
    def allowed_content_types_set(self) -> frozenset[str]:
        return frozenset(t.strip().lower() for t in self.allowed_content_types.split(",") if t.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
```

- [ ] **Step 5: Implement `config_files.py`**

```python
"""Loaders for YAML config files (intent presets; demos in step 6)."""

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ValidationError

from research_engine_client.models import IntentPreset, SearchIntent


class _IntentsFile(BaseModel):
    intents: dict[SearchIntent, IntentPreset]


class IntentRegistry:
    def __init__(self, presets: dict[SearchIntent, IntentPreset]) -> None:
        missing = set(SearchIntent) - set(presets)
        if missing:
            raise ValueError(f"intents file missing presets for: {sorted(m.value for m in missing)}")
        self._presets = presets

    @classmethod
    def load(cls, path: Path | str) -> Self:
        raw = yaml.safe_load(Path(path).read_text())
        try:
            parsed = _IntentsFile.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"invalid intents file {path}: {exc}") from exc
        return cls(parsed.intents)

    def get(self, intent: SearchIntent) -> IntentPreset:
        return self._presets[intent]

    def all(self) -> dict[SearchIntent, IntentPreset]:
        return dict(self._presets)
```

- [ ] **Step 6: Create `config/intents.yaml`.** Use engine names confirmed in Task 2.1. `engines: []` means "use categories only".

```yaml
# Search intent presets (V1-02). Engine names must be enabled in deploy/searxng/settings.yml.
intents:
  general:
    description: Broad web search across general engines.
    categories: [general]
    engines: []
  technical:
    description: Code, Q&A and developer docs.
    categories: [general, it]
    engines: [github, stackoverflow, mdn, duckduckgo, brave, bing]
  library:
    description: Software packages and their homepages.
    categories: [it, general]
    engines: [pypi, npm, github, duckduckgo, brave]
  product:
    description: Commercial products, vendors and pricing.
    categories: [general]
    engines: [duckduckgo, brave, bing, startpage, qwant]
  standard:
    description: Specifications and standards bodies.
    categories: [general]
    engines: [duckduckgo, brave, bing, wikipedia, mojeek]
  news:
    description: Recent news coverage.
    categories: [news]
    engines: [bing news, duckduckgo news]
  academic:
    description: Papers and scholarly sources.
    categories: [science]
    engines: [arxiv, google scholar, semantic scholar, crossref]
```

- [ ] **Step 7: Rewrite `.env.example`** with every `Settings` field (upper-case), grouped and commented. Secrets get `change-me`; everything else gets its default. Also include the Compose-only keys `SEARXNG_SECRET` and `LAB_SUBNET=127.0.0.1/32`, each with a comment.

- [ ] **Step 8: Run the tests.** Expected: PASS.

- [ ] **Step 9: Commit.** `git commit -m "feat(config): settings and search intent presets (V1-02, V1-18)"`

---

### Task 2.3: Event and cache seams (in-memory)

**Goal:** Define the `EventSink`, `EventSubscriber` and `Cache` protocols, with in-memory implementations and the `Emitter` helper. Every later component uses these. Step 4 adds the SQLite versions.

**Files:**
- Create: `packages/research_engine/src/research_engine/events/{__init__,base,memory}.py`
- Create: `packages/research_engine/src/research_engine/cache/{__init__,base,memory}.py`
- Modify: `packages/research_engine/pyproject.toml` (dep: `structlog`)
- Create: `packages/research_engine/src/research_engine/logging.py`
- Test: `tests/service/unit/test_events_memory.py`, `tests/service/unit/test_cache_memory.py`

**Acceptance Criteria:**
- [ ] `Emitter(sink, job_id="j").info(EventKind.JOB_STARTED, "go", n=1)` emits an `Event` with `level=info`, `job_id="j"`, `data={"n": 1}` and an aware UTC `ts`.
- [ ] `InMemoryEventBus.subscribe()` yields events emitted after subscribing, in order, to every subscriber.
- [ ] A slow subscriber's queue is bounded at 1000. When it overflows, the oldest event is dropped and the emitter is never blocked.
- [ ] Cancelling a subscriber's iteration removes its queue (`bus.subscriber_count == 0`).
- [ ] Every emitted event is also logged through structlog as JSON with `event_kind`, `job_id` and `level`.
- [ ] `InMemoryCache` returns a value before its TTL and `None` after it (tested with an injected clock). `stats()` counts hits and misses.
- [ ] `cache_key("search", model)` is stable across processes: it uses `model_dump_json()` of a frozen model, so key order is deterministic.

**Verify:** `uv run pytest tests/service/unit/test_events_memory.py tests/service/unit/test_cache_memory.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_events_memory.py
import asyncio

from research_engine.events.base import Emitter
from research_engine.events.memory import InMemoryEventBus
from research_engine_client.models import Event, EventKind, EventLevel


async def _collect(bus: InMemoryEventBus, n: int, out: list[Event]) -> None:
    async for ev in bus.subscribe():
        out.append(ev)
        if len(out) == n:
            return


async def test_emitter_builds_event() -> None:
    bus = InMemoryEventBus()
    got: list[Event] = []
    task = asyncio.create_task(_collect(bus, 1, got))
    await asyncio.sleep(0)
    await Emitter(bus, job_id="j").info(EventKind.JOB_STARTED, "go", n=1)
    await asyncio.wait_for(task, 1)
    ev = got[0]
    assert (ev.level, ev.kind, ev.job_id, ev.data) == (EventLevel.INFO, EventKind.JOB_STARTED, "j", {"n": 1})
    assert ev.ts.tzinfo is not None


async def test_fan_out_in_order() -> None:
    bus = InMemoryEventBus()
    a: list[Event] = []
    b: list[Event] = []
    ta = asyncio.create_task(_collect(bus, 3, a))
    tb = asyncio.create_task(_collect(bus, 3, b))
    await asyncio.sleep(0)
    em = Emitter(bus)
    for i in range(3):
        await em.info(EventKind.SYSTEM_HEALTH, f"m{i}")
    await asyncio.wait_for(asyncio.gather(ta, tb), 1)
    assert [e.message for e in a] == [e.message for e in b] == ["m0", "m1", "m2"]


async def test_bounded_queue_drops_oldest() -> None:
    bus = InMemoryEventBus(max_queue=2)
    it = bus.subscribe().__aiter__()
    first = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0)
    em = Emitter(bus)
    for i in range(5):
        await em.info(EventKind.SYSTEM_HEALTH, f"m{i}")
    got = [(await asyncio.wait_for(first, 1)).message]
    got.append((await asyncio.wait_for(it.__anext__(), 1)).message)
    assert got[-1] == "m4"  # newest retained; oldest dropped


async def test_unsubscribe_on_cancel() -> None:
    bus = InMemoryEventBus()
    task = asyncio.create_task(_collect(bus, 99, []))
    await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert bus.subscriber_count == 0
```

```python
# tests/service/unit/test_cache_memory.py
from research_engine.cache.base import cache_key
from research_engine.cache.memory import InMemoryCache
from research_engine_client.models import SearchRequest


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_ttl_and_stats() -> None:
    clock = Clock()
    cache = InMemoryCache(clock=clock)
    assert await cache.get("k") is None
    await cache.set("k", b"v", ttl_s=10)
    assert await cache.get("k") == b"v"
    clock.t += 11
    assert await cache.get("k") is None
    s = cache.stats()
    assert (s.hits, s.misses) == (1, 2)


def test_cache_key_stable() -> None:
    a = cache_key("search", SearchRequest(query="x", engines=("b", "a")))
    b = cache_key("search", SearchRequest(query="x", engines=("b", "a")))
    assert a == b and len(a) == 64
    assert cache_key("fetch", "https://a.example") != cache_key("search", "https://a.example")


def test_cache_key_ignores_use_cache_flag() -> None:
    assert cache_key("search", SearchRequest(query="x", use_cache=False)) == \
        cache_key("search", SearchRequest(query="x", use_cache=True))
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `events/base.py`, `events/memory.py` and `logging.py`**

```python
# events/base.py
"""Event seam: every step emits structured events (V1-15)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from research_engine_client.models import Event, EventKind, EventLevel

_log = structlog.get_logger("research_engine.events")


class EventSink(Protocol):
    async def emit(self, event: Event) -> None: ...


class EventSubscriber(Protocol):
    def subscribe(self) -> AsyncIterator[Event]: ...


class Emitter:
    def __init__(self, sink: EventSink, job_id: str | None = None) -> None:
        self._sink = sink
        self.job_id = job_id

    def bind(self, job_id: str | None) -> "Emitter":
        return Emitter(self._sink, job_id)

    async def _emit(self, level: EventLevel, kind: EventKind, message: str, data: dict[str, Any]) -> None:
        event = Event(ts=datetime.now(UTC), job_id=self.job_id, level=level, kind=kind,
                      message=message, data=data)
        _log.log(_LEVELS[level], message, event_kind=kind.value, job_id=self.job_id, level=level.value, **data)
        await self._sink.emit(event)

    async def debug(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.DEBUG, kind, message, data)

    async def info(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.INFO, kind, message, data)

    async def warning(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.WARNING, kind, message, data)

    async def error(self, kind: EventKind, message: str, **data: Any) -> None:
        await self._emit(EventLevel.ERROR, kind, message, data)


_LEVELS = {EventLevel.DEBUG: 10, EventLevel.INFO: 20, EventLevel.WARNING: 30, EventLevel.ERROR: 40}
```

```python
# events/memory.py
"""In-process pub/sub with bounded, drop-oldest subscriber queues."""

import asyncio
from collections.abc import AsyncIterator

from research_engine_client.models import Event


class InMemoryEventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._max = max_queue
        self._subs: set[asyncio.Queue[Event]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    async def emit(self, event: Event) -> None:
        for q in list(self._subs):
            if q.full():
                q.get_nowait()  # drop oldest; never block the emitter
            q.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max)
        self._subs.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.discard(q)
```

Note: `subscribe` must register its queue **before** the first `await`. An async generator body only starts on the first `__anext__`, so the tests call `asyncio.sleep(0)` after starting the consumer. Document this in the docstring.

```python
# logging.py
"""structlog JSON logging to stdout (V1-15)."""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
```

- [ ] **Step 4: Implement `cache/base.py` and `cache/memory.py`**

```python
# cache/base.py
"""Cache seam (V1-10). Keys are sha256 of kind + canonical request JSON."""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl_s: int) -> None: ...
    def stats(self) -> CacheStats: ...


def cache_key(kind: str, payload: BaseModel | str) -> str:
    if isinstance(payload, BaseModel):
        body = payload.model_dump_json(exclude={"use_cache"})
    else:
        body = payload
    return hashlib.sha256(f"{kind}\x00{body}".encode()).hexdigest()
```

```python
# cache/memory.py
"""Process-local TTL cache, used in tests and before step 4."""

import time
from collections.abc import Callable

from .base import CacheStats


class InMemoryCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[float, bytes]] = {}
        self._stats = CacheStats()

    async def get(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None or item[0] <= self._clock():
            self._data.pop(key, None)
            self._stats.misses += 1
            return None
        self._stats.hits += 1
        return item[1]

    async def set(self, key: str, value: bytes, ttl_s: int) -> None:
        self._data[key] = (self._clock() + ttl_s, value)

    def stats(self) -> CacheStats:
        return self._stats
```

- [ ] **Step 5: Run the tests.** Expected: PASS.

- [ ] **Step 6: Commit.** `git commit -m "feat(core): in-memory event bus, emitter, cache seam and JSON logging (V1-10, V1-15)"`

---

### Task 2.4: URL canonicalisation and result ranking

**Goal:** Write the pure functions that canonicalise URLs and merge and score hits across engines and pages.

**Files:**
- Create: `packages/research_engine/src/research_engine/pipeline/{__init__,urls,ranking}.py`
- Create: `packages/research_engine/src/research_engine/adapters/{__init__,search}.py` (`RawHit` and `RawSearchPage` dataclasses plus the `SearchProvider` protocol, needed by ranking)
- Test: `tests/service/unit/test_urls.py`, `tests/service/unit/test_ranking.py`

**Acceptance Criteria:**
- [ ] `canonicalize_url` does all of the following:
  - lowercases the scheme and host;
  - drops the fragment, default ports and tracking params (`utm_*`, `gclid`, `fbclid`, `mc_cid`, `mc_eid`, `ref`, `ref_src`);
  - sorts the remaining query params;
  - removes a trailing `/` except at the root;
  - keeps `http` and `https` distinct;
  - leaves path case unchanged;
  - drops `www.` **only** for domain grouping (`domain_of`), not in the canonical URL.
- [ ] `merge_and_score` merges hits with equal canonical URLs across pages, unions their engines and positions, and keeps the first non-empty title and snippet and the first non-null `published`.
- [ ] Score = `Σ_positions 1/(60+p)` × `(1 + 0.1 × (len(engines) − 1))`, rounded to 6 dp. Results are sorted by score descending, then minimum position, then canonical URL. `rank` is 1..n, truncated to `max_results`.
- [ ] A hit found by 3 engines at positions 5, 6 and 7 outranks a hit found by 1 engine at position 1.

**Verify:** `uv run pytest tests/service/unit/test_urls.py tests/service/unit/test_ranking.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_urls.py
import pytest

from research_engine.pipeline.urls import canonicalize_url, domain_of


@pytest.mark.parametrize(("raw", "expected"), [
    ("HTTPS://Example.COM/Path/?b=2&a=1#frag", "https://example.com/Path?a=1&b=2"),
    ("https://example.com:443/x/", "https://example.com/x"),
    ("http://example.com:80/", "http://example.com/"),
    ("https://example.com/?utm_source=x&utm_medium=y&id=3&gclid=z", "https://example.com/?id=3"),
    ("https://example.com/a?ref=hn&fbclid=1", "https://example.com/a"),
    ("https://www.example.com/a", "https://www.example.com/a"),
    ("https://example.com", "https://example.com/"),
])
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


def test_domain_of_strips_www() -> None:
    assert domain_of("https://WWW.Example.com/x") == "example.com"
```

```python
# tests/service/unit/test_ranking.py
from research_engine.adapters.search import RawHit, RawSearchPage
from research_engine.pipeline.ranking import merge_and_score


def hit(url: str, engines: list[str], positions: list[int], title: str = "t") -> RawHit:
    return RawHit(url=url, title=title, content="c", engines=engines, positions=positions, published=None)


def page(*hits: RawHit) -> RawSearchPage:
    return RawSearchPage(hits=list(hits), suggestions=[], infoboxes=[], unresponsive=[])


def test_agreement_beats_single_top_rank() -> None:
    res = merge_and_score([page(hit("https://solo.example/", ["a"], [1]),
                                hit("https://agreed.example/", ["a", "b", "c"], [5, 6, 7]))], 10)
    assert [r.domain for r in res] == ["agreed.example", "solo.example"]
    assert res[0].rank == 1 and res[0].engines == ["a", "b", "c"]


def test_merge_across_pages_and_dedupe_canonical() -> None:
    res = merge_and_score([
        page(hit("https://x.example/a/?utm_source=1", ["a"], [2], title="")),
        page(hit("https://x.example/a", ["b"], [12], title="Real title")),
    ], 10)
    assert len(res) == 1
    assert res[0].engines == ["a", "b"] and res[0].title == "Real title"
    expected = round((1 / 62 + 1 / 72) * 1.1, 6)
    assert res[0].score == expected


def test_truncates_and_ranks() -> None:
    hits = [hit(f"https://e{i}.example/", ["a"], [i]) for i in range(1, 6)]
    res = merge_and_score([page(*hits)], 3)
    assert [r.rank for r in res] == [1, 2, 3]
    assert [r.domain for r in res] == ["e1.example", "e2.example", "e3.example"]
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `adapters/search.py`**

```python
"""SearchProvider seam (V1-01). Concrete providers live in sibling modules."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from research_engine_client.models import Infobox, TimeRange, UnresponsiveEngine


@dataclass
class RawHit:
    url: str
    title: str
    content: str
    engines: list[str]
    positions: list[int]          # already offset by (pageno-1)*10
    published: datetime | None


@dataclass
class RawSearchPage:
    hits: list[RawHit]
    suggestions: list[str]
    infoboxes: list[Infobox]
    unresponsive: list[UnresponsiveEngine]


class SearchProvider(Protocol):
    async def search(self, query: str, *, categories: list[str], engines: list[str],
                     language: str, time_range: TimeRange | None, pageno: int) -> RawSearchPage: ...

    async def health(self) -> bool: ...
```

- [ ] **Step 4: Implement `pipeline/urls.py`**

```python
"""URL canonicalisation for dedupe and cache keys."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = {"gclid", "fbclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid", "yclid", "msclkid"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in _TRACKING_EXACT


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _is_tracking(k)))
    return urlunsplit((scheme, netloc, path, query, ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")
```

- [ ] **Step 5: Implement `pipeline/ranking.py`**

```python
"""Merge, dedupe and score hits across engines and pages (V1-01)."""

from dataclasses import dataclass, field
from datetime import datetime

from research_engine.adapters.search import RawSearchPage
from research_engine_client.models import SearchResult

from .urls import canonicalize_url, domain_of

RRF_K = 60
AGREEMENT_BONUS = 0.1


@dataclass
class _Acc:
    url: str
    title: str = ""
    snippet: str = ""
    engines: list[str] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)
    published: datetime | None = None


def merge_and_score(pages: list[RawSearchPage], max_results: int) -> list[SearchResult]:
    acc: dict[str, _Acc] = {}
    for page in pages:
        for h in page.hits:
            canon = canonicalize_url(h.url)
            a = acc.setdefault(canon, _Acc(url=h.url))
            a.title = a.title or h.title
            a.snippet = a.snippet or h.content
            a.published = a.published or h.published
            a.engines.extend(e for e in h.engines if e not in a.engines)
            a.positions.extend(h.positions)
    scored = []
    for canon, a in acc.items():
        base = sum(1 / (RRF_K + p) for p in a.positions)
        score = round(base * (1 + AGREEMENT_BONUS * (max(len(a.engines), 1) - 1)), 6)
        scored.append((score, min(a.positions, default=10**6), canon, a))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [
        SearchResult(rank=i, url=a.url, canonical_url=canon, title=a.title, snippet=a.snippet,
                     domain=domain_of(a.url), engines=a.engines, score=score, published_at=a.published)
        for i, (score, _, canon, a) in enumerate(scored[:max_results], start=1)
    ]
```

`published_at` must be timezone-aware. The adapter (Task 2.5) guarantees this.

- [ ] **Step 6: Run the tests.** Expected: PASS.

- [ ] **Step 7: Commit.** `git commit -m "feat(search): URL canonicalisation and cross-engine ranking (V1-01)"`

---

### Task 2.5: SearXNG adapter

**Goal:** Implement `SearxngProvider`, which turns SearXNG's JSON API into a `RawSearchPage` with typed errors. It is tested against the recorded fixtures with respx.

**Files:**
- Create: `packages/research_engine/src/research_engine/errors.py`
- Create: `packages/research_engine/src/research_engine/adapters/searxng.py`
- Modify: `packages/research_engine/pyproject.toml` (dep: `httpx`)
- Test: `tests/service/unit/test_searxng.py`

**Acceptance Criteria:**
- [ ] The request is `GET {base}/search` with `format=json`, `q`, `language` and `pageno`, plus `safesearch=0`. It sends `engines` (comma-joined) when engines are given, otherwise `categories`. `time_range` is sent only when set.
- [ ] Parsing the recorded `technical_page1.json` yields more than 0 hits. Each hit has non-empty `engines`, `positions` offset by `(pageno-1)*10`, and an aware-or-None `published`.
- [ ] `unresponsive_engines` `[[name, msg], ...]` maps to `UnresponsiveEngine(engine=name, error=msg)`.
- [ ] A timeout raises `ServiceError` with `code=upstream_timeout`, `retryable=True`, `source="searxng"` and `http_status=504`. An HTTP 5xx raises `upstream_error` (retryable, 502). An HTTP 403 (JSON disabled) raises `upstream_error`, not retryable, with a message mentioning `search.formats`.
- [ ] `health()` returns True on a 200 from `/healthz` and False on any error. It never raises.

**Verify:** `uv run pytest tests/service/unit/test_searxng.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_searxng.py
import json
from pathlib import Path

import httpx
import pytest
import respx

from research_engine.adapters.searxng import SearxngProvider
from research_engine.errors import ServiceError
from research_engine_client.models import ErrorCode, TimeRange

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "searxng"
BASE = "http://searxng.test:8080"


@pytest.fixture
async def provider() -> SearxngProvider:
    return SearxngProvider(BASE, httpx.AsyncClient(), timeout_s=5)


@respx.mock
async def test_params_with_engines(provider: SearxngProvider) -> None:
    route = respx.get(f"{BASE}/search").respond(json=json.loads((FIX / "technical_page1.json").read_text()))
    page = await provider.search("q", categories=["it"], engines=["github", "stackoverflow"],
                                 language="en-GB", time_range=TimeRange.MONTH, pageno=2)
    params = dict(route.calls.last.request.url.params)
    assert params == {"q": "q", "format": "json", "language": "en-GB", "pageno": "2",
                      "safesearch": "0", "engines": "github,stackoverflow", "time_range": "month"}
    assert page.hits and all(h.engines for h in page.hits)
    assert min(p for h in page.hits for p in h.positions) >= 11
    assert all(h.published is None or h.published.tzinfo for h in page.hits)


@respx.mock
async def test_categories_when_no_engines(provider: SearxngProvider) -> None:
    route = respx.get(f"{BASE}/search").respond(json={"results": [], "unresponsive_engines": [["bing", "timeout"]]})
    page = await provider.search("q", categories=["general"], engines=[], language="en-GB",
                                 time_range=None, pageno=1)
    params = dict(route.calls.last.request.url.params)
    assert params["categories"] == "general" and "engines" not in params and "time_range" not in params
    assert page.unresponsive[0].engine == "bing" and page.unresponsive[0].error == "timeout"


@respx.mock
async def test_timeout_maps_to_typed_error(provider: SearxngProvider) -> None:
    respx.get(f"{BASE}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ServiceError) as ei:
        await provider.search("q", categories=[], engines=[], language="en-GB", time_range=None, pageno=1)
    d = ei.value.detail
    assert (d.code, d.retryable, d.source, ei.value.http_status) == (ErrorCode.UPSTREAM_TIMEOUT, True, "searxng", 504)


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(503, True), (403, False)])
async def test_http_errors(provider: SearxngProvider, status: int, retryable: bool) -> None:
    respx.get(f"{BASE}/search").respond(status)
    with pytest.raises(ServiceError) as ei:
        await provider.search("q", categories=[], engines=[], language="en-GB", time_range=None, pageno=1)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_ERROR and ei.value.detail.retryable is retryable
    if status == 403:
        assert "search.formats" in ei.value.detail.message


@respx.mock
async def test_health(provider: SearxngProvider) -> None:
    respx.get(f"{BASE}/healthz").respond(200)
    assert await provider.health() is True
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ConnectError("down"))
    assert await provider.health() is False
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `errors.py`**

```python
"""Typed service errors carrying an ErrorDetail and HTTP status."""

from research_engine_client.models import ErrorCode, ErrorDetail


class ServiceError(Exception):
    def __init__(self, detail: ErrorDetail, http_status: int = 502) -> None:
        super().__init__(detail.message)
        self.detail = detail
        self.http_status = http_status

    @classmethod
    def of(cls, code: ErrorCode, message: str, *, retryable: bool, source: str | None = None,
           http_status: int = 502) -> "ServiceError":
        return cls(ErrorDetail(code=code, message=message, retryable=retryable, source=source), http_status)
```

- [ ] **Step 4: Implement `adapters/searxng.py`**

```python
"""SearXNG JSON API adapter (V1-01, V1-03). AGPL service, called over HTTP only (D14)."""

from datetime import UTC, datetime
from typing import Any

import httpx

from research_engine.errors import ServiceError
from research_engine_client.models import ErrorCode, Infobox, InfoboxLink, TimeRange, UnresponsiveEngine

from .search import RawHit, RawSearchPage

PAGE_SIZE = 10


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class SearxngProvider:
    def __init__(self, base_url: str, client: httpx.AsyncClient, timeout_s: float) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout_s

    async def search(self, query: str, *, categories: list[str], engines: list[str],
                     language: str, time_range: TimeRange | None, pageno: int) -> RawSearchPage:
        params: dict[str, str] = {"q": query, "format": "json", "language": language,
                                  "pageno": str(pageno), "safesearch": "0"}
        if engines:
            params["engines"] = ",".join(engines)
        else:
            params["categories"] = ",".join(categories)
        if time_range is not None:
            params["time_range"] = time_range.value
        try:
            resp = await self._client.get(f"{self._base}/search", params=params, timeout=self._timeout)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ServiceError.of(ErrorCode.UPSTREAM_TIMEOUT, f"SearXNG timed out: {exc}",
                                  retryable=True, source="searxng", http_status=504) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            msg = (f"SearXNG returned {status}"
                   + ("; enable json in search.formats" if status == 403 else ""))
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, msg, retryable=status >= 500,
                                  source="searxng") from exc
        except httpx.HTTPError as exc:
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, f"SearXNG unreachable: {exc}",
                                  retryable=True, source="searxng") from exc
        return self._parse(resp.json(), pageno)

    @staticmethod
    def _parse(body: dict[str, Any], pageno: int) -> RawSearchPage:
        offset = (pageno - 1) * PAGE_SIZE
        hits = []
        for r in body.get("results", []):
            url = r.get("url")
            if not url:
                continue
            engines = list(r.get("engines") or ([r["engine"]] if r.get("engine") else []))
            positions = [int(p) + offset for p in (r.get("positions") or [1])]
            hits.append(RawHit(url=url, title=r.get("title") or "", content=r.get("content") or "",
                               engines=engines, positions=positions,
                               published=_parse_dt(r.get("publishedDate"))))
        infoboxes = [
            Infobox(title=i.get("infobox") or "", content=i.get("content"), engine=i.get("engine"),
                    urls=[InfoboxLink(title=u.get("title", ""), url=u["url"])
                          for u in i.get("urls") or [] if u.get("url")])
            for i in body.get("infoboxes", [])
        ]
        unresponsive = [UnresponsiveEngine(engine=str(e[0]), error=str(e[1]))
                        for e in body.get("unresponsive_engines", []) if len(e) >= 2]
        return RawSearchPage(hits=hits, suggestions=list(body.get("suggestions", [])),
                             infoboxes=infoboxes, unresponsive=unresponsive)

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base}/healthz", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200
```

- [ ] **Step 5: Run the tests.** Expected: PASS. If a fixture field differs from this shape (for example, `positions` is missing), adjust the parser to match the **recorded** reality and add a test for that case.

- [ ] **Step 6: Commit.** `git commit -m "feat(search): SearXNG adapter with typed errors (V1-01, V1-03)"`

---

### Task 2.6: SearchService: presets, depth, broader retry, events and cache

**Goal:** Orchestrate a search: apply the preset and depth, fetch pages concurrently, merge, retry broader when too few results come back, record unresponsive engines, emit events, and cache the result.

**Files:**
- Create: `packages/research_engine/src/research_engine/pipeline/search.py`
- Test: `tests/service/unit/test_search_service.py`

**Acceptance Criteria:**
- [ ] `depth` maps to page counts quick=1, standard=2, deep=3. Pages are fetched concurrently.
- [ ] `req.engines` overrides the preset's engines. The preset's categories are always passed.
- [ ] If the merged results number fewer than `min(settings.search_min_results, req.max_results)`, the intent isn't `general` and `req.engines` is None, then the service emits `search.retry_broader` once, fetches page 1 of the `general` preset, and merges.
- [ ] Unresponsive engines are deduplicated by name across pages. Each one emits `search.engine_failed` (warning). If one page request raises `ServiceError`, the response still returns the other pages, with `UnresponsiveEngine(engine="searxng", ...)`. If every page fails, the first `ServiceError` is re-raised.
- [ ] Cache: on a hit, returns `(resp, True)`, emits `cache.hit` and doesn't call the provider. `use_cache=False` skips the read but still writes.
- [ ] Emits `search.started` and `search.done` (`data: {results: n, engines: k}`), carrying the passed `job_id`.

**Verify:** `uv run pytest tests/service/unit/test_search_service.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.** They use a fake provider, which is simpler and clearer than respx at this layer.

```python
# tests/service/unit/test_search_service.py
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from research_engine.adapters.search import RawHit, RawSearchPage
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.errors import ServiceError
from research_engine.pipeline.search import SearchService
from research_engine_client.models import (
    ErrorCode, Event, EventKind, SearchDepth, SearchIntent, SearchRequest, UnresponsiveEngine,
)

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class Call:
    categories: list[str]
    engines: list[str]
    pageno: int


@dataclass
class FakeProvider:
    per_call_hits: int = 6
    fail_pages: set[int] = field(default_factory=set)
    calls: list[Call] = field(default_factory=list)

    async def search(self, query, *, categories, engines, language, time_range, pageno):  # noqa: ANN001
        self.calls.append(Call(categories, engines, pageno))
        if pageno in self.fail_pages:
            raise ServiceError.of(ErrorCode.UPSTREAM_TIMEOUT, "slow", retryable=True, source="searxng")
        tag = "g" if not engines else engines[0]
        hits = [RawHit(url=f"https://{tag}{pageno}-{i}.example/", title="t", content="c",
                       engines=[tag], positions=[i + (pageno - 1) * 10], published=None)
                for i in range(1, self.per_call_hits + 1)]
        return RawSearchPage(hits=hits, suggestions=["s"], infoboxes=[],
                             unresponsive=[UnresponsiveEngine(engine="bing", error="captcha")])

    async def health(self) -> bool:
        return True


class Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
def svc_parts(settings_env: None) -> tuple[FakeProvider, Sink, InMemoryCache, SearchService]:
    provider, sink, cache = FakeProvider(), Sink(), InMemoryCache()
    svc = SearchService(provider, IntentRegistry.load(ROOT / "config/intents.yaml"), cache, sink, Settings())
    return provider, sink, cache, svc


async def test_depth_pages_and_preset(svc_parts) -> None:  # noqa: ANN001
    provider, sink, _, svc = svc_parts
    resp, hit = await svc.search(SearchRequest(query="q", intent=SearchIntent.TECHNICAL,
                                               depth=SearchDepth.STANDARD), job_id="j1")
    assert not hit and sorted(c.pageno for c in provider.calls) == [1, 2]
    assert provider.calls[0].engines[0] == "github"
    assert len(resp.results) == 12
    kinds = [e.kind for e in sink.events]
    assert kinds[0] is EventKind.SEARCH_STARTED and kinds[-1] is EventKind.SEARCH_DONE
    assert all(e.job_id == "j1" for e in sink.events)
    assert [u.engine for u in resp.unresponsive_engines] == ["bing"]
    assert kinds.count(EventKind.SEARCH_ENGINE_FAILED) == 1


async def test_broader_retry_when_thin(svc_parts) -> None:  # noqa: ANN001
    provider, sink, _, svc = svc_parts
    provider.per_call_hits = 2
    resp, _ = await svc.search(SearchRequest(query="q", intent=SearchIntent.NEWS, depth=SearchDepth.QUICK))
    assert EventKind.SEARCH_RETRY_BROADER in [e.kind for e in sink.events]
    assert provider.calls[-1].engines == [] and provider.calls[-1].categories == ["general"]
    assert len(resp.results) == 4


async def test_no_retry_when_engines_overridden(svc_parts) -> None:  # noqa: ANN001
    provider, sink, _, svc = svc_parts
    provider.per_call_hits = 1
    await svc.search(SearchRequest(query="q", intent=SearchIntent.NEWS, engines=("x",), depth=SearchDepth.QUICK))
    assert EventKind.SEARCH_RETRY_BROADER not in [e.kind for e in sink.events]


async def test_partial_page_failure(svc_parts) -> None:  # noqa: ANN001
    provider, _, _, svc = svc_parts
    provider.fail_pages = {2}
    resp, _ = await svc.search(SearchRequest(query="q", depth=SearchDepth.STANDARD))
    assert len(resp.results) == 6
    assert "searxng" in [u.engine for u in resp.unresponsive_engines]


async def test_all_pages_fail_raises(svc_parts) -> None:  # noqa: ANN001
    provider, _, _, svc = svc_parts
    provider.fail_pages = {1, 2, 3}
    with pytest.raises(ServiceError):
        await svc.search(SearchRequest(query="q", depth=SearchDepth.DEEP))


async def test_cache_hit_and_bypass(svc_parts) -> None:  # noqa: ANN001
    provider, sink, _, svc = svc_parts
    req = SearchRequest(query="q", depth=SearchDepth.QUICK)
    await svc.search(req)
    n = len(provider.calls)
    resp, hit = await svc.search(req)
    assert hit and len(provider.calls) == n and EventKind.CACHE_HIT in [e.kind for e in sink.events]
    _, hit2 = await svc.search(req.model_copy(update={"use_cache": False}))
    assert not hit2 and len(provider.calls) == n + 1
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `pipeline/search.py`**

```python
"""Search orchestration: presets, depth, broader retry, events, cache (V1-01..V1-03)."""

import asyncio

from research_engine.adapters.search import RawSearchPage, SearchProvider
from research_engine.cache.base import Cache, cache_key
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter, EventSink
from research_engine_client.models import (
    EventKind, SearchDepth, SearchIntent, SearchRequest, SearchResponse, UnresponsiveEngine,
)

from .ranking import merge_and_score

PAGES_FOR_DEPTH = {SearchDepth.QUICK: 1, SearchDepth.STANDARD: 2, SearchDepth.DEEP: 3}


class SearchService:
    def __init__(self, provider: SearchProvider, intents: IntentRegistry, cache: Cache,
                 events: EventSink, settings: Settings) -> None:
        self._provider = provider
        self._intents = intents
        self._cache = cache
        self._events = events
        self._settings = settings

    async def search(self, req: SearchRequest, *, job_id: str | None = None) -> tuple[SearchResponse, bool]:
        em = Emitter(self._events, job_id)
        key = cache_key("search", req)
        if req.use_cache and (cached := await self._cache.get(key)) is not None:
            await em.info(EventKind.CACHE_HIT, f"search cache hit: {req.query}", query=req.query)
            return SearchResponse.model_validate_json(cached), True

        preset = self._intents.get(req.intent)
        engines = list(req.engines) if req.engines else preset.engines
        await em.info(EventKind.SEARCH_STARTED, f"search: {req.query}", query=req.query,
                      intent=req.intent.value, engines=engines)
        pages, failures = await self._fetch_pages(req, preset.categories, engines,
                                                  range(1, PAGES_FOR_DEPTH[req.depth] + 1))
        if not pages:
            raise failures[0]
        results = merge_and_score(pages, req.max_results)

        threshold = min(self._settings.search_min_results, req.max_results)
        if len(results) < threshold and req.intent is not SearchIntent.GENERAL and not req.engines:
            await em.info(EventKind.SEARCH_RETRY_BROADER, f"only {len(results)} results; retrying broader",
                          found=len(results), threshold=threshold)
            general = self._intents.get(SearchIntent.GENERAL)
            more, more_fail = await self._fetch_pages(req, general.categories, general.engines, [1])
            pages += more
            failures += more_fail
            results = merge_and_score(pages, req.max_results)

        unresponsive = self._unresponsive(pages, failures)
        for u in unresponsive:
            await em.warning(EventKind.SEARCH_ENGINE_FAILED, f"engine {u.engine} failed: {u.error}",
                             engine=u.engine, error=u.error)
        resp = SearchResponse(
            query=req.query, results=results,
            suggestions=list(dict.fromkeys(s for p in pages for s in p.suggestions)),
            infoboxes=[i for p in pages for i in p.infoboxes],
            unresponsive_engines=unresponsive,
        )
        await self._cache.set(key, resp.model_dump_json().encode(), self._settings.cache_ttl_search_s)
        await em.info(EventKind.SEARCH_DONE, f"{len(results)} results for: {req.query}",
                      results=len(results), engines=len({e for r in results for e in r.engines}))
        return resp, False

    async def _fetch_pages(self, req: SearchRequest, categories: list[str], engines: list[str],
                           pagenos: range | list[int]) -> tuple[list[RawSearchPage], list[ServiceError]]:
        outcomes = await asyncio.gather(*(
            self._provider.search(req.query, categories=categories, engines=engines,
                                  language=req.language, time_range=req.time_range, pageno=p)
            for p in pagenos), return_exceptions=True)
        pages = [o for o in outcomes if isinstance(o, RawSearchPage)]
        failures = [o for o in outcomes if isinstance(o, ServiceError)]
        for o in outcomes:
            if isinstance(o, BaseException) and not isinstance(o, ServiceError):
                raise o
        return pages, failures

    @staticmethod
    def _unresponsive(pages: list[RawSearchPage], failures: list[ServiceError]) -> list[UnresponsiveEngine]:
        seen: dict[str, UnresponsiveEngine] = {}
        for p in pages:
            for u in p.unresponsive:
                seen.setdefault(u.engine, u)
        if failures:
            seen.setdefault("searxng", UnresponsiveEngine(engine="searxng", error=failures[0].detail.message))
        return sorted(seen.values(), key=lambda u: u.engine)
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat(search): search service with presets, broader retry, events and cache (V1-01..V1-03, V1-10)"`

---

### Task 2.7: FastAPI app, envelope, API-key auth, `/v1/search` and `/v1/engines`

**Goal:** Build the composition root `create_app()`, add the request-context and API-key middleware and the envelope exception handlers, and expose the first two endpoints, with OpenAPI examples.

**Files:**
- Modify: `packages/research_engine/pyproject.toml` (deps: `fastapi`, `uvicorn[standard]`)
- Create: `packages/research_engine/src/research_engine/app.py`
- Create: `packages/research_engine/src/research_engine/api/{__init__,deps,envelope,auth,search}.py`
- Modify: `tests/conftest.py` (add `app` and `client` fixtures with fake services)
- Test: `tests/service/unit/test_api_search.py`, `tests/service/unit/test_auth.py`
- Test: `tests/integration/test_search_live.py`

**Acceptance Criteria:**
- [ ] `POST /v1/search` without `X-API-Key` returns 401 with an envelope error `unauthorized`. A wrong key also returns 401. The check uses `hmac.compare_digest`.
- [ ] `POST /v1/search` with a valid key returns 200 with `Envelope[SearchResponse]`:
  - `meta.request_id` is a 32-hex value, also echoed in the `X-Request-ID` response header;
  - `meta.took_ms` is at least 0;
  - `meta.cache_hit` is false on the first call and true on the second.
- [ ] An invalid body (blank query) returns 422 with an envelope error `invalid_request`, whose message includes the field path.
- [ ] A `ServiceError` from the service returns its `http_status` with the envelope `errors=[detail]` and `data=null`.
- [ ] An unexpected exception returns 500 with `internal_error`. The traceback is logged but never returned.
- [ ] `GET /v1/engines` returns `Envelope[EnginesResponse]`, built from the intent registry: engines sorted, each with the intents that use it.
- [ ] `/openapi.json` includes a request example for `/v1/search`.
- [ ] Integration test (`-m integration`, needs the dev stack): `intent=technical` for `"python asyncio TaskGroup exception handling"` returns at least 10 results, and the union of `engines` across results has at least 3 entries.

**Verify:** `uv run pytest tests/service/unit/test_api_search.py tests/service/unit/test_auth.py -v` → all pass; `source <(scripts/dev_urls.sh) && uv run pytest -m integration tests/integration/test_search_live.py -v` → pass

**Steps:**

- [ ] **Step 1: Add the shared fixtures to `tests/conftest.py`.** Append:

```python
from collections.abc import AsyncIterator

import httpx


@pytest.fixture
async def app(settings_env: None):  # noqa: ANN201
    from research_engine.app import create_app
    from research_engine.config import Settings
    from research_engine.testing import build_test_services
    settings = Settings()
    services = build_test_services(settings)
    application = create_app(settings, services=services)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:  # noqa: ANN001
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://research.localhost",
                                 headers={"X-API-Key": "test-key"}) as c:
        yield c
```

`research_engine/testing.py` (a production module, so tests and examples can share it) provides `build_test_services(settings)`. It wires a `FakeSearchProvider` that returns deterministic hits, `InMemoryCache` and `InMemoryEventBus`. Steps 3–5 extend it. Its fake provider returns 12 hits from engines `["a", "b"]`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/service/unit/test_auth.py
import httpx

from research_engine_client.models import ErrorCode


async def test_missing_key_401(app) -> None:  # noqa: ANN001
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/search", json={"query": "x"})
    assert r.status_code == 401
    body = r.json()
    assert body["data"] is None and body["errors"][0]["code"] == ErrorCode.UNAUTHORIZED


async def test_wrong_key_401(app) -> None:  # noqa: ANN001
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"X-API-Key": "nope"}) as c:
        r = await c.post("/v1/search", json={"query": "x"})
    assert r.status_code == 401


async def test_openapi_is_public(app) -> None:  # noqa: ANN001
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/openapi.json")
    assert r.status_code == 200
    assert "example" in str(r.json()["paths"]["/v1/search"]["post"]["requestBody"])
```

```python
# tests/service/unit/test_api_search.py
import re

import httpx

from research_engine.errors import ServiceError
from research_engine_client.models import Envelope, EnginesResponse, ErrorCode, SearchResponse


async def test_search_envelope_and_cache(client: httpx.AsyncClient) -> None:
    r1 = await client.post("/v1/search", json={"query": "vector db", "depth": "quick"})
    assert r1.status_code == 200
    env = Envelope[SearchResponse].model_validate(r1.json())
    assert env.data is not None and len(env.data.results) > 0
    assert re.fullmatch(r"[0-9a-f]{32}", env.meta.request_id)
    assert r1.headers["x-request-id"] == env.meta.request_id
    assert env.meta.cache_hit is False
    r2 = await client.post("/v1/search", json={"query": "vector db", "depth": "quick"})
    assert r2.json()["meta"]["cache_hit"] is True


async def test_validation_error_envelope(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/search", json={"query": "  "})
    assert r.status_code == 422
    err = r.json()["errors"][0]
    assert err["code"] == ErrorCode.INVALID_REQUEST and "query" in err["message"]


async def test_service_error_envelope(app, client: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    async def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise ServiceError.of(ErrorCode.UPSTREAM_TIMEOUT, "slow", retryable=True, source="searxng",
                              http_status=504)
    monkeypatch.setattr(app.state.services.search, "search", boom)
    r = await client.post("/v1/search", json={"query": "x"})
    assert r.status_code == 504 and r.json()["errors"][0]["source"] == "searxng"


async def test_unexpected_error_hidden(app, client: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    async def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("secret internals")
    monkeypatch.setattr(app.state.services.search, "search", boom)
    r = await client.post("/v1/search", json={"query": "x"})
    assert r.status_code == 500
    assert r.json()["errors"][0]["code"] == ErrorCode.INTERNAL_ERROR
    assert "secret internals" not in r.text


async def test_engines(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/engines")
    env = Envelope[EnginesResponse].model_validate(r.json())
    assert env.data is not None
    names = [e.name for e in env.data.engines]
    assert names == sorted(names) and "github" in names
    assert len(env.data.intents) == 7
```

```python
# tests/integration/test_search_live.py
import os

import httpx
import pytest

from research_engine.adapters.searxng import SearxngProvider
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.search import SearchService
from research_engine_client.models import SearchIntent, SearchRequest

pytestmark = pytest.mark.integration


async def test_technical_query_meets_acceptance(settings_env: None) -> None:
    url = os.environ["SEARXNG_LIVE_URL"]
    async with httpx.AsyncClient() as http:
        svc = SearchService(SearxngProvider(url, http, 30), IntentRegistry.load("config/intents.yaml"),
                            InMemoryCache(), InMemoryEventBus(), Settings())
        resp, _ = await svc.search(SearchRequest(query="python asyncio TaskGroup exception handling",
                                                 intent=SearchIntent.TECHNICAL, use_cache=False))
    engines = {e for r in resp.results for e in r.engines}
    assert len(resp.results) >= 10, resp.unresponsive_engines
    assert len(engines) >= 3, engines
```

Integration tests read `SEARXNG_LIVE_URL` and `CRAWL4AI_LIVE_URL` (printed by `scripts/dev_urls.sh`), not `SEARXNG_URL`. The `settings_env` fixture overwrites `SEARXNG_URL` with a fake test value, so the separate names keep the two from colliding.

- [ ] **Step 3: Run them.** Expected: FAIL (import error).

- [ ] **Step 4: Implement the API modules**

```python
# api/deps.py
"""Service container hung on app.state (composition root fills it)."""

from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request

from research_engine.cache.base import Cache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.search import SearchService


@dataclass
class Services:
    settings: Settings
    intents: IntentRegistry
    events: InMemoryEventBus          # replaced by SqliteEventBus in step 4 (same interface)
    cache: Cache
    search: SearchService
    http: httpx.AsyncClient | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def get_services(request: Request) -> Services:
    return request.app.state.services
```

```python
# api/envelope.py
"""Envelope helpers and exception handlers: every response is Envelope[T]."""

import time
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from research_engine.errors import ServiceError
from research_engine_client.models import Envelope, ErrorCode, ErrorDetail, Meta

_log = structlog.get_logger("research_engine.api")


class RequestContextMiddleware:
    """Assigns request_id + start time; echoes X-Request-ID."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        scope["state"]["started"] = time.perf_counter()

        async def send_wrapper(message: dict) -> None:  # type: ignore[type-arg]
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _meta(request: Request, cache_hit: bool = False) -> Meta:
    started = getattr(request.state, "started", time.perf_counter())
    rid = getattr(request.state, "request_id", uuid.uuid4().hex)
    return Meta(request_id=rid, took_ms=max(0, int((time.perf_counter() - started) * 1000)),
                cache_hit=cache_hit)


def ok[T](request: Request, data: T, *, cache_hit: bool = False) -> Envelope[T]:
    return Envelope[T](data=data, meta=_meta(request, cache_hit))


def error_response(request: Request, status: int, *errors: ErrorDetail) -> JSONResponse:
    env = Envelope[None](data=None, meta=_meta(request), errors=list(errors))
    return JSONResponse(status_code=status, content=env.model_dump(mode="json"))


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return error_response(request, exc.http_status, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [ErrorDetail(code=ErrorCode.INVALID_REQUEST,
                               message=f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}",
                               retryable=False) for e in exc.errors()]
        return error_response(request, 422, *details)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        _log.exception("unhandled error", path=request.url.path)
        return error_response(request, 500, ErrorDetail(code=ErrorCode.INTERNAL_ERROR,
                                                        message="internal error", retryable=False))
```

```python
# api/auth.py
"""API-key auth for REST and MCP (§8). GUI session cookie support is added in step 6."""

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

from research_engine_client.models import SCHEMA_VERSION, ErrorCode

OPEN_PATHS = ("/health", "/version", "/openapi.json", "/docs", "/redoc", "/login", "/static/")


class ApiKeyMiddleware:
    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self.app = app
        self._key = api_key.encode()

    def _is_open(self, path: str) -> bool:
        return any(path == p or (p.endswith("/") and path.startswith(p)) for p in OPEN_PATHS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_open(scope["path"]):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"x-api-key", b"")
        if supplied and hmac.compare_digest(supplied, self._key):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"data": None, "errors": [{"code": ErrorCode.UNAUTHORIZED.value,
                           "message": "missing or invalid API key", "retryable": False, "source": None}],
                           "meta": {"request_id": scope.get("state", {}).get("request_id", ""),
                                    "schema_version": SCHEMA_VERSION, "took_ms": 0, "cache_hit": False}}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b'ApiKey header="X-API-Key"')]})
        await send({"type": "http.response.body", "body": body})
```

```python
# api/search.py
"""POST /v1/search and GET /v1/engines (V1-01, V1-02, V1-18)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from research_engine_client.models import (
    EngineInfo, EnginesResponse, Envelope, SearchRequest, SearchResponse,
)

from .deps import Services, get_services
from .envelope import ok

router = APIRouter(prefix="/v1", tags=["search"])

SEARCH_EXAMPLE = {"query": "compare open-source vector databases", "intent": "technical",
                  "max_results": 20, "depth": "standard"}


@router.post("/search", response_model=Envelope[SearchResponse])
async def search(request: Request,
                 req: Annotated[SearchRequest, Body(openapi_examples={"technical": {"value": SEARCH_EXAMPLE}})],
                 services: Annotated[Services, Depends(get_services)]) -> Envelope[SearchResponse]:
    resp, hit = await services.search.search(req)
    return ok(request, resp, cache_hit=hit)


@router.get("/engines", response_model=Envelope[EnginesResponse])
async def engines(request: Request, services: Annotated[Services, Depends(get_services)]) -> Envelope[EnginesResponse]:
    presets = services.intents.all()
    used: dict[str, list] = {}
    for intent, preset in presets.items():
        for e in preset.engines:
            used.setdefault(e, []).append(intent)
    data = EnginesResponse(engines=[EngineInfo(name=n, used_by=sorted(v)) for n, v in sorted(used.items())],
                           intents=presets)
    return ok(request, data)
```

```python
# app.py
"""Composition root: wires concrete adapters to protocols (Global Constraint 4)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from research_engine import __version__
from research_engine.adapters.searxng import SearxngProvider
from research_engine.api import search as search_api
from research_engine.api.auth import ApiKeyMiddleware
from research_engine.api.deps import Services
from research_engine.api.envelope import RequestContextMiddleware, install_exception_handlers
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings, get_settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.logging import configure_logging
from research_engine.pipeline.search import SearchService


def build_services(settings: Settings) -> Services:
    http = httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, follow_redirects=False)
    intents = IntentRegistry.load(settings.intents_file)
    events = InMemoryEventBus()
    cache = InMemoryCache()
    provider = SearxngProvider(settings.searxng_url, http, settings.search_timeout_s)
    return Services(settings=settings, intents=intents, events=events, cache=cache,
                    search=SearchService(provider, intents, cache, events, settings), http=http)


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.services = services or build_services(settings)
        try:
            yield
        finally:
            if app.state.services.http is not None:
                await app.state.services.http.aclose()

    app = FastAPI(title="Research Engine", version=__version__, lifespan=lifespan)
    install_exception_handlers(app)
    app.include_router(search_api.router)
    # Middleware order: last added runs first. RequestContext must wrap auth.
    app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key.get_secret_value())
    app.add_middleware(RequestContextMiddleware)
    return app
```

`research_engine/testing.py`:

```python
"""Deterministic fakes for tests, examples and the GUI demo mode. No network."""

from research_engine.adapters.search import RawHit, RawSearchPage
from research_engine.api.deps import Services
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.config_files import IntentRegistry
from research_engine.events.memory import InMemoryEventBus
from research_engine.pipeline.search import SearchService


class FakeSearchProvider:
    async def search(self, query, *, categories, engines, language, time_range, pageno):  # noqa: ANN001
        hits = [RawHit(url=f"https://result{i}.example/{pageno}", title=f"{query} {i}", content="snippet",
                       engines=["a", "b"] if i % 2 else ["a"], positions=[i + (pageno - 1) * 10],
                       published=None) for i in range(1, 13)]
        return RawSearchPage(hits=hits, suggestions=[], infoboxes=[], unresponsive=[])

    async def health(self) -> bool:
        return True


def build_test_services(settings: Settings) -> Services:
    intents = IntentRegistry.load(settings.intents_file)
    events, cache = InMemoryEventBus(), InMemoryCache()
    return Services(settings=settings, intents=intents, events=events, cache=cache,
                    search=SearchService(FakeSearchProvider(), intents, cache, events, settings))
```

`settings.intents_file` is relative, so `settings_env` also runs `monkeypatch.chdir(ROOT)`. Add `ROOT = Path(__file__).resolve().parent.parent` and `monkeypatch.chdir(ROOT)` to the fixture in `tests/conftest.py`.

- [ ] **Step 5: Run the unit tests.** Expected: PASS.

- [ ] **Step 6: Run the integration test** against the dev stack. Expected: PASS. If fewer than 3 engines respond, inspect `unresponsive_engines` and adjust the `technical` preset or the SearXNG engine list. Don't lower the acceptance threshold.

- [ ] **Step 7: Run the full step check** (Global Constraint 11).

- [ ] **Step 8: Commit.** `git commit -m "feat(api): app factory, envelope, API-key auth, /v1/search and /v1/engines (V1-01, V1-02, V1-14, V1-18)"`

---

**End of step 2:** request code review, report to the owner with evidence (unit and integration output), and **stop** for approval. After approval, run `git push`.
