# Step 3: Fetch pipeline

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:** V1-04, V1-05, V1-06, V1-11, §8 (SSRF, size limits, content-type allow-list, retries).

**Outcome:** `POST /v1/fetch` returns a `Document`. The pipeline works like this:
1. A static fetch with httpx comes first.
2. HTML goes through Trafilatura for text and through the table, link and extruct extractors. PDFs go through pypdf.
3. If the static result is thin, looks JS-rendered or failed, the pipeline escalates to Crawl4AI's sandboxed browser.

Every hop is checked by the SSRF guard, robots.txt and the per-domain politeness limits.

**Task order:**
- 3.0 runs first.
- 3.1, 3.2, 3.4 and 3.5 touch disjoint files and may run in parallel.
- 3.3 needs 3.1.
- 3.6 needs 3.1.
- 3.7 needs everything before it.

---

### Task 3.0: Spike. Crawl4AI with the Chromium sandbox on (B3)

**Goal:** Prove that Crawl4AI 0.9.4 renders pages **with Chromium's sandbox on** under this host's Docker. If it doesn't, apply the minimal per-service seccomp fallback. Record the outcome as ADR-0022 and record the Crawl4AI fixtures.

> **Spike rules:** the throwaway exploration happens on the running dev stack, and only the settled configuration is committed. If neither path works, **stop and ask the owner**, per decision B3. Never fall back to `--no-sandbox`.

**Files:**
- Modify: `compose.yaml` (the `crawl4ai` service: sandbox env, and `security_opt` only if the fallback is needed)
- Create (only if the fallback is needed): `deploy/crawl4ai/seccomp-chromium.json`
- Create: `deploy/crawl4ai/config.yml`, only if the env var alone doesn't remove `--no-sandbox`. It is mounted read-only over `/app/config.yml`.
- Modify: `docs/adr/0022-chromium-sandbox.md` (outcome section)
- Modify: `scripts/record_fixtures.py` (add a `crawl4ai` recorder)
- Create: `tests/fixtures/crawl4ai/crawl_example.json`

**Acceptance Criteria:**
- [ ] On the running stack, `docker compose exec crawl4ai sh -c 'cat /proc/*/cmdline 2>/dev/null | tr "\0" " " | grep -c -- "--no-sandbox"'` prints `0` while a crawl is in flight.
- [ ] A `POST /crawl` for `https://example.com` returns `success: true` with non-empty `markdown`.
- [ ] Neither `privileged: true` nor `cap_add: [SYS_ADMIN]` is set anywhere in `compose.yaml`.
- [ ] ADR-0022 records which path worked (default profiles, or the custom seccomp profile), the evidence commands, and the date.
- [ ] `tests/fixtures/crawl4ai/crawl_example.json` is a real `/crawl` response. It passes the `grep` scrub from Task 2.1.

**Verify:** `scripts/check_sandbox.sh` → `{"sandbox":"on","no_sandbox_procs":0,"crawl_ok":true}`

**Steps:**

- [ ] **Step 1: Write the check script first (red).** `scripts/check_sandbox.sh`:

```bash
#!/usr/bin/env bash
# Verifies Crawl4AI renders with Chromium's sandbox ON. Prints one JSON line; exit 0 only if both hold.
set -euo pipefail
cd "$(dirname "$0")/.."
source <(scripts/dev_urls.sh)
TOKEN=$(grep -E '^CRAWL4AI_API_TOKEN=' .env | cut -d= -f2-)
dc() { docker compose -f compose.yaml -f compose.dev.yaml "$@"; }
# start a crawl in the background so Chromium is running while we inspect processes
curl -s -m 90 -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"],"crawler_config":{"type":"CrawlerRunConfig","params":{"cache_mode":"bypass"}}}' \
  "$CRAWL4AI_LIVE_URL/crawl" > /tmp/re-crawl.json &
pid=$!
procs=0
for _ in $(seq 1 30); do
  n=$(dc exec -T crawl4ai sh -c 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f"; echo; done 2>/dev/null' | grep -c chrom || true)
  if [ "$n" -gt 0 ]; then
    procs=$(dc exec -T crawl4ai sh -c 'for f in /proc/[0-9]*/cmdline; do tr "\0" " " < "$f"; echo; done 2>/dev/null' | grep -c -- '--no-sandbox' || true)
    break
  fi
  sleep 1
done
wait "$pid"
ok=$(python3 -c 'import json;d=json.load(open("/tmp/re-crawl.json"));print(str(bool(d["results"][0]["success"] and d["results"][0]["markdown"])).lower())')
rm -f /tmp/re-crawl.json
sandbox=$([ "$procs" -eq 0 ] && echo on || echo off)
echo "{\"sandbox\":\"$sandbox\",\"no_sandbox_procs\":$procs,\"crawl_ok\":$ok}"
[ "$procs" -eq 0 ] && [ "$ok" = true ]
```

Run it with the current compose file. Record the output, whether red or green.

- [ ] **Step 2: Path A (default profiles).** With `CRAWL4AI_CHROMIUM_SANDBOX: "true"` already set, check whether `--no-sandbox` is gone and the crawl succeeds.
  - If the env var isn't honoured, copy the image's `/app/config.yml` with `docker compose cp crawl4ai:/app/config.yml deploy/crawl4ai/config.yml`. Remove `--no-sandbox` from `crawler.browser.extra_args` and mount it `:ro` over `/app/config.yml`.
  - Restart with `up -d --wait crawl4ai` and re-run the check.

- [ ] **Step 3: If path A fails, try path B (seccomp fallback).** The usual symptoms are Chromium logging `No usable sandbox!` or `Failed to move to new namespace`.
  1. Start from Docker's default seccomp profile (`https://github.com/moby/profiles/blob/main/seccomp/default.json`).
  2. Add only what Chromium's namespace sandbox needs: allow `clone`, `unshare` and `setns` with namespace flags.
  3. Save it as `deploy/crawl4ai/seccomp-chromium.json` and set `security_opt: ["seccomp=./deploy/crawl4ai/seccomp-chromium.json"]` on `crawl4ai` only.
  4. If AppArmor `docker-default` also blocks it (check `dmesg | grep apparmor` via the owner, because it needs sudo), **stop and ask the owner** before going further.

- [ ] **Step 4: If both fail, stop.** Report the evidence to the owner, per B3.

- [ ] **Step 5: Record the fixture.** Add to `scripts/record_fixtures.py`:

```python
def record_crawl4ai() -> None:
    base = os.environ["CRAWL4AI_LIVE_URL"]
    token = os.environ["CRAWL4AI_API_TOKEN"]
    out = FIX / "crawl4ai"
    out.mkdir(parents=True, exist_ok=True)
    body = {"urls": ["https://example.com"],
            "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
            "crawler_config": {"type": "CrawlerRunConfig", "params": {"cache_mode": "bypass"}}}
    r = httpx.post(f"{base}/crawl", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=120)
    r.raise_for_status()
    data = r.json()
    for res in data.get("results", []):
        res.pop("screenshot", None)
        res.pop("pdf", None)
    (out / "crawl_example.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print("recorded crawl4ai fixture in", out)
```

Register it as `"crawl4ai": record_crawl4ai`. Run it with `CRAWL4AI_API_TOKEN` exported from `.env` in your shell only. Never write the token to a file. Scrub the fixture with the Task 2.1 grep.

- [ ] **Step 6: Fill in ADR-0022's "Outcome" section** with the path taken, the verbatim `check_sandbox.sh` output and the date.

- [ ] **Step 7: Commit**

```bash
git add compose.yaml deploy/crawl4ai scripts tests/fixtures/crawl4ai docs/adr/0022-chromium-sandbox.md
git commit -m "feat(fetch): Crawl4AI with Chromium sandbox enabled, verified (V1-04, §12)"
```

---

### Task 3.1: SSRF guard

**Goal:** `SsrfGuard.check(url)` rejects any URL whose host is, or resolves to, a non-public address, unless the host is allow-listed.

**Files:**
- Create: `packages/research_engine/src/research_engine/safety/{__init__,ssrf}.py`
- Test: `tests/service/unit/test_ssrf.py`

**Acceptance Criteria:**
- [ ] Blocked when the host is, or any resolved address is, in any of these:
  - loopback (`127.0.0.0/8`, `::1`);
  - private (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`);
  - link-local (`169.254/16`, `fe80::/10`), which includes cloud metadata at `169.254.169.254`;
  - CGNAT (`100.64/10`);
  - multicast, reserved, unspecified (`0.0.0.0`, `::`);
  - IPv4-mapped IPv6 forms of all of the above;
  - the `localhost` name and any `*.localhost`.
- [ ] A literal-IP host is checked without DNS.
- [ ] A host in `allow_hosts`, matched case-insensitively and exactly, skips the check.
- [ ] Schemes other than http and https are blocked. A DNS failure raises `fetch_failed` (retryable), not `ssrf_blocked`.
- [ ] The raised `ServiceError` has `code=ssrf_blocked`, `retryable=False`, `http_status=403` and `source=<url>`.
- [ ] The resolver is injectable, and the tests use a fake resolver with no real DNS.

**Verify:** `uv run pytest tests/service/unit/test_ssrf.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_ssrf.py
import pytest

from research_engine.errors import ServiceError
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode


def resolver(table: dict[str, list[str]]):  # noqa: ANN201
    async def _resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]
    return _resolve


GUARD = SsrfGuard(frozenset({"intranet.example"}), resolver=resolver({
    "public.example": ["93.184.216.34"],
    "rebind.example": ["93.184.216.34", "10.0.0.5"],
    "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "intranet.example": ["10.1.2.3"],
}))


