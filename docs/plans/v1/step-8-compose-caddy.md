# Step 8: Docker image, full Compose stack, shared Caddy

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:**
- V1-20 (Docker Compose);
- §9 co-tenancy rules, Compose layout and shared Caddy;
- D11, D12, D14, D15, D17.

**Outcome:**
- A hardened, non-root app image built with uv.
- The full `research-engine` Compose project. It follows every co-tenancy rule, and a policy test enforces them.
- The tool-agnostic shared Caddy project in `deploy/caddy/`.
- A first, owner-approved bring-up at `https://research.toolbox`.

**Task order:** 8.1, then 8.2, then 8.3, then 8.4. 8.4 changes the live host and needs the owner's approval.

---

### Task 8.1: App Dockerfile

**Goal:** Build a multi-stage, uv-built, non-root image that serves the app with uvicorn, includes the git SHA, and passes its own healthcheck.

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Test: `tests/test_dockerfile_policy.py`

**Acceptance Criteria:**
- [ ] **Builder stage:** `python:3.13.16-slim-trixie` with `COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/`, `UV_COMPILE_BYTECODE=1`, `UV_LINK_MODE=copy` and `UV_NO_DEV=1`. It runs a cache-mounted `uv sync --locked --no-install-workspace --package research-engine`, then copies the sources and runs `uv sync --locked --package research-engine --no-editable`.
- [ ] **Runtime stage:** the same base. It copies `/app/.venv`, `config/` and `schemas/`, then:
  - creates user `app` (uid/gid 10001);
  - sets `ENV PATH=/app/.venv/bin:$PATH DB_PATH=/data/research-engine.sqlite`;
  - takes `ARG GIT_SHA=unknown` into `ENV GIT_SHA`;
  - runs as `USER 10001`, `EXPOSE 8000`;
  - `HEALTHCHECK` with `python -c` and `urllib`, against `http://127.0.0.1:8000/health`;
  - `CMD ["uvicorn", "research_engine.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "15"]` (≥ the job runner's worst-case stop of ~9 s; also set compose `stop_grace_period: 20s` on `app`).
- [ ] `.dockerignore` excludes `.env*` (except `.env.example`), `.git`, `data`, `backups`, `logs`, `deploys.jsonl`, `.venv`, the caches, `tests` and `docs`.
- [ ] The policy test, which parses the text, asserts all of these:
  - a pinned base image (not `latest`, with a patch version);
  - a pinned uv image;
  - a non-root `USER`;
  - a `HEALTHCHECK`;
  - no `--no-sandbox` anywhere;
  - no `ADD http`;
  - `.dockerignore` contains `.env`.
- [ ] `docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t research-engine-app:test .` succeeds.
- [ ] `docker run --rm research-engine-app:test id -u` prints `10001`.

**Verify:** `uv run pytest tests/test_dockerfile_policy.py -v && docker build -q --build-arg GIT_SHA=$(git rev-parse --short HEAD) -t research-engine-app:test . && docker run --rm research-engine-app:test id -u` → pass, an image id, `10001`

**Steps:**

- [ ] **Step 1: Write the failing policy test**

```python
# tests/test_dockerfile_policy.py
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_policy() -> None:
    df = (ROOT / "Dockerfile").read_text()
    froms = re.findall(r"^FROM\s+(\S+)", df, re.MULTILINE)
    assert froms and all(re.search(r":\d+\.\d+\.\d+", f) for f in froms), froms
    assert "ghcr.io/astral-sh/uv:0.12.23" in df
    assert re.search(r"^USER\s+10001", df, re.MULTILINE)
    assert "HEALTHCHECK" in df and "--no-sandbox" not in df and not re.search(r"^ADD\s+https?://", df, re.MULTILINE)
    assert "uv sync --locked" in df


def test_dockerignore() -> None:
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env*" in ignore and "!.env.example" in ignore and ".git" in ignore and "data" in ignore
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Write the `Dockerfile`**

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.13.16-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages/research_engine/pyproject.toml,target=packages/research_engine/pyproject.toml \
    --mount=type=bind,source=packages/research_engine_client/pyproject.toml,target=packages/research_engine_client/pyproject.toml \
    uv sync --locked --no-install-workspace --package research-engine
COPY packages ./packages
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package research-engine --no-editable

FROM python:3.13.16-slim-trixie
ARG GIT_SHA=unknown
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 config ./config
COPY --chown=10001:10001 schemas ./schemas
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DB_PATH=/data/research-engine.sqlite GIT_SHA=${GIT_SHA}
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "research_engine.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "15"]
```

`--forwarded-allow-ips "*"` is acceptable because `app` publishes no port, so only Caddy (on the `proxy` network) and internal services can reach it. Record this in ADR-0026's notes.

The healthcheck treats a `degraded` status (200) as healthy, so the app stays up while SearXNG restarts. Only a `down` database (503) fails it.

- [ ] **Step 4: Write `.dockerignore`**, then build and run the Verify command.

- [ ] **Step 5: Commit.** `git commit -m "build: hardened non-root app image built with uv (V1-20)"`

---

### Task 8.2: Full `compose.yaml` and its policy test

**Goal:** Extend the minimal Compose file into the complete co-tenant stack: `app`, `searxng` and `crawl4ai`, with hardening, limits, log rotation, healthcheck dependencies, the external `proxy` network, and V2 profile stubs.

**Files:**
- Modify: `compose.yaml`
- Create: `compose.debug.yaml` (optional `APP_PORT` on `127.0.0.1` only)
- Modify: `.env.example` (limits, `APP_PORT`, `GIT_SHA` notes)
- Modify: `.github/dependabot.yml` (docker ecosystem for `/` and `/deploy/caddy`)
- Test: `tests/test_compose_policy.py`

**Acceptance Criteria:**
- [ ] `docker compose config -q` succeeds when only `.env.example` values are present.
- [ ] **The policy test** parses `compose.yaml` as YAML and asserts the following:

  *Project-wide:*
  - top-level `name: research-engine`;
  - no service has `container_name`, `privileged`, `network_mode: host`, `pid: host` or a `/var/run/docker.sock` mount;
  - every service has `restart: unless-stopped`, a `healthcheck`, `logging.options.max-size`/`max-file`, and `deploy.resources.limits.memory` and `cpus`;
  - every `image:` has an explicit non-`latest` tag;
  - no service except `app` joins `proxy`, and `proxy` is declared `external: true, name: proxy`;
  - no service publishes `ports` in `compose.yaml`;
  - all volumes are named (no host bind mounts except read-only `./deploy/...` config files);
  - the strings `--no-sandbox`, `cap_add: [SYS_ADMIN]` and `NET_ADMIN` don't appear;
  - `docker compose config` with no profiles lists exactly `app`, `searxng` and `crawl4ai`.

  *Per service:*
  - `app` and `crawl4ai` have `security_opt` containing `no-new-privileges:true` and `cap_drop: [ALL]`;
  - `app` has `read_only: true`, a `/tmp` tmpfs, `user: "10001:10001"` and the `app-data:/data` volume;
  - `shm_size` is set on `crawl4ai` only;
  - `app.depends_on` lists `searxng` and `crawl4ai` with `condition: service_healthy`.
- [ ] **`app` environment:** `app` gets its configuration from an explicit `environment:` mapping that references `.env` values (`API_KEY: ${API_KEY:?}` and so on). It does **not** use `env_file`, so `SEARXNG_SECRET` never reaches the app.
- [ ] **Debug port:** `compose.debug.yaml` publishes `127.0.0.1:${APP_PORT}:8000` for `app` only. It is used only when `APP_PORT` is set (step 9's ops scripts add it conditionally).
- [ ] **Running stack:** `docker compose -f compose.yaml -f compose.dev.yaml up -d --wait` brings all three services to healthy. This is the dev override; it doesn't touch Caddy or `proxy`. The `proxy` network must exist first; the step-8.4 bring-up creates it. For dev testing before then, `docker network create proxy` is acceptable **only if the owner approves**, because it is a VM-wide name. Otherwise `compose.dev.yaml` overrides `proxy` with a project-local network.

**Verify:** `uv run pytest tests/test_compose_policy.py -v && docker compose --env-file .env.example config -q && echo compose-ok` → pass, `compose-ok`

**Steps:**

- [ ] **Step 1: Write the failing policy test**

```python
# tests/test_compose_policy.py
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text())
RAW = "\n".join(line.split("#", 1)[0] for line in (ROOT / "compose.yaml").read_text().splitlines())  # comments stripped
SERVICES = COMPOSE["services"]


def test_project_name() -> None:
    assert COMPOSE["name"] == "research-engine"


def test_core_services_without_profiles() -> None:
    assert {n for n, s in SERVICES.items() if not s.get("profiles")} == {"app", "searxng", "crawl4ai"}


def test_forbidden_settings() -> None:
    for name, svc in SERVICES.items():
        for key in ("container_name", "privileged", "pid", "ports"):
            assert key not in svc, f"{name} sets {key}"
        assert svc.get("network_mode") != "host"
        assert not any("docker.sock" in str(v) for v in svc.get("volumes", []))
    assert "--no-sandbox" not in RAW and "SYS_ADMIN" not in RAW and "NET_ADMIN" not in RAW


def test_every_service_is_a_good_cotenant() -> None:
    for name, svc in SERVICES.items():
        assert svc["restart"] == "unless-stopped", name
        assert "healthcheck" in svc, name
        opts = svc["logging"]["options"]
        assert "max-size" in opts and "max-file" in opts, name
        limits = svc["deploy"]["resources"]["limits"]
        assert "memory" in limits and "cpus" in limits, name
        if "image" in svc:
            assert re.search(r":[\w.\-]+$", svc["image"]) and not svc["image"].endswith(":latest"), name


def test_hardening() -> None:
    for name in ("app", "crawl4ai"):
        svc = SERVICES[name]
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["cap_drop"] == ["ALL"], name
    app = SERVICES["app"]
    assert app["read_only"] is True and app["user"] == "10001:10001"
    assert any(str(t).startswith("/tmp") for t in app["tmpfs"])
    assert "env_file" not in app
    assert [n for n, s in SERVICES.items() if "shm_size" in s] == ["crawl4ai"]
    assert app["depends_on"]["searxng"]["condition"] == "service_healthy"
    assert app["depends_on"]["crawl4ai"]["condition"] == "service_healthy"


def test_crawl4ai_sandbox_protections_present() -> None:
    """ADR-0022: the sandbox depends on these three things; a compose rewrite must never drop them."""
    c4 = SERVICES["crawl4ai"]
    assert "seccomp=./deploy/crawl4ai/seccomp-chromium.json" in c4["security_opt"]
    assert "./deploy/crawl4ai/addon:/opt/re-addon:ro" in c4["volumes"]
    assert c4["environment"]["PYTHONPATH"] == "/opt/re-addon"
    assert c4["environment"]["CRAWL4AI_CHROMIUM_SANDBOX"] == "true"
    assert set(c4.get("cap_add", [])) <= {"CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER"}, \
        "only documented entrypoint capabilities may be added back; never SYS_ADMIN"


def test_networks() -> None:
    assert COMPOSE["networks"]["proxy"] == {"external": True, "name": "proxy"}
    on_proxy = [n for n, s in SERVICES.items() if "proxy" in (s.get("networks") or {})]
    assert on_proxy == ["app"]


def test_volumes_named_only() -> None:
    for name, svc in SERVICES.items():
        for v in svc.get("volumes", []):
            src = str(v).split(":", 1)[0]
            if src.startswith(("./", "/")):
                assert src.startswith("./deploy/") and str(v).endswith(":ro"), f"{name}: {v}"
```

- [ ] **Step 2: Run it.** Expected: FAIL.

- [ ] **Step 3: Write the full `compose.yaml`.** The YAML anchors `x-logging` and `x-hardening` keep it DRY.

```yaml
name: research-engine

x-logging: &logging
  driver: json-file
  options: { max-size: "10m", max-file: "5" }

services:
  app:
    build:
      context: .
      args: { GIT_SHA: "${GIT_SHA:-unknown}" }
    image: research-engine-app:${GIT_SHA:-dev}
    restart: unless-stopped
    stop_grace_period: 20s   # > uvicorn --timeout-graceful-shutdown 15 > job runner worst-case stop (~9 s)
    user: "10001:10001"
    read_only: true
    tmpfs: ["/tmp:size=64m"]
    security_opt: ["no-new-privileges:true"]
    cap_drop: [ALL]
    environment:
      API_KEY: ${API_KEY:?set API_KEY}
      SESSION_SECRET: ${SESSION_SECRET:?set SESSION_SECRET}
      CRAWL4AI_API_TOKEN: ${CRAWL4AI_API_TOKEN:?set CRAWL4AI_API_TOKEN}
      SITE_HOST: ${SITE_HOST:-research.localhost}
      SEARXNG_URL: http://searxng:8080
      CRAWL4AI_URL: http://crawl4ai:11235
      DB_PATH: /data/research-engine.sqlite
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      SSRF_ALLOW_HOSTS: ${SSRF_ALLOW_HOSTS:-}
      USER_AGENT: ${USER_AGENT:-ResearchEngine/0.1 (+https://github.com/pentonvillefandango/research-engine)}
      # every other Settings field: add here as ${NAME:-default} (keep in sync with .env.example; tested)
    volumes: ["app-data:/data"]
    depends_on:
      searxng: { condition: service_healthy }
      crawl4ai: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    deploy:
      resources:
        limits: { memory: "${APP_MEM_LIMIT:-1g}", cpus: "${APP_CPUS:-1.0}" }
    logging: *logging
    networks:
      internal: {}
      proxy:
        aliases: [research-engine-app]

  searxng:
    image: searxng/searxng:2026.10.4-d48c4b555
    restart: unless-stopped
    # hardening verified at implementation: start from cap_drop ALL and add back only what the entrypoint needs
    environment:
      SEARXNG_SECRET: ${SEARXNG_SECRET:?set SEARXNG_SECRET}
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
    deploy:
      resources:
        limits: { memory: "${SEARXNG_MEM_LIMIT:-512m}", cpus: "${SEARXNG_CPUS:-1.0}" }
    logging: *logging
    networks: [internal]

  crawl4ai:
    image: unclecode/crawl4ai:0.9.4
    restart: unless-stopped
    shm_size: 1gb
    # Chromium sandbox stays ON (ADR-0022): custom seccomp profile + read-only add-on. Never remove these.
    security_opt:
      - "no-new-privileges:true"
      - "seccomp=./deploy/crawl4ai/seccomp-chromium.json"
    cap_drop: [ALL]   # add back only named capabilities the entrypoint needs (see below), documented
    environment:
      CRAWL4AI_API_TOKEN: ${CRAWL4AI_API_TOKEN:?set CRAWL4AI_API_TOKEN}
      CRAWL4AI_CHROMIUM_SANDBOX: "true"
      PYTHONPATH: /opt/re-addon
    volumes:
      - ./deploy/crawl4ai/addon:/opt/re-addon:ro
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://127.0.0.1:11235/health || exit 1"]
      interval: 20s
      timeout: 5s
      retries: 6
      start_period: 40s
    deploy:
      resources:
        limits: { memory: "${CRAWL4AI_MEM_LIMIT:-4g}", cpus: "${CRAWL4AI_CPUS:-2.0}" }
    logging: *logging
    networks: [internal]

  # ---- V2 (not part of V1; kept as documentation of the intended shape) ----
  # gluetun-uk:
  #   profiles: [vpn]
  #   image: qmcgaw/gluetun:<pinned>
  #   cap_add: [NET_ADMIN]   # Gluetun only (§9)
  #   devices: ["/dev/net/tun:/dev/net/tun"]
  # ollama:
  #   profiles: [ollama]
  #   image: ollama/ollama:<pinned>

networks:
  internal: {}
  proxy:
    external: true
    name: proxy

volumes:
  app-data: {}
  searxng-cache: {}
```

The policy test strips comments before matching, so the commented V2 stub (which mentions `NET_ADMIN`) doesn't trip it.

The crawl4ai block above already carries the step-3 sandbox settings (seccomp profile, read-only add-on mount via `PYTHONPATH`, `CRAWL4AI_CHROMIUM_SANDBOX`). Keep them exactly; `test_crawl4ai_sandbox_protections_present` enforces them. If Crawl4AI's image can't run `read_only`, leave it off and record why in ADR-0022. The policy test requires `read_only` for `app` only.

**After any change to the crawl4ai service, re-run `scripts/check_sandbox.sh` and require `"sandbox":"on"` with `default_ok`, `builtin_ok` and `addon_active` true.** In step 3 a throwaway test showed the container went unhealthy under `cap_drop: [ALL]` before any browser started; the cause is unknown. Read the container logs to find it before adding anything back.

**Crawl4AI's internal Redis** must be able to write. If `cap_drop: [ALL]` breaks the image's entrypoint (for example, it needs `SETUID`/`SETGID` to drop to `appuser`), add back only the named capabilities it needs. Find them by reading the entrypoint, not by trial and error, and document them in a comment.

- [ ] **Step 4: Update `compose.dev.yaml`.** Keep the ephemeral loopback ports, and override `networks.proxy` to a project-local network, so dev runs never need the VM-wide `proxy` network:

```yaml
networks:
  proxy:
    external: false
    name: research-engine-dev-proxy
```

- [ ] **Step 5: Write `compose.debug.yaml`.** Use `services: { app: { ports: ["127.0.0.1:${APP_PORT:?}:8000"] } }`.

- [ ] **Step 6: Run the stack checks.** Run `docker compose --env-file .env.example config -q`, then a dev bring-up: `docker compose -f compose.yaml -f compose.dev.yaml up -d --build --wait`, then `docker compose ps` to confirm everything is healthy. Re-run `scripts/check_sandbox.sh` to confirm the hardening didn't break the sandbox.

- [ ] **Step 7: Update Dependabot and commit.** Add docker update entries for `/` and `/deploy/caddy`, then `git commit -m "build: full co-tenant compose stack with hardening, limits and policy test (V1-20, §9)"`.

---

### Task 8.3: Shared Caddy project and site file

**Goal:** Build the shared Caddy project in `deploy/caddy/`. It is tool-agnostic and owns ports 80/443 and the `proxy` network. It includes this tool's site file (lab-subnet restriction, SSE-safe proxying, `tls internal`) and a `toolbox` index page.

**Files:**
- Create: `deploy/caddy/compose.yaml`, `deploy/caddy/Caddyfile`, `deploy/caddy/sites/research-engine.caddy`, `deploy/caddy/index/index.html`, `deploy/caddy/.env.example`, `deploy/caddy/README.md`
- Test: `tests/test_caddy_policy.py`

**Acceptance Criteria:**
- [ ] `deploy/caddy/compose.yaml` contains:
  - `name: caddy`;
  - the service `caddy` with image `caddy:2.11.6`;
  - `ports: ["80:80", "443:443", "443:443/udp"]`;
  - the network `proxy` declared with `name: proxy`. The Caddy project creates it; it is not external here.
  - named volumes `caddy-data` and `caddy-config`;
  - read-only bind mounts of `./Caddyfile`, `./sites` and `./index`;
  - `env_file: .env` (holding `SITE_HOST`, `LAB_SUBNET` and `TOOLBOX_HOST`);
  - `restart: unless-stopped`, log rotation, limits (256m, 0.5 CPU) and a healthcheck (`wget -q -O /dev/null http://127.0.0.1:2019/config/`, which hits the admin API on localhost inside the container).
- [ ] `Caddyfile` contains:
  - a global block with `admin 127.0.0.1:2019`;
  - `import sites/*.caddy`;
  - the index site `{$TOOLBOX_HOST:toolbox} { tls internal; root * /srv/index; file_server }`.
- [ ] `sites/research-engine.caddy`:
  - serves `{$SITE_HOST}` with `tls internal`;
  - uses `@lab remote_ip {$LAB_SUBNET}`;
  - `handle @lab { reverse_proxy research-engine-app:8000 { flush_interval -1 } }`;
  - responds `403` for everything else;
  - has a commented plain-HTTP alternative, `http://{$SITE_HOST}`.
- [ ] `caddy validate` passes. It runs as `docker run --rm -e SITE_HOST=research.localhost -e LAB_SUBNET=127.0.0.1/32 -e TOOLBOX_HOST=toolbox.localhost -v "$PWD/deploy/caddy":/etc/caddy:ro caddy:2.11.6 caddy validate --config /etc/caddy/Caddyfile`. It's a one-off container with no ports, and `--rm`.
- [ ] The policy test asserts the pinned image, `name: caddy`, no `latest`, `flush_interval -1` and the `remote_ip` matcher present, and **no** hard-coded IPs or real hostnames in the repo files except the `toolbox` default placeholder.
- [ ] `deploy/caddy/README.md` covers:
  - install: copy to `/opt/caddy`, create `.env` from `.env.example`, then `docker compose up -d`;
  - adding a tool: drop a file in `sites/`, then `docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`;
  - the plain-HTTP alternative;
  - trusting Caddy's root CA on macOS: export it with `docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .`, then `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root.crt`.

**Verify:** `uv run pytest tests/test_caddy_policy.py -v && docker run --rm -e SITE_HOST=research.localhost -e LAB_SUBNET=127.0.0.1/32 -e TOOLBOX_HOST=toolbox.localhost -v "$PWD/deploy/caddy":/etc/caddy:ro caddy:2.11.6 caddy validate --config /etc/caddy/Caddyfile` → pass, `Valid configuration`

**Steps:**

- [ ] **Step 1: Write the failing policy test.** Follow the style of `test_compose_policy.py`. Also assert the site file contains `flush_interval -1`, `remote_ip {$LAB_SUBNET}` and `reverse_proxy research-engine-app:8000`. Then run a regex over every file in `deploy/caddy/` for IPv4 literals (`\b\d{1,3}(\.\d{1,3}){3}\b`), allowing only `127.0.0.1` and documentation ranges in comments.

- [ ] **Step 2: Write the files** as specified. `index/index.html` is a small static page: "Tools on this host", with one link to `https://research.toolbox/`. Add a comment that hostnames are examples and are edited per deployment.

- [ ] **Step 3: Run Verify.** Expected: pass.

- [ ] **Step 4: Commit.** `git commit -m "feat(deploy): shared Caddy project with research-engine site file (D15, D17, V1-20)"`

---

### Task 8.4: First live bring-up (owner-approved)

**Goal:** With the owner's explicit approval, install the shared Caddy at `/opt/caddy`, start the research-engine stack, and confirm `https://research.toolbox` serves the GUI and API.

> **USER-ORDERED GATE: NON-SKIPPABLE.** This step starts services on the shared host and publishes ports 80 and 443. It MUST NOT run until the owner approves in the conversation. Close it only with captured evidence for every acceptance criterion.

**Files:** none in the repo. This creates `/opt/caddy/` (copied from `deploy/caddy/`) and `/opt/caddy/.env`, both outside git.

**Acceptance Criteria:**
- [ ] Before anything starts, the owner has approved, and confirmed that nothing else on the VM uses ports 80/443 (`ss -ltnp '( sport = :80 or sport = :443 )'` is empty).
- [ ] `/opt/caddy/.env` holds the owner's `SITE_HOST=research.toolbox`, `LAB_SUBNET` (their lab CIDR, supplied by them) and `TOOLBOX_HOST=toolbox`. It is mode 600 and never committed.
- [ ] `cd /opt/caddy && docker compose up -d --wait` reports `caddy` healthy. `docker network inspect proxy` exists.
- [ ] `cd /opt/research-engine && GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build --wait` reports `app`, `searxng` and `crawl4ai` healthy.
- [ ] The owner has added the UniFi DNS records (A for `toolbox`, CNAME for `research.toolbox`) and trusted the Caddy root CA on their MacBook.
- [ ] From the VM, `curl -sk --resolve research.toolbox:443:127.0.0.1 https://research.toolbox/health` returns `"status":"up"`. From a lab client, `https://research.toolbox/` shows the login page. From a non-lab IP, it returns 403. The last check can only be done if the owner can test it; if not, record it as untested.
- [ ] The owner runs `examples/openai_agents_mcp.py` against `https://research.toolbox` and confirms the tool calls and the cited answer (B6b).

**Verify:** `curl -sk --resolve research.toolbox:443:127.0.0.1 https://research.toolbox/health | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["status"])'` → `up`

**Steps:**

- [ ] **Step 1: Ask the owner for approval and their `LAB_SUBNET`.** Explain exactly what will start and which ports open.
- [ ] **Step 2: Install Caddy.** `sudo` isn't needed, because `/opt/caddy` is owned by `admin`. Run `cp -r deploy/caddy/. /opt/caddy/`, then create `/opt/caddy/.env` from `.env.example` with the owner's values and `chmod 600` it.
- [ ] **Step 3:** `cd /opt/caddy && docker compose up -d --wait`.
- [ ] **Step 4: Start the stack.** Stop the dev override stack first, with `docker compose -f compose.yaml -f compose.dev.yaml down`. **Never pass `-v`.** Then run `GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build --wait`.
- [ ] **Step 5: Run the Verify command and the other checks.** Capture the output.
- [ ] **Step 6: Owner checks.** The owner does the DNS, CA trust, browser and agent example checks, and confirms each in the conversation.
- [ ] **Step 7:** No commit is needed. Record the evidence in the step report.

---

**End of step 8:** request code review, report to the owner with evidence, and **stop** for approval. After approval, run `git push`.
