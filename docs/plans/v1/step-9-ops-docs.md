# Step 9: Ops scripts, Makefile, CLAUDE.md, OPERATIONS.md and deployment docs

> Part of `docs/plans/2026-10-04-research-engine-v1.md`. Read that file first for the Global Constraints and the interface contract.

**Branch:** `v1`. **Feature refs:** V1-22, §10 (ops commands, release and deploy rules, guardrails, ops acceptance criteria), §8 (README for outsiders), D18, D19.

**Outcome:**
- Every operator action is a scripted, idempotent, non-interactive command that ends with a single JSON line and returns a meaningful exit code.
- `make deploy` builds the **exact committed tree** (from a git worktree) and runs the smoke test. If the smoke test fails, it rolls back automatically and records the outcome in `deploys.jsonl`.
- Backups, restore and bootstrap work as described in §10.
- The docs serve outsiders, the owner, and Claude.

**Ops script conventions (all tasks):**
- Scripts are bash with `set -euo pipefail`, and every script sources `ops/lib.sh`.
- The **last stdout line** is one JSON object: `{"ok": bool, "command": str, ...}`. Human-readable progress goes to **stderr**.
- Exit codes: 0 means success, 1 means the check or command failed, 2 means a usage or precondition error.
- No script prints secrets. `ops/lib.sh` provides `redact`, which masks the value of any `*KEY*`, `*TOKEN*`, `*SECRET*` or `*PASSWORD*` variable.
- Scripts address the stack only as `docker compose -p research-engine ...`. They never touch other projects, never run `docker system prune`, and never pass `down -v`.
- Scripts are testable. `PATH` can be prefixed with a directory of fake `docker`, `curl` and `systemctl` executables. The tests in `tests/ops/` do this and run the scripts with `subprocess`.

**Task order:**
- 9.1 runs first.
- 9.2, 9.3 and 9.4 follow 9.1. They touch disjoint scripts but share `Makefile`, so run them serially or have one worker own `Makefile`.
- 9.5 follows them, then 9.6, then 9.7.
- 9.6 and 9.7 need the owner's approval.

---

### Task 9.1: `ops/lib.sh`, Makefile skeleton, `status`, `health`, `version`, `logs`

**Goal:** Add the shared helpers, the four read-only commands, and the Makefile wrapping every ops command.

**Files:**
- Create: `ops/lib.sh`, `ops/status.sh`, `ops/health.sh`, `ops/version.sh`, `ops/logs.sh`, `Makefile`
- Test: `tests/ops/conftest.py` (fake-bin fixture), `tests/ops/test_readonly_ops.py`

**Acceptance Criteria:**
- [ ] `ops/lib.sh` provides:
  - `REPO_DIR`, `ENV_FILE` (default `$REPO_DIR/.env`) and `DEPLOYS_LOG` (`$REPO_DIR/deploys.jsonl`);
  - `log` (to stderr), `json_out` (build the final JSON line with `python3 -c 'import json,sys; ...'` from `key=value` arguments, so quoting is always correct), `fail` (code + message, then JSON with `ok:false` and exit);
  - `dc` (`docker compose -p research-engine --project-directory "$DEPLOY_DIR" -f "$DEPLOY_DIR/compose.yaml" --env-file "$ENV_FILE"`, adding `-f compose.debug.yaml` when `APP_PORT` is set in `.env`);
  - `env_get NAME` (reads one value from `.env` without sourcing it), `redact`, and `require_cmd`.