async def test_public_allowed() -> None:
    await GUARD.check("https://public.example/page")
    await GUARD.check("https://v6.example/")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://[::1]/", "http://10.0.0.1/", "http://172.16.5.4/",
    "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "http://100.64.0.1/",
    "http://0.0.0.0/", "http://[fd00::1]/", "http://[fe80::1]/", "http://224.0.0.1/",
    "http://localhost:8000/", "http://foo.localhost/", "https://rebind.example/",
    "https://mapped.example/", "ftp://public.example/",
])
async def test_blocked(url: str) -> None:
    with pytest.raises(ServiceError) as ei:
        await GUARD.check(url)
    d = ei.value.detail
    assert d.code is ErrorCode.SSRF_BLOCKED and d.retryable is False and ei.value.http_status == 403


async def test_allow_list_bypasses() -> None:
    await GUARD.check("http://Intranet.Example/wiki")


async def test_dns_failure_is_fetch_failed() -> None:
    with pytest.raises(ServiceError) as ei:
        await GUARD.check("https://nope.example/")
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and ei.value.detail.retryable
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `safety/ssrf.py`**

```python
"""SSRF protection (§8): block fetches to non-public addresses unless allow-listed.

Residual risk: DNS can change between this check and the connection (rebinding).
Mitigated by checking every redirect hop and by Crawl4AI's own internal-URL guard; see ADR-0026.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from research_engine.errors import ServiceError
from research_engine_client.models import ErrorCode

Resolver = Callable[[str], Awaitable[list[str]]]

_EXTRA_BLOCKED = [ipaddress.ip_network("100.64.0.0/10")]


async def _system_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_blocked_ip(raw: str) -> bool:
    ip = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or any(ip in n for n in _EXTRA_BLOCKED))


class SsrfGuard:
    def __init__(self, allow_hosts: frozenset[str], resolver: Resolver | None = None) -> None:
        self._allow = frozenset(h.lower() for h in allow_hosts)
        self._resolve = resolver or _system_resolve

    @staticmethod
    def _blocked(url: str, why: str) -> ServiceError:
        return ServiceError.of(ErrorCode.SSRF_BLOCKED, f"blocked: {why}", retryable=False,
                               source=url, http_status=403)

    async def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise self._blocked(url, f"scheme {parts.scheme!r} not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            raise self._blocked(url, "missing host")
        if host in self._allow:
            return
        if host == "localhost" or host.endswith(".localhost"):
            raise self._blocked(url, "localhost")
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = await self._resolve(host)
            except OSError as exc:
                raise ServiceError.of(ErrorCode.FETCH_FAILED, f"DNS lookup failed for {host}: {exc}",
                                      retryable=True, source=url) from exc
        bad = [a for a in addresses if _is_blocked_ip(a)]
        if bad:
            raise self._blocked(url, f"{host} resolves to non-public address {bad[0]}")
```

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat(safety): SSRF guard for private, loopback and metadata ranges (§8, V1-11)"`

---

### Task 3.2: Per-domain limiter and robots.txt policy

**Goal:** Add the `DomainLimiter` (per-domain concurrency and minimum spacing between requests) and the `RobotsPolicy` (Protego, cached per origin, RFC 9309 failure semantics, crawl-delay honoured).

**Files:**
- Create: `packages/research_engine/src/research_engine/safety/{limiter,robots}.py`
- Modify: `packages/research_engine/pyproject.toml` (dep: `protego`)
- Test: `tests/service/unit/test_limiter.py`, `tests/service/unit/test_robots.py`

**Acceptance Criteria:**
- [ ] `DomainLimiter(concurrency=1, delay_s=0.05)`: two `slot()`s for the same domain start at least 0.05 s apart. Slots for different domains don't wait on each other.
- [ ] `limiter.set_delay(domain, s)` raises the delay for that domain only, capped at 30 s.
- [ ] `RobotsPolicy.check(url)` behaves as follows:
  - It fetches `scheme://host[:port]/robots.txt` once per origin per TTL (default 3600 s), checks it with the `SsrfGuard`, and doesn't follow redirects off-origin.
  - A 2xx response is parsed with Protego (in `to_thread`).
  - A 4xx response means allow all.
  - A 5xx response or a network error means disallow all, cached for only 600 s (RFC 9309 §2.3.1.4).
  - A disallowed URL raises `ServiceError(code=robots_disallowed, retryable=False, http_status=403)`.
  - `Crawl-delay` for our user-agent token is applied via `limiter.set_delay`.
- [ ] The user-agent token for robots matching is the product token of `settings.user_agent`, i.e. everything before the first `/`.
- [ ] It emits `robots.fetched` on a fetch and `robots.disallowed` on a block, via an optional `Emitter` argument.

**Verify:** `uv run pytest tests/service/unit/test_limiter.py tests/service/unit/test_robots.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_limiter.py
import asyncio
import time

from research_engine.safety.limiter import DomainLimiter


async def test_spacing_same_domain() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=0.05)
    starts: list[float] = []

    async def go() -> None:
        async with lim.slot("https://a.example/x"):
            starts.append(time.monotonic())

    await asyncio.gather(go(), go())
    assert starts[1] - starts[0] >= 0.045


async def test_different_domains_parallel() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=0.5)
    t0 = time.monotonic()

    async def go(u: str) -> None:
        async with lim.slot(u):
            pass

    await asyncio.gather(go("https://a.example/"), go("https://b.example/"))
    assert time.monotonic() - t0 < 0.2


def test_set_delay_capped() -> None:
    lim = DomainLimiter(concurrency=1, delay_s=1)
    lim.set_delay("a.example", 99)
    assert lim.delay_for("a.example") == 30
    assert lim.delay_for("b.example") == 1
```

```python
# tests/service/unit/test_robots.py
import httpx
import pytest
import respx

from research_engine.errors import ServiceError
from research_engine.safety.limiter import DomainLimiter
from research_engine.safety.robots import RobotsPolicy
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode

UA = "ResearchEngine/0.1 (+https://github.com/pentonvillefandango/research-engine)"
ROBOTS = "User-agent: *\nDisallow: /private\n\nUser-agent: ResearchEngine\nDisallow: /nobots\nCrawl-delay: 5\n"


async def _pub(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def policy() -> tuple[RobotsPolicy, DomainLimiter]:
    lim = DomainLimiter(concurrency=1, delay_s=1)
    return RobotsPolicy(httpx.AsyncClient(), UA, lim, SsrfGuard(frozenset(), resolver=_pub)), lim


@respx.mock
async def test_allow_disallow_and_crawl_delay(policy) -> None:  # noqa: ANN001
    pol, lim = policy
    route = respx.get("https://a.example/robots.txt").respond(200, text=ROBOTS)
    await pol.check("https://a.example/public")
    with pytest.raises(ServiceError) as ei:
        await pol.check("https://a.example/nobots/page")
    assert ei.value.detail.code is ErrorCode.ROBOTS_DISALLOWED and ei.value.http_status == 403
    assert route.call_count == 1                      # cached per origin
    assert lim.delay_for("a.example") == 5


@respx.mock
async def test_4xx_allows_all(policy) -> None:  # noqa: ANN001
    pol, _ = policy
    respx.get("https://b.example/robots.txt").respond(404)
    await pol.check("https://b.example/anything")


@respx.mock
async def test_5xx_disallows_all(policy) -> None:  # noqa: ANN001
    pol, _ = policy
    respx.get("https://c.example/robots.txt").respond(503)
    with pytest.raises(ServiceError):
        await pol.check("https://c.example/x")
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Add the dependency.** `uv add --package research-engine "protego>=0.7.0"`.

- [ ] **Step 4: Implement `safety/limiter.py`**

```python
"""Per-domain politeness: bounded concurrency and minimum spacing (V1-11)."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from research_engine.pipeline.urls import domain_of

MAX_DELAY_S = 30.0


class DomainLimiter:
    def __init__(self, concurrency: int, delay_s: float) -> None:
        self._concurrency = concurrency
        self._default_delay = delay_s
        self._delays: dict[str, float] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    def delay_for(self, domain: str) -> float:
        return self._delays.get(domain, self._default_delay)

    def set_delay(self, domain: str, delay_s: float) -> None:
        self._delays[domain] = min(max(delay_s, self._default_delay), MAX_DELAY_S)

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        domain = domain_of(url)
        sem = self._sems.setdefault(domain, asyncio.Semaphore(self._concurrency))
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with sem:
            async with lock:  # serialise the spacing decision per domain
                wait = self._last.get(domain, 0.0) + self.delay_for(domain) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last[domain] = time.monotonic()
            yield
```

- [ ] **Step 5: Implement `safety/robots.py`**

```python
"""robots.txt policy with Protego (V1-11). Failure semantics follow RFC 9309."""

import asyncio
import time
from urllib.parse import urlsplit

import httpx
from protego import Protego

from research_engine.errors import ServiceError
from research_engine.events.base import Emitter
from research_engine.pipeline.urls import domain_of
from research_engine_client.models import ErrorCode, EventKind

from .limiter import DomainLimiter
from .ssrf import SsrfGuard

_ALLOW_ALL = Protego.parse("")
_DISALLOW_ALL = Protego.parse("User-agent: *\nDisallow: /\n")
ERROR_TTL_S = 600


class RobotsPolicy:
    def __init__(self, client: httpx.AsyncClient, user_agent: str, limiter: DomainLimiter,
                 guard: SsrfGuard, ttl_s: int = 3600) -> None:
        self._client = client
        self._ua = user_agent
        self._token = user_agent.split("/", 1)[0].strip()
        self._limiter = limiter
        self._guard = guard
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, Protego]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def check(self, url: str, em: Emitter | None = None) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rules = await self._rules(origin, em)
        if not rules.can_fetch(url, self._token):
            if em:
                await em.warning(EventKind.ROBOTS_DISALLOWED, f"robots.txt disallows {url}", url=url)
            raise ServiceError.of(ErrorCode.ROBOTS_DISALLOWED, f"robots.txt disallows {url}",
                                  retryable=False, source=url, http_status=403)

    async def _rules(self, origin: str, em: Emitter | None) -> Protego:
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._cache.get(origin)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            rules, ttl, status = await self._fetch(origin)
            self._cache[origin] = (time.monotonic() + ttl, rules)
            delay = rules.crawl_delay(self._token)
            if delay:
                self._limiter.set_delay(domain_of(origin), float(delay))
            if em:
                await em.debug(EventKind.ROBOTS_FETCHED, f"robots.txt for {origin}: {status}",
                               origin=origin, status=status)
            return rules

    async def _fetch(self, origin: str) -> tuple[Protego, int, str]:
        robots_url = f"{origin}/robots.txt"
        await self._guard.check(robots_url)
        try:
            resp = await self._client.get(robots_url, headers={"User-Agent": self._ua},
                                          timeout=15, follow_redirects=False)
        except httpx.HTTPError as exc:
            return _DISALLOW_ALL, ERROR_TTL_S, f"error {type(exc).__name__}"
        if resp.is_redirect:
            return _ALLOW_ALL, self._ttl, "redirect (treated as allow)"
        if 200 <= resp.status_code < 300:
            return await asyncio.to_thread(Protego.parse, resp.text), self._ttl, str(resp.status_code)
        if 400 <= resp.status_code < 500:
            return _ALLOW_ALL, self._ttl, str(resp.status_code)
        return _DISALLOW_ALL, ERROR_TTL_S, str(resp.status_code)
```

Redirects: following an on-origin redirect is allowed by RFC 9309, but V1 keeps it simple and treats a redirect as allow. Record this in a code comment and in ADR-0026.

- [ ] **Step 6: Run the tests.** Expected: PASS.

- [ ] **Step 7: Commit.** `git commit -m "feat(safety): per-domain limiter and robots.txt policy (V1-11)"`

---

### Task 3.3: Retry helper and static fetcher

**Goal:** Add a small async retry-with-backoff helper (also applied to the SearXNG adapter), and `StaticFetcher`, which streams with a size cap, enforces the content-type allow-list, and follows redirects manually with an SSRF check on every hop.

**Files:**
- Create: `packages/research_engine/src/research_engine/retry.py`
- Create: `packages/research_engine/src/research_engine/adapters/{fetch,static_fetch}.py`
- Modify: `packages/research_engine/src/research_engine/adapters/searxng.py` (wrap the request in `retry`)
- Test: `tests/service/unit/test_retry.py`, `tests/service/unit/test_static_fetch.py`

**Acceptance Criteria:**
- [ ] `retry(fn, attempts=3, base_delay_s=0.5)` retries only `ServiceError`s with `retryable=True`, sleeping `base × 2^i` with ±20% jitter, and re-raises the last error. The tests inject a no-op sleep.
- [ ] `StaticFetcher.fetch(url, timeout_s)`:
  - follows up to 5 redirects, calling `guard.check` on each hop;
  - fails with `fetch_failed` on the 6th redirect or a redirect loop;
  - records the hops in `RawPage.redirects`.
- [ ] A `Content-Type` that isn't in the allow-list raises `content_type_not_allowed` (not retryable, 415). A missing content type is sniffed: `%PDF-` means PDF, `<html` or `<!doctype html` means HTML, anything else is rejected.
- [ ] A body larger than `max_response_bytes` (by `Content-Length` or while streaming) raises `response_too_large` (not retryable, 413) and stops reading.
- [ ] A status of 429 or 5xx raises `fetch_failed` (retryable). Other 4xx statuses raise `fetch_failed` (not retryable). Timeouts raise `upstream_timeout`. `RawPage.status` holds the final status for 2xx.
- [ ] HTML bodies are decoded using the charset from the header, falling back to `<meta charset>`, then UTF-8 with `errors="replace"`. PDFs leave `html=None`.

**Verify:** `uv run pytest tests/service/unit/test_retry.py tests/service/unit/test_static_fetch.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_retry.py
import pytest

from research_engine.errors import ServiceError
from research_engine.retry import retry
from research_engine_client.models import ErrorCode


async def _nosleep(_: float) -> None:
    return None


async def test_retries_then_succeeds() -> None:
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, "x", retryable=True)
        return "ok"

    assert await retry(flaky, attempts=3, sleep=_nosleep) == "ok" and calls == 3


async def test_non_retryable_raises_immediately() -> None:
    calls = 0

    async def bad() -> None:
        nonlocal calls
        calls += 1
        raise ServiceError.of(ErrorCode.SSRF_BLOCKED, "x", retryable=False)

    with pytest.raises(ServiceError):
        await retry(bad, attempts=3, sleep=_nosleep)
    assert calls == 1
```

```python
# tests/service/unit/test_static_fetch.py
import httpx
import pytest
import respx

from research_engine.adapters.static_fetch import StaticFetcher
from research_engine.errors import ServiceError
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, FetchMethod


async def _resolve(host: str) -> list[str]:
    return {"evil.example": ["10.0.0.1"]}.get(host, ["93.184.216.34"])


@pytest.fixture
def fetcher() -> StaticFetcher:
    return StaticFetcher(httpx.AsyncClient(), SsrfGuard(frozenset(), resolver=_resolve),
                         max_bytes=1000, allowed_types=frozenset({"text/html", "application/pdf"}),
                         user_agent="UA/1")


@respx.mock
async def test_html_ok(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/").respond(200, html="<html><body>hé</body></html>",
                                            headers={"content-type": "text/html; charset=utf-8"})
    page = await fetcher.fetch("https://a.example/", timeout_s=5)
    assert page.method is FetchMethod.STATIC and page.status == 200 and "hé" in (page.html or "")


@respx.mock
async def test_redirect_to_private_blocked(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/r").respond(302, headers={"location": "http://evil.example/admin"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/r", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@respx.mock
async def test_redirect_chain_recorded(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/1").respond(301, headers={"location": "/2"})
    respx.get("https://a.example/2").respond(200, html="<html></html>", headers={"content-type": "text/html"})
    page = await fetcher.fetch("https://a.example/1", timeout_s=5)
    assert page.final_url == "https://a.example/2" and page.redirects == ["https://a.example/1"]


@respx.mock
async def test_too_many_redirects(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/loop").respond(302, headers={"location": "/loop"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/loop", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED


@respx.mock
async def test_too_large(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/big").respond(200, content=b"x" * 5000, headers={"content-type": "text/html"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/big", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.RESPONSE_TOO_LARGE and ei.value.http_status == 413


@respx.mock
async def test_content_type_rejected(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/z").respond(200, content=b"PK..", headers={"content-type": "application/zip"})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/z", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.CONTENT_TYPE_NOT_ALLOWED


@respx.mock
async def test_pdf_sniffed_without_header(fetcher: StaticFetcher) -> None:
    respx.get("https://a.example/f").respond(200, content=b"%PDF-1.7\n...")
    page = await fetcher.fetch("https://a.example/f", timeout_s=5)
    assert page.content_type == "application/pdf" and page.html is None


@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (404, False)])
async def test_status_errors(fetcher: StaticFetcher, status: int, retryable: bool) -> None:
    respx.get("https://a.example/s").respond(status)
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://a.example/s", timeout_s=5)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and ei.value.detail.retryable is retryable
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `retry.py`**

```python
"""Retry with exponential backoff for retryable ServiceErrors (§8 Reliability)."""

import asyncio
import random
from collections.abc import Awaitable, Callable

from research_engine.errors import ServiceError


async def retry[T](fn: Callable[[], Awaitable[T]], *, attempts: int = 3, base_delay_s: float = 0.5,
                   sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> T:
    for i in range(attempts):
        try:
            return await fn()
        except ServiceError as exc:
            if not exc.detail.retryable or i == attempts - 1:
                raise
            await sleep(base_delay_s * 2**i * random.uniform(0.8, 1.2))  # noqa: S311 - jitter, not crypto
    raise AssertionError("unreachable")
```

Apply it in `SearxngProvider.search`: move the request into an inner `async def _once()` and `return self._parse((await retry(_once, attempts=2)).json(), pageno)`. Keep `test_searxng.py` green. The timeout test now expects 2 calls, so assert `route.call_count == 2` there.

- [ ] **Step 4: Implement `adapters/fetch.py`** (the protocol and `RawPage`, exactly as in the contract)

```python
"""Fetcher seam (V1-04). Concrete: StaticFetcher, Crawl4AIFetcher."""

from dataclasses import dataclass, field
from typing import Protocol

from research_engine_client.models import FetchMethod


@dataclass
class RawPage:
    url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    html: str | None
    markdown: str | None
    method: FetchMethod
    redirects: list[str] = field(default_factory=list)


class Fetcher(Protocol):
    async def fetch(self, url: str, *, timeout_s: float) -> RawPage: ...
    async def health(self) -> bool: ...
```

- [ ] **Step 5: Implement `adapters/static_fetch.py`**

```python
"""Static HTTP fetch with httpx: manual redirects (SSRF-checked), size cap, type allow-list (V1-04, §8)."""

import re
from urllib.parse import urljoin

import httpx

from research_engine.errors import ServiceError
from research_engine.retry import retry
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, FetchMethod

from .fetch import RawPage

MAX_REDIRECTS = 5
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_-]+)""", re.IGNORECASE)


def _sniff(body: bytes) -> str | None:
    head = body[:1024].lstrip().lower()
    if head.startswith(b"%pdf-"):
        return "application/pdf"
    if head.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return None