- [ ] `DEPLOY_DIR` defaults to the current live worktree, from the symlink `$REPO_DIR/.deploy/current`, and falls back to `$REPO_DIR` before the first deploy. `.deploy/` is added to `.gitignore`.
- [ ] `make status` prints JSON with:
  - per container: name, state, health, restart count, CPU % and memory (`docker stats --no-stream` filtered to this project's containers);
  - volume usage (`docker system df -v`, filtered to `research-engine_*` volumes; read-only, no prune);
  - the running commit and tag (from `/version` via the app container);
  - the last deploy (the last line of `deploys.jsonl`);
  - the last backup (the newest file in `backups/`: name, size and time).

  No value of any secret appears. A test greps the output for the fake `.env` secrets.
- [ ] `make health` calls `/health` via `dc exec -T app python -c ...` (no host port needed) and reads each container's health. It exits 0 only if `/health` reports `up` and every container is `healthy`.
- [ ] `make version` prints the running commit, tag and image versions (`dc images --format json`).
- [ ] `make logs SERVICE=app SINCE=30m` passes through `dc logs --no-color --since $SINCE $SERVICE`. App logs are already JSON. The final line is the JSON summary `{ok, service, lines}`. An unknown `SERVICE` exits 2.
- [ ] The `Makefile` has `.PHONY` targets for every §10 command plus `help` and `test`. Each target is a one-line call to `ops/<cmd>.sh` that passes variables through. `make help` lists each target with a one-line description.

**Verify:** `uv run pytest tests/ops/test_readonly_ops.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the fake-bin fixture and the failing tests.** `tests/ops/conftest.py` provides `fake_env(tmp_path)`, which:
  - creates a temporary repo copy containing only `ops/` and `Makefile`;
  - writes a `.env` holding fake secrets (`API_KEY=SEKRIT-API`, `SEARXNG_SECRET=SEKRIT-SX`, and so on);
  - creates a `bin/` directory with a fake `docker` script. It records its argv to `calls.log` and prints canned JSON chosen by the subcommand, taken from `tests/ops/fixtures/docker/*.json`.
  - returns a `run(cmd, **env)` helper that executes `bash ops/<cmd>.sh` with `PATH=bin:$PATH` and parses the last stdout line as JSON.

  The tests cover:
  - `status` output has keys `containers`, `volumes`, `version`, `last_deploy`, `last_backup`, and no `SEKRIT` anywhere in stdout or stderr;
  - `health` exits 1 when the fake reports one container `unhealthy`;
  - `logs SERVICE=nope` exits 2;
  - every `docker` call in `calls.log` contains `-p research-engine` and no `prune` or `down -v`.

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement `ops/lib.sh`**

```bash
#!/usr/bin/env bash
# Shared helpers for ops scripts. Contract: progress on stderr; final stdout line is one JSON object.
set -euo pipefail
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
DEPLOYS_LOG="${DEPLOYS_LOG:-$REPO_DIR/deploys.jsonl}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"
PROJECT=research-engine
if [ -z "${DEPLOY_DIR:-}" ]; then
  if [ -L "$REPO_DIR/.deploy/current" ]; then DEPLOY_DIR="$(readlink -f "$REPO_DIR/.deploy/current")"; else DEPLOY_DIR="$REPO_DIR"; fi
fi

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

json_out() {  # json_out key=value ... ; values that parse as JSON are embedded as JSON
  python3 - "$@" <<'PY'
import json, sys
out = {}
for arg in sys.argv[1:]:
    k, _, v = arg.partition("=")
    try:
        out[k] = json.loads(v)
    except ValueError:
        out[k] = v
print(json.dumps(out, sort_keys=True))
PY
}

fail() {  # fail <exit-code> <command> <message> [extra key=value...]
  local code=$1 cmd=$2 msg=$3; shift 3
  json_out ok=false command="$cmd" error="$msg" "$@"
  exit "$code"
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || fail 2 "${CMD:-ops}" "missing required command: $1"; }

env_get() {  # read NAME from .env without sourcing it
  [ -f "$ENV_FILE" ] || return 1
  grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2-
}

redact() { sed -E 's/((KEY|TOKEN|SECRET|PASSWORD)[A-Z_]*=)[^ "]*/\1***REDACTED***/g'; }

dc() {
  local files=(-f "$DEPLOY_DIR/compose.yaml")
  if [ -n "$(env_get APP_PORT || true)" ]; then files+=(-f "$DEPLOY_DIR/compose.debug.yaml"); fi
  docker compose -p "$PROJECT" --project-directory "$DEPLOY_DIR" "${files[@]}" --env-file "$ENV_FILE" "$@"
}

app_get() {  # app_get /path  -> body of an in-container GET to the app (no host port needed)
  dc exec -T app python -c "import sys,urllib.request;r=urllib.request.urlopen('http://127.0.0.1:8000$1',timeout=10);sys.stdout.write(r.read().decode())"
}
```

- [ ] **Step 4: Implement `status.sh`, `health.sh`, `version.sh`, `logs.sh` and the `Makefile`.** Each script sets `CMD=<name>`, sources `lib.sh`, does its work, and ends with `json_out ok=true command=<name> ...`. Container stats use `docker stats --no-stream --format '{{json .}}'` restricted to `$(dc ps -q)`. Example `Makefile`:

```makefile
.PHONY: help bootstrap deploy rollback status health smoke logs backup restore version test
SERVICE ?= app
SINCE ?= 30m

help:            ## List ops commands
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'
bootstrap:       ## One-off idempotent VM setup (needs sudo; owner runs or approves)
	@ops/bootstrap.sh
deploy:          ## Build + start the current commit, smoke test, auto-rollback on failure (approval required)
	@ops/deploy.sh
rollback:        ## Redeploy the last good commit from deploys.jsonl (approval required)
	@ops/rollback.sh
status:          ## Containers, health, resources, version, last deploy/backup (read-only)
	@ops/status.sh
health:          ## /health plus container healthchecks (read-only)
	@ops/health.sh
smoke:           ## Demo set against the live API, <2 min (read-only)
	@ops/smoke.sh
logs:            ## Recent logs: make logs SERVICE=app SINCE=30m (read-only)
	@ops/logs.sh "$(SERVICE)" "$(SINCE)"
backup:          ## Online SQLite backup to backups/ with retention
	@ops/backup.sh
restore:         ## Restore a backup: make restore FILE=backups/x.sqlite (approval required)
	@ops/restore.sh "$(FILE)"
version:         ## Running commit, tag and image versions (read-only)
	@ops/version.sh
test:            ## Lint, type-check and unit tests
	@uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest
```

- [ ] **Step 5: Run the tests.** Expected: PASS.

- [ ] **Step 6: Commit.** `git commit -m "feat(ops): ops library, Makefile and read-only status/health/version/logs (V1-22)"`

---

### Task 9.2: `make smoke`

**Goal:** Run the test-console demo set against the live API in under 2 minutes, with a JSON pass/fail per check.

**Files:**
- Create: `packages/research_engine/src/research_engine/smoke.py` (logic, using the typed client)
- Modify: `packages/research_engine/src/research_engine/cli.py` (`research-engine smoke --url --timeout`)
- Create: `ops/smoke.sh`
- Test: `tests/service/unit/test_smoke.py`

**Acceptance Criteria:**
- [ ] `run_smoke(client, demos, deadline_s=110)` runs these checks, using URLs from `config/demos.yaml`:

  | Check | Passes when |
  |---|---|
  | `version` | always |
  | `health` | status `up` or `degraded` |
  | `search` | the technical demo returns at least 5 results |
  | `fetch_static` | the Python docs demo returns at least 100 words |
  | `fetch_pdf` | the PDF demo returns at least 100 words |
  | `search_read` | `top_n=2` ends `done` or `partial` with at least 1 document |

  Every check runs with `use_cache=false`, so the smoke test exercises the real path. The JS-app demo isn't in smoke because it's slow and the integration tests cover it.
- [ ] It returns `{"ok": all_passed, "checks": [{"name", "ok", "ms", "detail"}], "took_ms"}`. Checks still pending at the deadline are marked failed with `detail="deadline"`.
- [ ] `ops/smoke.sh` runs the CLI **inside the app container** (`dc exec -T app research-engine smoke --url http://127.0.0.1:8000`), so no host port or TLS trust is needed. The API key comes from the container's own environment. It prints the CLI's JSON line and exits 0 or 1.
- [ ] The unit test runs `run_smoke` against the test app through ASGITransport. Fake services pass every check. A fake that makes `search` fail produces `ok=false` and a failing `search` check.

**Verify:** `uv run pytest tests/service/unit/test_smoke.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test.**
- [ ] **Step 2: Implement `smoke.py`.** Each check is an async function wrapped by a timing and exception guard. Together they run under `asyncio.timeout(deadline_s)`, sequentially, to keep the load on the host light.
- [ ] **Step 3: Implement the CLI subcommand.** It reads `API_KEY` from the environment and prints exactly one JSON line.
- [ ] **Step 4: Implement `ops/smoke.sh`.**
- [ ] **Step 5: Run the tests.** Expected: PASS.
- [ ] **Step 6: Commit.** `git commit -m "feat(ops): smoke test of the demo set via the typed client (V1-22, V1-21)"`

---

### Task 9.3: `make deploy` and `make rollback`

**Goal:** Deploy exactly the committed tree, smoke-test it, roll back automatically on failure, and record every action in `deploys.jsonl`.

**Files:**
- Create: `ops/deploy.sh`, `ops/rollback.sh`, `ops/_deploy_lib.sh`
- Modify: `.gitignore` (add `.deploy/`)
- Test: `tests/ops/test_deploy.py`

**Acceptance Criteria:**
- [ ] **`deploy.sh`:**
  1. Refuses with exit 2, and `{"ok":false,"error":"working tree has uncommitted changes"}`, if `git status --porcelain` (excluding ignored files) is non-empty.
  2. Reads the target with `TARGET=$(git rev-parse HEAD)`. Reads the previous version as the `to` of the last `ok` entry in `deploys.jsonl`, or `none`.
  3. Creates or reuses the worktree `.deploy/<short-sha>` with `git worktree add --detach .deploy/<sha> <sha>`, and points the symlink `.deploy/current` at it.
  4. Runs `GIT_SHA=<short> dc up -d --build --wait --wait-timeout 300`, then `ops/smoke.sh`.
  5. On success, appends `{"ts","action":"deploy","from","to","result":"ok","smoke":"pass"}` and prints JSON with `ok:true`.
  6. On a failed `up` or smoke, appends `result:"failed"` with the smoke detail, then **automatically** runs the rollback routine to the last good commit. It appends that `rollback` entry and exits 1 with `{"ok":false,"rolled_back_to":"<sha>"}`. If there's no previous good commit, it exits 1 with `rolled_back_to:null` and leaves the failed stack up for diagnosis.
- [ ] **`rollback.sh`:**
  - It picks the target: `SHA` if given, otherwise the last `ok` entry's `to` **that differs from the current one**.
  - It creates the branch `rollback/<UTC-ts>` at that sha (so no work is lost, per §10), adds a worktree for it, and repoints `current`.
  - It runs `up` with `--wait` and then smoke, and appends `{"action":"rollback",...}`.
  - It never runs `git reset` or checkout in the main working tree.
- [ ] **Worktree cleanup:** old worktrees under `.deploy/` are pruned to the 3 most recent plus `current`, using `git worktree remove`, and only under `.deploy/`.
- [ ] **`deploys.jsonl`** is append-only, one JSON object per line, and parseable by `json.loads`. Times are UTC ISO-8601.
- [ ] **Tests:** the tests use a temporary git repo with two commits, the fake `docker` (whose `up` succeeds), and a fake smoke selected via `SMOKE_CMD`. They prove:
  - a dirty tree is refused;
  - a successful deploy appends `ok`;
  - a failing smoke triggers a rollback to the previous commit, with the `rollback/` branch created, the `current` symlink pointing at the old sha, and both entries recorded;
  - the main working tree's HEAD and files are untouched throughout.

**Verify:** `uv run pytest tests/ops/test_deploy.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.**
- [ ] **Step 2: Implement `_deploy_lib.sh`** with these functions:
  - `last_good_sha [exclude]`, using `python3` to scan `deploys.jsonl` backwards;
  - `record action from to result smoke detail`;
  - `activate_worktree sha [branch]`;
  - `bring_up short_sha`;
  - `run_smoke`, which defaults to `ops/smoke.sh` and can be overridden by `SMOKE_CMD` for tests;
  - `prune_worktrees`.

  Then write `deploy.sh` and `rollback.sh` on top of these.
- [ ] **Step 3: Run the tests.** Expected: PASS.
- [ ] **Step 4: Commit.** `git commit -m "feat(ops): worktree-based deploy with smoke test and automatic rollback (V1-22, §10)"`

---

### Task 9.4: `make backup`, `make restore`, `make bootstrap`

**Goal:** Online SQLite backup with retention, restore with validation and a health check, and idempotent VM bootstrap (Caddy install, `proxy` network, `.env` creation, nightly backup timer).

**Files:**
- Modify: `packages/research_engine/src/research_engine/cli.py` (`research-engine db backup --out` and `research-engine db restore --from`)
- Create: `ops/backup.sh`, `ops/restore.sh`, `ops/bootstrap.sh`
- Create: `deploy/systemd/research-engine-backup.service`, `deploy/systemd/research-engine-backup.timer`
- Test: `tests/service/unit/test_db_cli.py`, `tests/ops/test_backup_restore.py`, `tests/ops/test_bootstrap.py`

**Acceptance Criteria:**
- [ ] **`research-engine db backup --out PATH`** uses `sqlite3.Connection.backup`, which is safe while the app is writing in WAL mode, then runs `PRAGMA integrity_check` on the copy. It prints `{ok, path, bytes}`.
- [ ] **`research-engine db restore --from PATH`** validates the source: `integrity_check` returns `ok`, and the tables `job`, `event` and `cache_entry` exist. It then copies the source into `DB_PATH` with the backup API and removes stale `-wal` and `-shm` files. The unit tests cover both commands with temporary files.
- [ ] **`ops/backup.sh`:**
  1. Runs `dc exec -T app research-engine db backup --out /data/.backup-<ts>.sqlite`.
  2. Copies the result with `dc cp app:/data/.backup-<ts>.sqlite "$BACKUP_DIR/research-engine-<ts>.sqlite"` and removes the in-container temporary file.
  3. Applies retention: it deletes only files matching `research-engine-*.sqlite` in `$BACKUP_DIR` older than `BACKUP_RETENTION_DAYS` (default 14).
  4. Prints `{ok, file, bytes, pruned}`.
- [ ] **`ops/restore.sh FILE`:**
  1. Refuses with exit 2 when `FILE` is missing, outside `$BACKUP_DIR`, or not a `research-engine-*.sqlite` file.
  2. `dc stop app`.
  3. `dc run --rm --no-deps -v "$FILE":/restore.sqlite:ro app research-engine db restore --from /restore.sqlite`.
  4. `dc start app`, then `ops/health.sh`.
  5. Prints `{ok, restored, health}`.
- [ ] **`ops/bootstrap.sh`** is idempotent, non-interactive, and supports `--dry-run`, which prints the planned actions as JSON and changes nothing. It does the following:
  1. Checks that `docker` and `docker compose` exist.
  2. Checks that `/opt/research-engine` is owned by the current user.
  3. If `.env` is missing, creates it from `.env.example`. It generates `API_KEY`, `SESSION_SECRET`, `SEARXNG_SECRET` and `CRAWL4AI_API_TOKEN` with `openssl rand -hex 32` and sets mode 600. **It never prints the values.** An existing `.env` is never overwritten.
  4. Installs or updates `/opt/caddy` from `deploy/caddy/` with `rsync --exclude .env`. If `/opt/caddy/.env` is missing, it requires `SITE_HOST` and `LAB_SUBNET` in the environment, or exits 2 saying what to set.
  5. Runs `docker compose up -d --wait` in `/opt/caddy`, which creates the `proxy` network.
  6. Installs the systemd units with `sudo install -m 644`, then `sudo systemctl daemon-reload` and `sudo systemctl enable --now research-engine-backup.timer`. The timer runs daily at 03:30 with `Persistent=true`, as `User=admin`, with `WorkingDirectory=/opt/research-engine`. It's the only sudo use in the script.
  7. Prints `{ok, actions:[...]}`.
- [ ] **Tests:** the fake-bin tests cover bootstrap `--dry-run` (no files changed and correct planned actions), `.env` generation (mode 600, four non-empty distinct secrets, none appearing in stdout or stderr), the restore path-validation refusals, and backup retention pruning only the matching files.

**Verify:** `uv run pytest tests/service/unit/test_db_cli.py tests/ops/test_backup_restore.py tests/ops/test_bootstrap.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.**
- [ ] **Step 2: Implement the CLI `db` subcommands.** They use synchronous `sqlite3`; it's a one-shot CLI.
- [ ] **Step 3: Implement the three scripts and the systemd units.** The `.service` file runs `ExecStart=/usr/bin/make -C /opt/research-engine backup`. The repo path is the documented example; `bootstrap.sh` writes units with `REPO_DIR` substituted, so nothing lab-specific is committed.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(ops): backup with retention, validated restore, idempotent bootstrap and backup timer (V1-22, §10)"`

---

### Task 9.5: Documentation: README, OPERATIONS.md, deploy-toolbox.md, CLAUDE.md, ADR updates

**Goal:** Write documentation for outsiders (README), the owner (OPERATIONS and the toolbox deployment example) and Claude (CLAUDE.md updated with the real commands and guardrails), and bring the ADRs up to date.

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/adr/0022-chromium-sandbox.md` (if it still needs updating), `docs/adr/0026-ssrf-and-robots-semantics.md`
- Create: `docs/OPERATIONS.md`, `docs/deploy-toolbox.md`
- Test: `tests/test_docs.py`

**Acceptance Criteria:**
- [ ] **`README.md`** covers:
  - what the project is and its status;
  - features (V1);
  - a quick start: clone, `cp .env.example .env`, generate secrets, `docker compose -f compose.yaml -f compose.dev.yaml up -d --build --wait`, then open the app. The dev override needs no Caddy.
  - configuration (a table of every env var);
  - API usage (curl examples for each endpoint, the envelope shape, errors, jobs and long-polling);
  - MCP (the URL, the `X-API-Key` header, and a client config snippet);
  - the Python client;
  - the GUI (login, dashboard, test console);
  - security notes (SSRF, robots, sandbox, untrusted content);
  - a link to the deployment example;
  - development (uv, tests, integration tests, pre-commit);
  - licence.

  It names no lab IPs. `research.toolbox.home.arpa` appears only in the deployment-example section.
- [ ] **`docs/OPERATIONS.md`** covers:
  - each `make` command with its output shape and exit codes;
  - the deploy and rollback flow, including the worktree layout and `deploys.jsonl`;
  - backup and restore;
  - the incident checklist (health is degraded or down, a container is restarting, the disk is filling, a bad deploy, SearXNG engines are blocked);
  - the guardrails from §10.
- [ ] **`docs/deploy-toolbox.md`**, the example deployment, covers:
  - the Proxmox VM (Debian, 8 vCPU and 16 GB to start);
  - the QEMU guest agent;
  - the fixed IP by DHCP reservation;
  - Docker install;
  - `make bootstrap`, with the `SITE_HOST` and `LAB_SUBNET` env vars;
  - the UniFi DNS records (an A record for `toolbox.home.arpa`, a CNAME for `research.toolbox.home.arpa` → `toolbox.home.arpa`, with the 9.3 and 9.4 menu paths from §9);
  - Caddy root CA trust on macOS;
  - `make deploy`;
  - verification.
- [ ] **`CLAUDE.md`** is updated with:
  - the real commands (`make test` and the ops commands);
  - the approval rules (deploy, rollback, restore and bootstrap always ask);
  - the destructive-command deny list;
  - the integration-test procedure using the dev stack and `scripts/dev_urls.sh`;
  - where fixtures come from;
  - a reminder that Superpowers is the working method.
- [ ] **`tests/test_docs.py`** asserts the following:
  - every `make` target in the `Makefile` is mentioned in OPERATIONS.md;
  - every `.env.example` key is mentioned in the README configuration table;
  - no IPv4 literal other than `127.0.0.1` or `0.0.0.0`, and no `/home/` path, appears in `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, `docs/OPERATIONS.md`, `docs/deploy-toolbox.md` or `docs/adr/*.md`. Plans and specs are excluded because their test code deliberately uses example public and private addresses;
  - every ADR file has the sections `Status`, `Context`, `Decision` and `Consequences`.

**Verify:** `uv run pytest tests/test_docs.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing doc tests.**
- [ ] **Step 2: Write the documents.**
- [ ] **Step 3: Run the tests.** Expected: PASS.
- [ ] **Step 4: Run the full step check** (Global Constraint 11).
- [ ] **Step 5: Commit.** `git commit -m "docs: README, OPERATIONS, toolbox deployment guide, CLAUDE.md and ADR updates (V1-22, §8)"`

---

### Task 9.6: Ops acceptance run (owner-approved)

**Goal:** Prove each §10 ops acceptance criterion on the live host, with captured evidence.

> **USER-ORDERED GATE: NON-SKIPPABLE.** This task restarts and restores the live service. Every `make deploy`, `make rollback`, `make restore` and `make bootstrap` in it needs the owner's approval at the moment it runs. Close it only after every acceptance criterion has been re-checked independently, with the output captured.

**Files:** none (evidence only). The deliberately broken commit lives on the throwaway branch `ops-acceptance/broken`, which is deleted afterwards and never pushed.

**Acceptance Criteria:**
- [ ] **Bootstrap:** `make bootstrap` (run by the owner because it needs sudo) finishes with `{"ok":true}`, and a second run is a no-op (idempotent). `systemctl list-timers research-engine-backup.timer` shows the next run.
- [ ] **Deploy:** `make deploy` of the current `v1` HEAD gives `{"ok":true}`. `deploys.jsonl` gains an `ok` entry, and `make status` shows the new sha.
- [ ] **Broken commit:** a commit on `ops-acceptance/broken` changes `config/demos.yaml` so the smoke `search` check fails. A real code break would also do. Deploying it with `make deploy` makes the smoke test fail, triggers an automatic rollback and exits 1. `deploys.jsonl` then shows a `failed` deploy and an `ok` rollback to the previous sha, `make health` passes, and `git -C /opt/research-engine status` shows the main working tree unchanged.
- [ ] **Backup and restore:** `make backup` creates `backups/research-engine-<ts>.sqlite`, and `make restore FILE=<it>` gives `{"ok":true}` followed by a passing `make health`.
- [ ] **JSON summaries:** every ops command ran in this task ended with a JSON line that `python3 -c 'import json,sys; json.loads(sys.stdin.read().splitlines()[-1])'` can parse. The failing commands exited non-zero.
- [ ] **Fresh-VM criterion:** "From a fresh Debian VM, following the README plus `make bootstrap` and `make deploy` gives a working stack at `https://research.toolbox.home.arpa`" is partly verified on `toolbox`. A truly fresh VM is the owner's call: either they rebuild from the template and follow `docs/deploy-toolbox.md`, or they accept the `toolbox` run as evidence. Record which.

**Verify:** `tail -n 3 deploys.jsonl | python3 -c 'import sys,json;[print(json.loads(l)["action"], json.loads(l)["result"]) for l in sys.stdin]'` → shows `deploy failed` and then `rollback ok`.

**Steps:**

- [ ] **Step 1:** Ask the owner to run `make bootstrap`, then run it a second time. Capture both outputs.
- [ ] **Step 2:** With approval, run `make deploy`. Capture the output.
- [ ] **Step 3:** Create the broken branch and commit. With approval, `make deploy`. Capture the rollback evidence. Then `git switch v1 && git branch -D ops-acceptance/broken`, and remove its worktree via the prune logic.
- [ ] **Step 4:** Run `make backup`. With approval, run `make restore FILE=...`, then `make health`. Capture the output.
- [ ] **Step 5:** Put all the evidence in the step report.

---

### Task 9.7: Finish V1: final review, PR and tag

**Goal:** Run the final whole-branch review, then open the PR from `v1` to `main` and, after the owner merges it, tag `v1.0.0`. Use `superpowers-extended-cc:finishing-a-development-branch`.

> **USER-ORDERED GATE: NON-SKIPPABLE.** Opening the PR, merging and pushing the tag are outward-facing actions on the public repo. Each needs the owner's go-ahead.

**Files:** none (git and GitHub actions only).

**Acceptance Criteria:**
- [ ] On the `v1` HEAD:
  - `uv run ruff check && uv run ruff format --check && uv run pyright && uv run pytest` are all green;
  - `uv run pytest -m integration` against the live stack is green;
  - `uv run research-engine schemas export --check` is green;
  - `uv tool run pre-commit run --all-files` is green.
- [ ] **V1 acceptance checklist** (REQUIREMENTS §5): each of the 7 criteria is ticked, with a link to its evidence. The evidence is the test names, step reports or owner confirmations:
  1. at least 10 results from at least 3 engines: `tests/integration/test_search_live.py`;
  2. static, SPA and PDF fetches: `test_fetch_live.py`;
  3. schema validation: `tests/client/test_schemas.py` and `test_openapi.py`;
  4. events in the GUI within 1 s: `test_gui_dashboard.py::test_live_event_within_one_second`;
  5. `docker compose up` with no keys: step 8 bring-up, which used no paid services;
  6. OpenAI Agents SDK: `test_mcp.py`, plus the owner's manual run;
  7. non-developer console run: the owner's confirmation in step 6.
- [ ] **Final review:** run `superpowers-extended-cc:requesting-code-review` over the whole `main...v1` diff. Fix or explicitly decline each finding with a reason.
- [ ] **PR:** `gh pr create --base main --head v1` with a description listing the feature IDs, the acceptance checklist and the evidence, ending with the session attribution. CI must be green on the PR.
- [ ] **Merge and tag:** the owner merges. Then `git tag -a v1.0.0 -m "Research Engine V1"` and `git push origin v1.0.0`, after the owner approves.

**Verify:** `gh pr view --json state,mergeStateStatus,statusCheckRollup` → `MERGED`; `git ls-remote --tags origin v1.0.0` → one line

**Steps:**

- [ ] **Step 1:** Run every verification command and capture its output (superpowers-extended-cc:verification-before-completion).
- [ ] **Step 2:** Run the final code review. The review covers a large diff and may take a while; tell the owner that when dispatching it.
- [ ] **Step 3:** Ask the owner to approve the PR. Create it.
- [ ] **Step 4:** After the owner merges, and with their approval, push the tag.

---

**End of step 9 / V1.**