def _decode(body: bytes, header_charset: str | None) -> str:
    charset = header_charset
    if not charset and (m := _META_CHARSET.search(body[:4096])):
        charset = m.group(1).decode("ascii", "ignore")
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class StaticFetcher:
    def __init__(self, client: httpx.AsyncClient, guard: SsrfGuard, *, max_bytes: int,
                 allowed_types: frozenset[str], user_agent: str) -> None:
        self._client = client
        self._guard = guard
        self._max = max_bytes
        self._allowed = allowed_types
        self._ua = user_agent

    async def fetch(self, url: str, *, timeout_s: float) -> RawPage:
        return await retry(lambda: self._fetch_once(url, timeout_s), attempts=3)

    async def _fetch_once(self, url: str, timeout_s: float) -> RawPage:
        current, hops = url, []
        for _ in range(MAX_REDIRECTS + 1):
            await self._guard.check(current)
            try:
                async with self._client.stream("GET", current, timeout=timeout_s, follow_redirects=False,
                                               headers={"User-Agent": self._ua,
                                                        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"}) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        hops.append(current)
                        current = urljoin(current, resp.headers["location"])
                        continue
                    self._check_status(resp, current)
                    body = await self._read_capped(resp, current)
                    return self._build(url, current, resp, body, hops)
            except httpx.TimeoutException as exc:
                raise ServiceError.of(ErrorCode.UPSTREAM_TIMEOUT, f"timed out fetching {current}",
                                      retryable=True, source=current, http_status=504) from exc
            except httpx.HTTPError as exc:
                raise ServiceError.of(ErrorCode.FETCH_FAILED, f"network error fetching {current}: {exc}",
                                      retryable=True, source=current) from exc
        raise ServiceError.of(ErrorCode.FETCH_FAILED, f"too many redirects from {url}",
                              retryable=False, source=url)

    @staticmethod
    def _check_status(resp: httpx.Response, url: str) -> None:
        if resp.status_code < 400:
            return
        retryable = resp.status_code == 429 or resp.status_code >= 500
        raise ServiceError.of(ErrorCode.FETCH_FAILED, f"HTTP {resp.status_code} from {url}",
                              retryable=retryable, source=url)

    async def _read_capped(self, resp: httpx.Response, url: str) -> bytes:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max:
            raise self._too_large(url)
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > self._max:
                raise self._too_large(url)
            chunks.append(chunk)
        return b"".join(chunks)

    def _too_large(self, url: str) -> ServiceError:
        return ServiceError.of(ErrorCode.RESPONSE_TOO_LARGE, f"response exceeds {self._max} bytes",
                               retryable=False, source=url, http_status=413)

    def _build(self, url: str, final: str, resp: httpx.Response, body: bytes, hops: list[str]) -> RawPage:
        raw_type = resp.headers.get("content-type", "")
        ctype = raw_type.split(";", 1)[0].strip().lower() or (_sniff(body) or "")
        if ctype not in self._allowed:
            raise ServiceError.of(ErrorCode.CONTENT_TYPE_NOT_ALLOWED, f"content type {ctype or 'unknown'!r} not allowed",
                                  retryable=False, source=final, http_status=415)
        html = None
        if ctype in ("text/html", "application/xhtml+xml", "text/plain"):
            html = _decode(body, resp.charset_encoding)
        return RawPage(url=url, final_url=final, status=resp.status_code, content_type=ctype, body=body,
                       html=html, markdown=None, method=FetchMethod.STATIC, redirects=hops)

    async def health(self) -> bool:
        return True
```

- [ ] **Step 6: Run the tests** (both files plus `test_searxng.py`). Expected: PASS.

- [ ] **Step 7: Commit.** `git commit -m "feat(fetch): retry helper and SSRF-safe static fetcher (V1-04, §8)"`

---

### Task 3.4: HTML extractor (Trafilatura, tables, links, extruct) and page fixtures

**Goal:** `DefaultHtmlExtractor.extract(html, base_url)` returns markdown, metadata, tables, links and structured data, tested against hand-written page fixtures.

**Files:**
- Create: `packages/research_engine/src/research_engine/adapters/{extract,html_extract}.py`
- Create: `packages/research_engine/src/research_engine/pipeline/heuristics.py` (`looks_js_rendered`)
- Modify: `packages/research_engine/pyproject.toml` (deps: `trafilatura`, `extruct`, `lxml`)
- Create: `tests/fixtures/pages/article.html`, `tests/fixtures/pages/spa.html`, `tests/fixtures/pages/jsonld_tables.html`
- Test: `tests/service/unit/test_html_extract.py`, `tests/service/unit/test_heuristics.py`

**Acceptance Criteria:**
- [ ] `article.html`, an original roughly 400-word article written for this repo with `<html lang="en">`, a title, `<meta name="author">` and `<time datetime>`, produces:
  - `word_count >= 300`;
  - the title, author and language `en`;
  - an aware `published_at`;
  - markdown containing a `#` heading.
- [ ] `jsonld_tables.html` produces:
  - a `Product` JSON-LD item, OpenGraph `og:title` in `opengraph`, and one microdata item;
  - 2 tables, one with `<caption>` and `<thead>`, one with a first-row `<th>` header;
  - `Table.headers` and `rows` exactly as expected;
  - `source_selector` set to `table:nth-of-type(1)` or `table:nth-of-type(2)`.
- [ ] Layout tables with fewer than 2 rows or 2 columns are skipped.
- [ ] Links are absolute (resolved against `base_url`), http(s) only, deduplicated by URL, and keep the first anchor text, whitespace-normalised. `external` is True when the domain differs. They're capped at 500.
- [ ] `looks_js_rendered(html, word_count, threshold)` is True for `spa.html`, which has an empty `<div id="root">`, 3 script tags and a `<noscript>You need to enable JavaScript`. It's False for `article.html`.
- [ ] Malformed HTML (`"<html><body><table><tr><td>x"`) doesn't raise.

**Verify:** `uv run pytest tests/service/unit/test_html_extract.py tests/service/unit/test_heuristics.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the fixtures.** All content is original; no copied web pages.
  - `article.html`: an original roughly 400-word explainer on "How metasearch engines merge results", with `<h1>`, three `<h2>` sections, `<meta name="author" content="Test Author">`, `<time datetime="2026-03-14T09:00:00Z">`, `<html lang="en">`, 3 internal and 2 external links, and nav/footer boilerplate that Trafilatura should drop.
  - `spa.html`: `<html><head><title>App</title><script src="/a.js"></script><script src="/b.js"></script><script>window.__STATE__={}</script></head><body><noscript>You need to enable JavaScript to run this app.</noscript><div id="root"></div></body></html>`.
  - `jsonld_tables.html`, containing:
    - a `<script type="application/ld+json">` Product block with `name` "Widget" and an `offers.price` of "9.99";
    - `<meta property="og:title" content="Widget page">`;
    - a microdata `itemscope itemtype="https://schema.org/Person"` with `itemprop="name"` "Ada";
    - table 1: `<caption>Plans</caption>`, `<thead>` with Plan/Price, and 2 body rows (Free/0 and Pro/10);
    - table 2: no thead, a first row of `<th>` A/B, and a body row 1/2;
    - a layout table with 1 cell.

- [ ] **Step 2: Write the failing tests**

```python
# tests/service/unit/test_html_extract.py
from pathlib import Path

from research_engine.adapters.html_extract import DefaultHtmlExtractor

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"
X = DefaultHtmlExtractor()


def test_article() -> None:
    ex = X.extract((PAGES / "article.html").read_text(), "https://blog.example/post")
    assert ex.word_count >= 300 and ex.title and ex.author == "Test Author" and ex.language == "en"
    assert ex.published_at is not None and ex.published_at.tzinfo is not None
    assert "# " in ex.markdown or ex.markdown.lstrip().startswith("#")
    assert all(link.url.startswith("http") for link in ex.links)
    assert {link.external for link in ex.links} == {True, False}


def test_structured_and_tables() -> None:
    ex = X.extract((PAGES / "jsonld_tables.html").read_text(), "https://shop.example/w")
    assert any(item.get("@type") == "Product" for item in ex.structured_data.json_ld)
    assert ex.structured_data.opengraph.get("og:title") == "Widget page"
    assert len(ex.structured_data.microdata) == 1
    assert len(ex.tables) == 2
    t1, t2 = ex.tables
    assert t1.caption == "Plans" and t1.headers == ["Plan", "Price"] and t1.rows == [["Free", "0"], ["Pro", "10"]]
    assert t1.source_selector == "table:nth-of-type(1)"
    assert t2.headers == ["A", "B"] and t2.rows == [["1", "2"]]


def test_malformed_html_does_not_raise() -> None:
    X.extract("<html><body><table><tr><td>x", "https://a.example/")


def test_link_cap_and_dedupe() -> None:
    html = "<html><body>" + "".join(f'<a href="/p{i % 600}"> L  {i} </a>' for i in range(1200)) + "</body></html>"
    ex = X.extract(html, "https://a.example/")
    assert len(ex.links) == 500 and ex.links[0].text == "L 0"
```

```python
# tests/service/unit/test_heuristics.py
from pathlib import Path

from research_engine.pipeline.heuristics import looks_js_rendered

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"


def test_spa_detected() -> None:
    assert looks_js_rendered((PAGES / "spa.html").read_text(), word_count=0, threshold=150)


def test_article_not_js() -> None:
    assert not looks_js_rendered((PAGES / "article.html").read_text(), word_count=400, threshold=150)
```

- [ ] **Step 3: Run them.** Expected: FAIL (import error).

- [ ] **Step 4: Add the dependencies.** `uv add --package research-engine "trafilatura>=2.3.0" "extruct>=0.18.0" "lxml>=6.1.3"`. Confirm that `uv sync` builds `jstyleson` cleanly; the spike showed it does on 3.13.

- [ ] **Step 5: Implement `adapters/extract.py`.** It holds the `Extracted` dataclass and the `HtmlExtractor` and `PdfExtractor` protocols, as in the contract, with `published_at: datetime | None`.

- [ ] **Step 6: Implement `adapters/html_extract.py`**

```python
"""HTML → markdown/metadata (Trafilatura), tables (lxml), links, embedded data (extruct). V1-04, V1-05.

Synchronous by design: callers run it via asyncio.to_thread (Global Constraint 6).
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import extruct
import trafilatura
from lxml import html as lxml_html

from research_engine.pipeline.urls import domain_of
from research_engine_client.models import Link, StructuredData, Table

from .extract import Extracted

MAX_LINKS = 500


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class DefaultHtmlExtractor:
    def extract(self, html: str, base_url: str) -> Extracted:
        tree = self._tree(html)
        markdown = trafilatura.extract(html, url=base_url, output_format="markdown", include_tables=True,
                                       include_links=False, include_comments=False, favor_recall=True) or ""
        meta = trafilatura.extract_metadata(html, default_url=base_url)
        title = _norm(getattr(meta, "title", None)) or (_norm(tree.findtext(".//title")) if tree is not None else "")
        lang = getattr(meta, "language", None) or (tree.get("lang") if tree is not None else None)
        text_len = len(" ".join(markdown.split()))
        return Extracted(
            title=title or None,
            author=_norm(getattr(meta, "author", None)) or None,
            published_at=_parse_date(getattr(meta, "date", None)),
            language=(lang or "").split("-")[0].lower() or None,
            markdown=markdown,
            word_count=len(markdown.split()),
            links=self._links(tree, base_url) if tree is not None else [],
            tables=self._tables(tree) if tree is not None else [],
            structured_data=self._structured(html, base_url),
            html_len=len(html),
            text_len=text_len,
        )

    @staticmethod
    def _tree(html: str) -> Any:
        try:
            return lxml_html.document_fromstring(html)
        except (ValueError, lxml_html.etree.ParserError):  # type: ignore[attr-defined]
            return None

    @staticmethod
    def _links(tree: Any, base_url: str) -> list[Link]:
        base_domain = domain_of(base_url)
        seen: dict[str, Link] = {}
        for a in tree.iterfind(".//a[@href]"):
            url = urljoin(base_url, a.get("href", "").strip())
            if urlsplit(url).scheme not in ("http", "https") or url in seen:
                continue
            seen[url] = Link(url=url, text=_norm(a.text_content()), external=domain_of(url) != base_domain)
            if len(seen) >= MAX_LINKS:
                break
        return list(seen.values())

    @staticmethod
    def _tables(tree: Any) -> list[Table]:
        out = []
        for idx, table in enumerate(tree.iterfind(".//table"), start=1):
            rows = [[_norm(c.text_content()) for c in tr.iterfind("./*") if c.tag in ("td", "th")]
                    for tr in table.iterfind(".//tr")]
            rows = [r for r in rows if r]
            if len(rows) < 2 or max(len(r) for r in rows) < 2:
                continue
            first_tr = table.find(".//tr")
            has_header = table.find(".//thead") is not None or (
                first_tr is not None and all(c.tag == "th" for c in first_tr.iterfind("./*")))
            headers, body = (rows[0], rows[1:]) if has_header else ([], rows)
            caption = table.find("./caption")
            out.append(Table(caption=_norm(caption.text_content()) if caption is not None else None,
                             headers=headers, rows=body, source_selector=f"table:nth-of-type({idx})"))
        return out

    @staticmethod
    def _structured(html: str, base_url: str) -> StructuredData:
        try:
            data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld", "microdata", "opengraph"],
                                   uniform=True, errors="ignore")
        except Exception:  # noqa: BLE001 - extruct raises many types on hostile markup; degrade to empty
            return StructuredData()
        og: dict[str, Any] = {}
        for item in data.get("opengraph", []):
            for k, v in item.items():
                if k != "@context":
                    og.setdefault(k if ":" in k else f"og:{k}", v)
        return StructuredData(json_ld=data.get("json-ld", []), microdata=data.get("microdata", []), opengraph=og)
```

Confirm that the `nth-of-type` index counts nested tables in document order. That's acceptable for V1; note it in the docstring. Check the trafilatura metadata attribute names against the installed 2.3.0 (`python -c "import trafilatura;help(trafilatura.extract_metadata)"`). If `uniform=True` gives OpenGraph keys without the `og:` prefix, the normalisation above handles it.

- [ ] **Step 7: Implement `pipeline/heuristics.py`**

```python
"""Signals that a page needs a real browser (V1-04 escalation)."""

import re

_NOSCRIPT_JS = re.compile(r"<noscript[^>]*>[^<]*(enable|requires?|need)[^<]*javascript", re.IGNORECASE)
_EMPTY_MOUNT = re.compile(
    r"""<div[^>]+id=["'](root|app|__next|__nuxt|svelte|main)["'][^>]*>\s*</div>""", re.IGNORECASE)
_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)


def looks_js_rendered(html: str, *, word_count: int, threshold: int) -> bool:
    if _NOSCRIPT_JS.search(html):
        return True
    return bool(_EMPTY_MOUNT.search(html)) and len(_SCRIPT.findall(html)) >= 3 and word_count < threshold
```

- [ ] **Step 8: Run the tests.** Expected: PASS.

- [ ] **Step 9: Commit.** `git commit -m "feat(fetch): HTML extraction (text, tables, links, JSON-LD/microdata/OG) and SPA heuristics (V1-04, V1-05)"`

---

### Task 3.5: PDF extractor

**Goal:** `PypdfExtractor.extract(body, url)` turns PDF bytes into text markdown, with the title and author taken from the PDF metadata.

**Files:**
- Create: `packages/research_engine/src/research_engine/adapters/pdf_extract.py`
- Create: `scripts/make_pdf_fixture.py`, `tests/fixtures/pages/sample.pdf` (generated, about 2 KB)
- Modify: `packages/research_engine/pyproject.toml` (dep: `pypdf`)
- Test: `tests/service/unit/test_pdf_extract.py`

**Acceptance Criteria:**
- [ ] `sample.pdf` (2 pages, generated by the script) yields markdown containing both pages' text separated by `\n\n---\n\n`, `title="Sample Vendor Sheet"` and `author="Test Author"`.
- [ ] An encrypted, corrupt or empty PDF raises `ServiceError(code=extraction_failed, retryable=False)`.
- [ ] `tables=[]`, `links=[]` and `language=None` (full PDF intelligence is V2).

**Verify:** `uv run pytest tests/service/unit/test_pdf_extract.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the fixture generator.** `scripts/make_pdf_fixture.py` writes a minimal valid 2-page PDF with Helvetica text and an Info dictionary, with xref offsets computed. It uses no extra dependency.

```python
"""Write tests/fixtures/pages/sample.pdf: a tiny 2-page PDF with Info metadata. Run once."""

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pages" / "sample.pdf"
PAGES = ["Widget Pro pricing sheet. Plan Free costs 0. Plan Pro costs 10 per month.",
         "Support hours are 9 to 5. Contact sales for enterprise volume discounts."]


def build() -> bytes:
    objs: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(PAGES)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(PAGES)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(PAGES):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objs.append(b"<< /Title (Sample Vendor Sheet) /Author (Test Author) >>")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {len(objs)} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


if __name__ == "__main__":
    OUT.write_bytes(build())
    print("wrote", OUT)
```

Run `uv run python scripts/make_pdf_fixture.py`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/service/unit/test_pdf_extract.py
from pathlib import Path

import pytest

from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.errors import ServiceError
from research_engine_client.models import ErrorCode

PDF = Path(__file__).resolve().parents[2] / "fixtures" / "pages" / "sample.pdf"


def test_sample_pdf() -> None:
    ex = PypdfExtractor().extract(PDF.read_bytes(), "https://vendor.example/sheet.pdf")
    assert "Widget Pro pricing" in ex.markdown and "Support hours" in ex.markdown
    assert "\n\n---\n\n" in ex.markdown
    assert ex.title == "Sample Vendor Sheet" and ex.author == "Test Author"
    assert ex.tables == [] and ex.language is None and ex.word_count > 20


@pytest.mark.parametrize("body", [b"", b"%PDF-1.4 garbage"])
def test_bad_pdf(body: bytes) -> None:
    with pytest.raises(ServiceError) as ei:
        PypdfExtractor().extract(body, "https://x.example/a.pdf")
    assert ei.value.detail.code is ErrorCode.EXTRACTION_FAILED
```

- [ ] **Step 3: Implement `adapters/pdf_extract.py`**

```python
"""Basic PDF text extraction with pypdf (V1-06). Docling replaces this in V2."""

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from research_engine.errors import ServiceError
from research_engine_client.models import ErrorCode, StructuredData

from .extract import Extracted


class PypdfExtractor:
    def extract(self, body: bytes, url: str) -> Extracted:
        try:
            reader = PdfReader(io.BytesIO(body))
            if reader.is_encrypted:
                raise ServiceError.of(ErrorCode.EXTRACTION_FAILED, "encrypted PDF", retryable=False, source=url)
            pages = [(p.extract_text() or "").strip() for p in reader.pages]
            meta = reader.metadata
        except (PdfReadError, ValueError, KeyError, OSError) as exc:
            raise ServiceError.of(ErrorCode.EXTRACTION_FAILED, f"could not read PDF: {exc}",
                                  retryable=False, source=url) from exc
        text = "\n\n---\n\n".join(p for p in pages if p)
        return Extracted(title=(meta.title if meta else None) or None, author=(meta.author if meta else None) or None,
                         published_at=None, language=None, markdown=text, word_count=len(text.split()),
                         links=[], tables=[], structured_data=StructuredData(),
                         html_len=len(body), text_len=len(text))
```

If `test_bad_pdf` shows that pypdf raises another exception type for garbage input, add it to the tuple. Find the real type with systematic debugging rather than catching `Exception`.

- [ ] **Step 4: Run the tests.** Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "feat(fetch): basic PDF extraction with pypdf (V1-06)"`

---

### Task 3.6: Crawl4AI browser fetcher

**Goal:** `Crawl4AIFetcher` renders a URL through Crawl4AI's `/crawl` API with a bearer token, and maps the result and failures to `RawPage` and `ServiceError`.

**Files:**
- Create: `packages/research_engine/src/research_engine/adapters/crawl4ai.py`
- Test: `tests/service/unit/test_crawl4ai.py`

**Acceptance Criteria:**
- [ ] It calls `guard.check(url)` before the request.
- [ ] It sends `POST {base}/crawl` with `Authorization: Bearer <token>` and the body `{"urls":[url],"browser_config":{"type":"BrowserConfig","params":{"headless":true}},"crawler_config":{"type":"CrawlerRunConfig","params":{"cache_mode":"bypass","page_timeout":<ms>}}}`. It never sends `proxy`, `proxy_config` or `extra_args`.
- [ ] Parsing the recorded `crawl_example.json` gives a `RawPage` with:
  - `method=browser`;
  - `html` set from `result.html`;
  - `markdown` set from `result.markdown.raw_markdown`, or `result.markdown` if it is a string;
  - `final_url` set from `redirected_url` or the url;
  - `status` set from `status_code`, defaulting to 200.
- [ ] If `redirected_url` fails the SSRF guard, it raises `ssrf_blocked`.
- [ ] `success: false` raises `fetch_failed` (retryable), carrying `error_message`. An HTTP 401 or 403 from Crawl4AI raises `upstream_error` (not retryable, "check CRAWL4AI_API_TOKEN"). A timeout raises `upstream_timeout`.
- [ ] `health()` returns True only on a 200 from `GET /health`, and never raises.

**Verify:** `uv run pytest tests/service/unit/test_crawl4ai.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/service/unit/test_crawl4ai.py
import json
from pathlib import Path

import httpx
import pytest
import respx

from research_engine.adapters.crawl4ai import Crawl4AIFetcher
from research_engine.errors import ServiceError
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, FetchMethod

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "crawl4ai" / "crawl_example.json"
BASE = "http://crawl4ai.test:11235"


async def _resolve(host: str) -> list[str]:
    return {"internal.example": ["10.0.0.9"]}.get(host, ["93.184.216.34"])


@pytest.fixture
def fetcher() -> Crawl4AIFetcher:
    return Crawl4AIFetcher(BASE, "tok", httpx.AsyncClient(), SsrfGuard(frozenset(), resolver=_resolve))


@respx.mock
async def test_crawl_ok(fetcher: Crawl4AIFetcher) -> None:
    route = respx.post(f"{BASE}/crawl").respond(json=json.loads(FIX.read_text()))
    page = await fetcher.fetch("https://example.com", timeout_s=30)
    req = route.calls.last.request
    sent = json.loads(req.content)
    assert req.headers["authorization"] == "Bearer tok"
    assert sent["urls"] == ["https://example.com"]
    assert sent["crawler_config"]["params"]["page_timeout"] == 30000
    assert "proxy" not in json.dumps(sent) and "extra_args" not in json.dumps(sent)
    assert page.method is FetchMethod.BROWSER and page.html and page.markdown


@respx.mock
async def test_unsuccessful_crawl(fetcher: Crawl4AIFetcher) -> None:
    respx.post(f"{BASE}/crawl").respond(json={"success": True, "results": [
        {"url": "https://example.com", "success": False, "error_message": "net::ERR_NAME_NOT_RESOLVED"}]})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.FETCH_FAILED and "ERR_NAME" in ei.value.detail.message


@respx.mock
async def test_redirect_to_internal_blocked(fetcher: Crawl4AIFetcher) -> None:
    respx.post(f"{BASE}/crawl").respond(json={"success": True, "results": [
        {"url": "https://example.com", "success": True, "html": "<p>x</p>", "markdown": "x",
         "redirected_url": "http://internal.example/"}]})
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.SSRF_BLOCKED


@respx.mock
async def test_auth_failure(fetcher: Crawl4AIFetcher) -> None:
    respx.post(f"{BASE}/crawl").respond(401)
    with pytest.raises(ServiceError) as ei:
        await fetcher.fetch("https://example.com", timeout_s=30)
    assert ei.value.detail.code is ErrorCode.UPSTREAM_ERROR and not ei.value.detail.retryable
    assert "CRAWL4AI_API_TOKEN" in ei.value.detail.message
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `adapters/crawl4ai.py`**

```python
"""Crawl4AI headless-browser fetcher via its REST API (V1-04). Sandbox stays ON (B3)."""

from typing import Any

import httpx

from research_engine.errors import ServiceError
from research_engine.retry import retry
from research_engine.safety.ssrf import SsrfGuard
from research_engine_client.models import ErrorCode, FetchMethod

from .fetch import RawPage


class Crawl4AIFetcher:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient, guard: SsrfGuard) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client
        self._guard = guard

    async def fetch(self, url: str, *, timeout_s: float) -> RawPage:
        await self._guard.check(url)
        return await retry(lambda: self._crawl(url, timeout_s), attempts=2)

    async def _crawl(self, url: str, timeout_s: float) -> RawPage:
        body = {"urls": [url],
                "browser_config": {"type": "BrowserConfig", "params": {"headless": True}},
                "crawler_config": {"type": "CrawlerRunConfig",
                                   "params": {"cache_mode": "bypass", "page_timeout": int(timeout_s * 1000)}}}
        try:
            resp = await self._client.post(f"{self._base}/crawl", json=body, timeout=timeout_s + 30,
                                           headers={"Authorization": f"Bearer {self._token}"})
        except httpx.TimeoutException as exc:
            raise ServiceError.of(ErrorCode.UPSTREAM_TIMEOUT, f"browser timed out on {url}",
                                  retryable=True, source="crawl4ai", http_status=504) from exc
        except httpx.HTTPError as exc:
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, f"Crawl4AI unreachable: {exc}",
                                  retryable=True, source="crawl4ai") from exc
        if resp.status_code in (401, 403):
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, "Crawl4AI rejected credentials; check CRAWL4AI_API_TOKEN",
                                  retryable=False, source="crawl4ai")
        if resp.status_code >= 400:
            raise ServiceError.of(ErrorCode.UPSTREAM_ERROR, f"Crawl4AI returned {resp.status_code}",
                                  retryable=resp.status_code >= 500, source="crawl4ai")
        result: dict[str, Any] = (resp.json().get("results") or [{}])[0]
        if not result.get("success"):
            raise ServiceError.of(ErrorCode.FETCH_FAILED,
                                  f"browser fetch failed: {result.get('error_message') or 'unknown error'}",
                                  retryable=True, source=url)
        final = result.get("redirected_url") or url
        if final != url:
            await self._guard.check(final)
        md = result.get("markdown")
        markdown = md.get("raw_markdown") if isinstance(md, dict) else md
        html = result.get("html") or ""
        return RawPage(url=url, final_url=final, status=int(result.get("status_code") or 200),
                       content_type="text/html", body=html.encode(), html=html, markdown=markdown or "",
                       method=FetchMethod.BROWSER, redirects=[url] if final != url else [])

    async def health(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base}/health", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200
```

- [ ] **Step 4: Run the tests.** Expected: PASS. If the recorded fixture's shape differs, for example `markdown` is a string or `status_code` is missing, the parser above already tolerates both. Add a test for whatever shape was actually recorded.

- [ ] **Step 5: Commit.** `git commit -m "feat(fetch): Crawl4AI browser fetcher (V1-04)"`

---

### Task 3.7: FetchService, `/v1/fetch` and live integration

**Goal:** Orchestrate the tiered fetch (cache → robots → politeness → static → extract → escalate to browser → Document), expose `POST /v1/fetch`, and prove the acceptance criteria against the live stack.

**Files:**
- Create: `packages/research_engine/src/research_engine/pipeline/fetch.py`
- Create: `packages/research_engine/src/research_engine/api/fetch.py`
- Modify: `packages/research_engine/src/research_engine/api/deps.py` (add `fetch: FetchService`)
- Modify: `packages/research_engine/src/research_engine/app.py` (wire `SsrfGuard`, `DomainLimiter`, `RobotsPolicy`, the fetchers, the extractors and `FetchService`; include the router)
- Modify: `packages/research_engine/src/research_engine/testing.py` (fake static and browser fetchers serving the fixture pages; wire `FetchService`)
- Test: `tests/service/unit/test_fetch_service.py`, `tests/service/unit/test_api_fetch.py`, `tests/integration/test_fetch_live.py`

**Acceptance Criteria:**
- [ ] **mode=auto, static article:**
  - static only, `provenance.method=static`;
  - `quality.escalation_reason=None`;
  - `content_hash = "sha256:" + sha256(markdown)`;
  - `provenance.job_id` set from the `job_id` argument.
- [ ] **mode=auto, SPA:** escalates with `js_rendered`. The Document has `method=browser` and the markdown is Crawl4AI's. Tables, links and structured data come from extracting the rendered HTML. `warnings` contains `"escalated to browser: js_rendered"`. The event `fetch.escalated` is emitted.
- [ ] **mode=auto, thin page** (word_count < threshold, no SPA signals): escalates with `thin_content`. If the browser then fails, the service returns the static Document with the warning `"browser escalation failed: <message>"` instead of failing.
- [ ] **mode=auto, static failure:**
  - a retryable `fetch_failed`, or a non-retryable 403, escalates with `static_failed`;
  - `ssrf_blocked`, `robots_disallowed`, `content_type_not_allowed` and `response_too_large` **do not** escalate; they are raised.
- [ ] **mode=static** never escalates. **mode=browser** goes straight to the browser with `escalation_reason=forced`.
- [ ] **PDF:** PDF content is extracted with `PypdfExtractor` and never escalated.
- [ ] **html format:** `html` is populated only when `"html" in req.formats`.
- [ ] **Cache and politeness:**
  - the cache key is computed on the request with its URL canonicalised;
  - a hit returns `(doc, True)` and emits `cache.hit`;
  - `use_cache=false` bypasses the read;
  - TTL is `cache_ttl_page_s`;
  - the robots check and the `DomainLimiter.slot` are applied to the **original** URL before any network fetch.
- [ ] **Blocking work:** every call to an extractor runs in `asyncio.to_thread`. A test asserts the event loop isn't blocked, using a fake extractor that records `threading.get_ident()`, which must differ from the loop's thread.
- [ ] **Endpoint:** `POST /v1/fetch` returns `Envelope[Document]` with `meta.cache_hit`. For SSRF and robots blocks it returns 403 with the typed error.
- [ ] **Integration** (`-m integration`, dev stack): these all return a Document with `word_count >= 100` and clean markdown:
  - the static article `https://docs.python.org/3/library/asyncio-task.html`, with `method=static`;
  - a JS-rendered SPA, with `method=browser`;
  - the PDF `https://www.rfc-editor.org/rfc/pdfrfc/rfc9110.txt.pdf`, with method `static` and `pypdf` text.
- [ ] **SPA URL choice:** the SPA URL lives in `tests/integration/live_urls.py`. It must be verified at implementation time to give a static word count under 150 and a browser word count of at least 150. Try candidates in order and pick the first that qualifies: `https://www.crawl4ai.com/`, `https://excalidraw.com/`, `https://app.diagrams.net/`. If none qualify, ask the owner for a URL.

**Verify:** `uv run pytest tests/service/unit/test_fetch_service.py tests/service/unit/test_api_fetch.py -v` → all pass; `source <(scripts/dev_urls.sh) && CRAWL4AI_API_TOKEN=$(grep ^CRAWL4AI_API_TOKEN= .env | cut -d= -f2-) uv run pytest -m integration tests/integration/test_fetch_live.py -v` → 3 passed

**Steps:**

- [ ] **Step 1: Write the failing service tests.** They use fakes for both fetchers.

```python
# tests/service/unit/test_fetch_service.py
import hashlib
import threading
from pathlib import Path

import pytest

from research_engine.adapters.fetch import RawPage
from research_engine.adapters.html_extract import DefaultHtmlExtractor
from research_engine.adapters.pdf_extract import PypdfExtractor
from research_engine.cache.memory import InMemoryCache
from research_engine.config import Settings
from research_engine.errors import ServiceError
from research_engine.pipeline.fetch import FetchService
from research_engine.safety.limiter import DomainLimiter
from research_engine_client.models import (
    DocumentFormat, ErrorCode, Event, EventKind, EscalationReason, FetchMethod, FetchMode, FetchRequest,
)

PAGES = Path(__file__).resolve().parents[2] / "fixtures" / "pages"


class FakeFetcher:
    def __init__(self, method: FetchMethod, pages: dict[str, RawPage | ServiceError]) -> None:
        self.method, self.pages, self.calls = method, pages, []

    async def fetch(self, url: str, *, timeout_s: float) -> RawPage:
        self.calls.append(url)
        out = self.pages[url]
        if isinstance(out, ServiceError):
            raise out
        return out

    async def health(self) -> bool:
        return True


def html_page(url: str, file: str, method: FetchMethod = FetchMethod.STATIC, markdown: str | None = None) -> RawPage:
    html = (PAGES / file).read_text()
    return RawPage(url=url, final_url=url, status=200, content_type="text/html", body=html.encode(),
                   html=html, markdown=markdown, method=method)


class AllowRobots:
    def __init__(self) -> None:
        self.checked: list[str] = []

    async def check(self, url: str, em=None) -> None:  # noqa: ANN001
        self.checked.append(url)


class Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, e: Event) -> None:
        self.events.append(e)


def make(static: dict, browser: dict, settings: Settings) -> tuple[FetchService, FakeFetcher, FakeFetcher, Sink]:  # type: ignore[type-arg]
    s, b, sink = FakeFetcher(FetchMethod.STATIC, static), FakeFetcher(FetchMethod.BROWSER, browser), Sink()
    svc = FetchService(s, b, DefaultHtmlExtractor(), PypdfExtractor(), AllowRobots(),  # type: ignore[arg-type]
                       DomainLimiter(2, 0), InMemoryCache(), sink, settings)
    return svc, s, b, sink


@pytest.fixture
def settings(settings_env: None) -> Settings:
    return Settings()


async def test_static_article(settings: Settings) -> None:
    url = "https://blog.example/post"
    svc, _, b, _ = make({url: html_page(url, "article.html")}, {}, settings)
    doc, hit = await svc.fetch(FetchRequest(url=url), job_id="j9")
    assert not hit and doc.provenance.method is FetchMethod.STATIC and b.calls == []
    assert doc.provenance.content_hash == "sha256:" + hashlib.sha256(doc.markdown.encode()).hexdigest()
    assert doc.provenance.job_id == "j9" and doc.quality.escalation_reason is None and doc.html is None


async def test_spa_escalates(settings: Settings) -> None:
    url = "https://app.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="# Rendered\n\n" + "word " * 200)
    svc, _, b, sink = make({url: html_page(url, "spa.html")}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.provenance.method is FetchMethod.BROWSER and doc.markdown.startswith("# Rendered")
    assert doc.quality.escalation_reason is EscalationReason.JS_RENDERED
    assert "escalated to browser: js_rendered" in doc.warnings
    assert EventKind.FETCH_ESCALATED in [e.kind for e in sink.events]


async def test_thin_then_browser_fails_keeps_static(settings: Settings) -> None:
    url = "https://thin.example/"
    thin = RawPage(url=url, final_url=url, status=200, content_type="text/html", body=b"",
                   html="<html><head><title>T</title></head><body><p>short page</p></body></html>",
                   markdown=None, method=FetchMethod.STATIC)
    err = ServiceError.of(ErrorCode.FETCH_FAILED, "boom", retryable=True)
    svc, _, _, _ = make({url: thin}, {url: err}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.provenance.method is FetchMethod.STATIC
    assert any(w.startswith("browser escalation failed") for w in doc.warnings)


@pytest.mark.parametrize("code", [ErrorCode.SSRF_BLOCKED, ErrorCode.CONTENT_TYPE_NOT_ALLOWED,
                                  ErrorCode.RESPONSE_TOO_LARGE])
async def test_non_escalating_errors(settings: Settings, code: ErrorCode) -> None:
    url = "https://x.example/"
    svc, _, b, _ = make({url: ServiceError.of(code, "no", retryable=False)}, {}, settings)
    with pytest.raises(ServiceError):
        await svc.fetch(FetchRequest(url=url))
    assert b.calls == []


async def test_static_failure_escalates(settings: Settings) -> None:
    url = "https://f.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, _, _, _ = make({url: ServiceError.of(ErrorCode.FETCH_FAILED, "403", retryable=False)}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert doc.quality.escalation_reason is EscalationReason.STATIC_FAILED


async def test_modes(settings: Settings) -> None:
    url = "https://m.example/"
    rendered = html_page(url, "article.html", FetchMethod.BROWSER, markdown="word " * 300)
    svc, s, _, _ = make({url: html_page(url, "spa.html")}, {url: rendered}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url, mode=FetchMode.STATIC, use_cache=False))
    assert doc.provenance.method is FetchMethod.STATIC
    doc, _ = await svc.fetch(FetchRequest(url=url, mode=FetchMode.BROWSER, use_cache=False))
    assert doc.quality.escalation_reason is EscalationReason.FORCED and s.calls == [url]


async def test_pdf(settings: Settings) -> None:
    url = "https://v.example/a.pdf"
    body = (PAGES / "sample.pdf").read_bytes()
    pdf = RawPage(url=url, final_url=url, status=200, content_type="application/pdf", body=body,
                  html=None, markdown=None, method=FetchMethod.STATIC)
    svc, _, b, _ = make({url: pdf}, {}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url))
    assert "Widget Pro" in doc.markdown and b.calls == []


async def test_html_format_and_cache(settings: Settings) -> None:
    url = "https://blog.example/post?utm_source=x"
    svc, s, _, sink = make({url: html_page(url, "article.html")}, {}, settings)
    doc, _ = await svc.fetch(FetchRequest(url=url, formats=(DocumentFormat.MARKDOWN, DocumentFormat.HTML)))
    assert doc.html
    doc2, hit = await svc.fetch(FetchRequest(url="https://blog.example/post",
                                             formats=(DocumentFormat.MARKDOWN, DocumentFormat.HTML)))
    assert hit and len(s.calls) == 1 and EventKind.CACHE_HIT in [e.kind for e in sink.events]


async def test_extraction_off_loop(settings: Settings) -> None:
    loop_thread = threading.get_ident()
    seen: list[int] = []

    class Spy(DefaultHtmlExtractor):
        def extract(self, html: str, base_url: str):  # noqa: ANN201
            seen.append(threading.get_ident())
            return super().extract(html, base_url)

    url = "https://blog.example/post"
    svc, _, _, _ = make({url: html_page(url, "article.html")}, {}, settings)
    svc._html = Spy()  # noqa: SLF001 - test seam
    await svc.fetch(FetchRequest(url=url))
    assert seen and all(t != loop_thread for t in seen)
```

- [ ] **Step 2: Run them.** Expected: FAIL (import error).

- [ ] **Step 3: Implement `pipeline/fetch.py`**

```python
"""Tiered fetch orchestration (V1-04..V1-06, V1-10, V1-11)."""

import asyncio
import hashlib
from datetime import UTC, datetime

from research_engine.adapters.extract import Extracted, HtmlExtractor, PdfExtractor
from research_engine.adapters.fetch import Fetcher, RawPage
from research_engine.cache.base import Cache, cache_key
from research_engine.config import Settings
from research_engine.errors import ServiceError
from research_engine.events.base import Emitter, EventSink
from research_engine.safety.limiter import DomainLimiter
from research_engine.safety.robots import RobotsPolicy
from research_engine_client.models import (
    Document, DocumentFormat, ErrorCode, EscalationReason, EventKind, FetchMethod, FetchMode,
    FetchRequest, Provenance, Quality,
)

from .heuristics import looks_js_rendered
from .urls import canonicalize_url

NO_ESCALATE = {ErrorCode.SSRF_BLOCKED, ErrorCode.ROBOTS_DISALLOWED, ErrorCode.CONTENT_TYPE_NOT_ALLOWED,
               ErrorCode.RESPONSE_TOO_LARGE}


class FetchService:
    def __init__(self, static: Fetcher, browser: Fetcher, html: HtmlExtractor, pdf: PdfExtractor,
                 robots: RobotsPolicy, limiter: DomainLimiter, cache: Cache, events: EventSink,
                 settings: Settings) -> None:
        self._static, self._browser, self._html, self._pdf = static, browser, html, pdf
        self._robots, self._limiter, self._cache = robots, limiter, cache
        self._events, self._settings = events, settings

    async def fetch(self, req: FetchRequest, *, job_id: str | None = None) -> tuple[Document, bool]:
        em = Emitter(self._events, job_id)
        key = cache_key("fetch", req.model_copy(update={"url": canonicalize_url(req.url)}))
        if req.use_cache and (cached := await self._cache.get(key)) is not None:
            await em.info(EventKind.CACHE_HIT, f"page cache hit: {req.url}", url=req.url)
            return Document.model_validate_json(cached), True

        await em.info(EventKind.FETCH_STARTED, f"fetch {req.url}", url=req.url, mode=req.mode.value)
        try:
            await self._robots.check(req.url, em)
            async with self._limiter.slot(req.url):
                doc = await self._run(req, em, job_id)
        except ServiceError as exc:
            await em.error(EventKind.FETCH_FAILED, f"fetch failed: {exc.detail.message}", url=req.url,
                           code=exc.detail.code.value)
            raise
        await self._cache.set(key, doc.model_dump_json().encode(), self._settings.cache_ttl_page_s)
        await em.info(EventKind.FETCH_DONE, f"fetched {req.url} ({doc.word_count} words, {doc.provenance.method.value})",
                      url=req.url, words=doc.word_count, method=doc.provenance.method.value)
        return doc, False

    async def _run(self, req: FetchRequest, em: Emitter, job_id: str | None) -> Document:
        if req.mode is FetchMode.BROWSER:
            return await self._browser_doc(req, em, job_id, EscalationReason.FORCED, [])
        try:
            raw = await self._static.fetch(req.url, timeout_s=req.timeout_s)
        except ServiceError as exc:
            if req.mode is FetchMode.STATIC or exc.detail.code in NO_ESCALATE:
                raise
            return await self._browser_doc(req, em, job_id, EscalationReason.STATIC_FAILED,
                                           [f"static fetch failed: {exc.detail.message}"])
        if raw.content_type == "application/pdf":
            ex = await asyncio.to_thread(self._pdf.extract, raw.body, raw.final_url)
            return self._document(req, raw, ex, job_id, None, [])
        ex = await asyncio.to_thread(self._html.extract, raw.html or "", raw.final_url)
        await em.debug(EventKind.FETCH_STATIC_DONE, f"static: {ex.word_count} words", url=req.url, words=ex.word_count)
        static_doc = self._document(req, raw, ex, job_id, None, [])
        if req.mode is FetchMode.STATIC:
            return static_doc
        reason = None
        if looks_js_rendered(raw.html or "", word_count=ex.word_count, threshold=self._settings.thin_word_threshold):
            reason = EscalationReason.JS_RENDERED
        elif ex.word_count < self._settings.thin_word_threshold:
            reason = EscalationReason.THIN_CONTENT
        if reason is None:
            return static_doc
        try:
            return await self._browser_doc(req, em, job_id, reason, [])
        except ServiceError as exc:
            if exc.detail.code is ErrorCode.SSRF_BLOCKED:
                raise
            static_doc.warnings.append(f"browser escalation failed: {exc.detail.message}")
            return static_doc

    async def _browser_doc(self, req: FetchRequest, em: Emitter, job_id: str | None,
                           reason: EscalationReason, warnings: list[str]) -> Document:
        await em.info(EventKind.FETCH_ESCALATED, f"escalating to browser ({reason.value})", url=req.url,
                      reason=reason.value)
        raw = await self._browser.fetch(req.url, timeout_s=req.timeout_s)
        ex = await asyncio.to_thread(self._html.extract, raw.html or "", raw.final_url)
        if raw.markdown and raw.markdown.strip():
            ex.markdown, ex.word_count = raw.markdown, len(raw.markdown.split())
        await em.info(EventKind.FETCH_BROWSER_DONE, f"browser: {ex.word_count} words", url=req.url, words=ex.word_count)
        return self._document(req, raw, ex, job_id, reason, [*warnings, f"escalated to browser: {reason.value}"])

    def _document(self, req: FetchRequest, raw: RawPage, ex: Extracted, job_id: str | None,
                  reason: EscalationReason | None, warnings: list[str]) -> Document:
        digest = hashlib.sha256(ex.markdown.encode()).hexdigest()
        ratio = min(1.0, ex.text_len / ex.html_len) if ex.html_len else 0.0
        return Document(
            url=req.url, final_url=raw.final_url, status=raw.status, title=ex.title, author=ex.author,
            published_at=ex.published_at, language=ex.language, markdown=ex.markdown,
            html=raw.html if DocumentFormat.HTML in req.formats else None, word_count=ex.word_count,
            links=ex.links, tables=ex.tables, structured_data=ex.structured_data,
            provenance=Provenance(url=raw.final_url, fetched_at=datetime.now(UTC), content_hash=f"sha256:{digest}",
                                  method=raw.method if raw.method is not FetchMethod.API else FetchMethod.STATIC,
                                  job_id=job_id),
            quality=Quality(word_count=ex.word_count, text_html_ratio=round(ratio, 4), has_title=bool(ex.title),
                            escalation_reason=reason),
            warnings=warnings,
        )
```

Make `Extracted` a non-frozen dataclass, so `_browser_doc` can override `markdown` and `word_count`.

- [ ] **Step 4: Write the API test, then `api/fetch.py`.** The test posts to `/v1/fetch` through `build_test_services`, which wires the fakes from `testing.py` (fake static fetcher serving `article.html` at `https://blog.example/post`). It asserts a 200 response with `Envelope[Document]`, and that a URL configured in the fake to raise `ssrf_blocked` returns 403.

```python
# api/fetch.py
"""POST /v1/fetch (V1-04)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from research_engine_client.models import Document, Envelope, FetchRequest

from .deps import Services, get_services
from .envelope import ok

router = APIRouter(prefix="/v1", tags=["fetch"])
FETCH_EXAMPLE = {"url": "https://docs.python.org/3/library/asyncio-task.html", "mode": "auto"}


@router.post("/fetch", response_model=Envelope[Document])
async def fetch(request: Request,
                req: Annotated[FetchRequest, Body(openapi_examples={"article": {"value": FETCH_EXAMPLE}})],
                services: Annotated[Services, Depends(get_services)]) -> Envelope[Document]:
    doc, hit = await services.fetch.fetch(req)
    return ok(request, doc, cache_hit=hit)
```

- [ ] **Step 5: Wire it in `app.py`'s `build_services`**

```python
guard = SsrfGuard(settings.ssrf_allow_hosts_set)
limiter = DomainLimiter(settings.domain_concurrency, settings.domain_delay_s)
robots = RobotsPolicy(http, settings.user_agent, limiter, guard)
static = StaticFetcher(http, guard, max_bytes=settings.max_response_bytes,
                       allowed_types=settings.allowed_content_types_set, user_agent=settings.user_agent)
browser = Crawl4AIFetcher(settings.crawl4ai_url, settings.crawl4ai_api_token.get_secret_value(), http, guard)
fetch = FetchService(static, browser, DefaultHtmlExtractor(), PypdfExtractor(), robots, limiter, cache, events, settings)
```

Add `fetch: FetchService` to `Services` as a **required** field placed after `search`, so the field order is valid. Keep `extra` last. Then update `testing.build_test_services` to match: fake fetchers serve the fixture pages, a stub robots policy allows everything, and `fake-blocked.example` raises `ssrf_blocked`.

- [ ] **Step 6: Write the integration test.** `tests/integration/live_urls.py` holds `STATIC_ARTICLE`, `SPA` and `PDF`. `tests/integration/test_fetch_live.py` builds the real `FetchService` against `CRAWL4AI_LIVE_URL`, with the token read from the env var `CRAWL4AI_API_TOKEN`, and asserts the criteria above. Pick the SPA as described in the Acceptance Criteria and record the measured word counts in a comment.

- [ ] **Step 7: Run unit then integration.** Expected: all PASS. If the PDF host's robots.txt disallows the fetch, pick another public standards PDF and note it.

- [ ] **Step 8: Run the full step check** (Global Constraint 11).

- [ ] **Step 9: Commit.** `git commit -m "feat(fetch): tiered fetch service and /v1/fetch with live acceptance tests (V1-04, V1-05, V1-06, V1-11)"`

---

**End of step 3:** request code review, report to the owner with evidence (sandbox check output, unit output, integration output), and **stop** for approval. After approval, run `git push`.
