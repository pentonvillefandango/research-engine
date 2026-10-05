# Operations runbook

How to run, check, deploy, back up and repair a Research Engine host. Every command here works the same for the owner and for Claude Code. The example deployment is in [deploy-toolbox.md](deploy-toolbox.md).

## Conventions

- Every operation is `make <target>`, a thin wrapper around `ops/<target>.sh`. Run them from the repository root (for example `/opt/research-engine`).
- Progress goes to stderr. The **last line on stdout is one JSON object**, always with `ok` (true or false) and `command`. A failure adds `error`.
- The scripts exit **0** on success, **1** when the check or command failed, and **2** on a usage or precondition error (bad argument, dirty tree, or the shared lock is held: deploy, rollback and restore give up at once, while a backup waits up to `BACKUP_LOCK_WAIT` first). `make` reports a failing script as `make: *** [Makefile:NN: <target>] Error 1` (or `Error 2`) on stderr, and then itself exits 2. Read the JSON `ok`, or run `ops/<target>.sh` directly when you need the exact code.
- No script prints a secret. Logs and command output pass through a redactor that masks key, token, secret and password values.
- The scripts address the stack only as Compose project `research-engine`, with `--env-file .env`. They never touch other projects or VM-wide Docker state.
- Useful environment overrides: `BACKUP_DIR` (default `backups/`), `BACKUP_RETENTION_DAYS` (14), `BACKUP_LOCK_WAIT` (600 seconds), `CADDY_DIR` (`/opt/caddy`), `ENV_FILE` (`.env`), `DEPLOYS_LOG` (`deploys.jsonl`), `TAIL` (500 lines, for logs).

## Commands

| Command | What it does | Final JSON line |
| --- | --- | --- |
| `make help` | Lists the targets. | (plain text) |
| `make status` | Containers, health, restart counts, CPU and memory, this project's volume sizes, running version, last deploy and last backup. Read-only. Fails if an expected service isn't running. | `{ok, command, containers: [{name, state, health, restarts, cpu, memory}], not_running, volumes: [{name, size}], version, last_deploy, last_backup}` |
| `make health` | The app's `/health`, every container's healthcheck, whether `SITE_HOST` in `.env` equals `SITE_HOST` in `$CADDY_DIR/.env` (compared, never printed), and whether any of `API_KEY`, `SESSION_SECRET`, `CRAWL4AI_API_TOKEN` and `SEARXNG_SECRET` in `.env` is missing, the `.env.example` placeholder or shorter than 32 characters (names only, never values). Read-only. Fails unless the app is `up`, every service is running and healthy, the hosts match and no secret is weak. | `{ok, command, app_status, containers: {name: health}, problems, site_host_match, site_host_reason, weak_secrets}` |
| `make smoke` | The demo set against the live API, from inside the app container, in under 2 minutes: `version`, `health`, `mcp`, `search`, `fetch_static`, `fetch_pdf`, `search_read`. Read-only apart from those requests. | `{ok, command, checks: [{name, ok, ms, detail}], took_ms}` |
| `make sandbox` | Checks that Chromium in the live `crawl4ai` runs with its sandbox on, on both launch paths (ADR-0022). Two crawls of `https://example.com` from inside the container; takes 10–30 s. | `{ok, command, mode, sandbox, no_sandbox_procs, default_ok, builtin_ok, addon_active, addon_warnings, crawl_ok, ...}` |
| `make logs SERVICE=app SINCE=30m` | Recent logs for one service (`app`, `searxng` or `crawl4ai`), redacted. `SINCE` is a number plus `s`, `m`, `h` or `d`. | log lines, then `{ok, command, service, lines}` |
| `make version` | Running version, git SHA, schema version and image tags. Read-only. | `{ok, command, version, git_sha, schema_version, images: [...]}` |
| `make backup` | Online SQLite backup into `backups/`, then retention. The app keeps running. | `{ok, command, file, bytes, pruned}` |
| `make restore FILE=backups/research-engine-<ts>.sqlite` | Checks the file, takes a safety backup, stops `app`, restores the file, starts `app`, runs `health`. | `{ok, command, restored, health, safety_backup}` |
| `make deploy` | Deploys the committed `HEAD`: worktree, build, `up --wait`, smoke, sandbox; rolls back automatically on failure. | ok: `{ok, command, from, to, smoke, sandbox}`; failed: adds `error`, `detail`, `stack_changed`, `rolled_back_to`, `rollback_target`, `rollback_ok`, `rollback_branch`, `current` |
| `make rollback` or `make rollback SHA=<sha>` | Redeploys the last good commit (or the given one) on a new `rollback/<ts>` branch, then smoke and sandbox. | ok: `{ok, command, from, to, branch, smoke, sandbox}`; failed: adds `error`, `switched`, `current` and `detail` |
| `make bootstrap` | One-off, idempotent VM set-up (see below). Uses `sudo`, so the owner runs it. | `{ok, command, dry_run, actions: [{step, action, ...}]}` |
| `make test` | Lint, format check, type check and unit tests. Touches nothing live. | (tool output) |

## Deploy and rollback

The live stack always runs a **committed** version. Changes go live only by committing, then `make deploy`.

### Layout

```text
/opt/research-engine/            main working tree (never reset or checked out by ops)
├── .deploy/
│   ├── <short-sha>/             one git worktree per deployed commit (12-character SHA)
│   ├── current -> <short-sha>   the live one; the ops scripts run Compose from here
│   └── lock                     one deploy, rollback, restore or backup at a time
├── deploys.jsonl                append-only history (git-ignored)
├── backups/                     mode 700, files mode 600 (git-ignored)
└── .env                         mode 600 (git-ignored)
```

### What `make deploy` does

1. Refuses (exit 2) if the working tree has uncommitted changes, if any of `API_KEY`, `SESSION_SECRET`, `CRAWL4AI_API_TOKEN` and `SEARXNG_SECRET` in `.env` is missing, the `.env.example` placeholder or shorter than 32 characters (`weak_secrets` names them, never their values), or if another deploy holds the lock.
2. Creates (or reuses, after checking it) the worktree `.deploy/<short-sha>` at `HEAD` and points `current` at it.
3. Runs `docker compose up -d --build --wait` from the worktree, with `GIT_SHA` set, so the app image is `research-engine-app:<short-sha>`.
4. Runs `make smoke`, then `make sandbox` (every deploy recreates `crawl4ai`, so the sandbox is checked every time).
5. On success: appends an `ok` entry to `deploys.jsonl`, keeps the current worktree plus the 3 most recent others, and removes older `research-engine-app:<sha>` images by explicit tag. It never prunes.
6. On failure: appends a `failed` entry, then rolls back to the last good commit. If there is none, it leaves the failed stack up for diagnosis.

`make rollback` picks the newest `ok` entry whose commit differs from the current one, or `SHA=<sha>`. It creates the branch `rollback/<UTC-timestamp>` at that commit (so no work is lost), a worktree, repoints `current`, brings the stack up and runs smoke and sandbox. It also appends an entry.

### `deploys.jsonl`

One JSON object per line, for example:

```json
{"action": "deploy", "from": "1a2b3c4d5e6f", "result": "ok", "sandbox": "pass", "smoke": "pass", "to": "6f5e4d3c2b1a", "ts": "2026-10-05T18:00:00Z"}
```

Fields: `ts` (UTC), `action` (`deploy` or `rollback`), `from`, `to`, `result` (`ok` or `failed`), `smoke` and `sandbox` (`pass`, `fail` or `skipped`), and when relevant `detail` (the failing step's result), `branch` (rollbacks) and `from_source: "running"` (see below). The newest `ok` entry is the rollback target. Read it with `tail -n 5 deploys.jsonl`.

### Recovery cases

- **A deploy was interrupted during `up`** (killed, VM rebooted). `current` already points at the new commit, but `deploys.jsonl` has no entry for it. Spot it: `readlink .deploy/current` differs from the last entry's `to`, and `make version` may show either commit. Recover with `make rollback SHA=<last ok to>`, or, if `HEAD` is still the commit you want, run `make deploy` again (it reuses the checked worktree). The lock is released when a process ends, so it is never left stale.
- **A worktree is registered but its directory is missing.** `git worktree list` marks it `prunable`, and a deploy of that commit fails. Run `git worktree prune`. It removes only git's records for directories that no longer exist.
- **Stray `rollback/<ts>` branches.** Every rollback leaves one. List them with `git branch --list 'rollback/*'`. Delete one you no longer need with `git branch -d rollback/<ts>` (git refuses while a worktree still has it checked out). Use `-D` only after `git branch --contains <sha>` shows the commit is on another branch.
- **The first worktree deploy.** It recreates every container once, because Compose now runs from `.deploy/<sha>` instead of the main tree. The version that was running before is recorded as `from` with `from_source: "running"`. It has no worktree, so plain `make rollback` never picks it. Reach it only with `make rollback SHA=<that sha>`; a failed first deploy prints it as `suggested_sha`.

## Backup and restore

- `make backup` runs `research-engine db backup` inside `app`. It uses SQLite's online backup API, so it is safe while the app is writing, and it checks the copy with `PRAGMA integrity_check`. The file lands in `backups/research-engine-<UTC-timestamp>.sqlite` (mode 600; the directory is mode 700). Then retention deletes `research-engine-*.sqlite` files in `backups/` older than `BACKUP_RETENTION_DAYS` (default 14), and nothing else.
- A **nightly backup** runs at 03:30 from the systemd timer `research-engine-backup.timer`, installed by `make bootstrap` (`Persistent=true`, so a run missed while the VM was off happens at the next boot). Check it with `systemctl list-timers research-engine-backup.timer`. `make status` shows `last_backup`, the newest file in `backups/`; right after a restore that is the `-prerestore` safety copy.
- Backups take the same lock as deploy, rollback and restore, so they never overlap. A backup waits for the lock for up to `BACKUP_LOCK_WAIT` seconds (default 600).
- `make restore FILE=backups/research-engine-<ts>.sqlite` accepts only a `research-engine-*.sqlite` file directly inside `backups/` (exit 2 otherwise). It takes the lock, then, while the app still runs, checks the file (`integrity_check` and the tables `job`, `event` and `cache_entry`) and takes a safety backup of the current database (`research-engine-<ts>-prerestore.sqlite`, restorable like any other). If either step fails, nothing changes. Only then does it stop `app`, copy the backup into place (removing stale `-wal` and `-shm` files), start `app` and run `health`. The final JSON line names the safety copy in `safety_backup`. The safety backup is taken while the app still runs, so anything written between it and the app stopping (a few seconds) is in neither copy. Restore refuses (exit 2) a file with a non-empty `-wal` file beside it: restore files made by `make backup`.
- **Back up Caddy's root CA too.** Caddy's private CA lives in the `caddy-data` volume of the shared Caddy (`/data/caddy/pki/authorities/local/`). If it is lost, Caddy makes a new CA and every client must trust the new root. Copy it off the VM once, keep it private (it holds the root key), and don't leave a copy on the VM:

  ```bash
  cd /opt/caddy && docker compose cp caddy:/data/caddy/pki/authorities/local ./ca-backup
  # from your own machine: scp -r <vm>:/opt/caddy/ca-backup ./caddy-ca-backup
  rm -rf /opt/caddy/ca-backup
  ```

## Bootstrap

`make bootstrap` (or `ops/bootstrap.sh --dry-run`, which prints the planned actions and changes nothing) is safe to re-run. It:

1. checks for `docker`, `docker compose`, `make` and `systemctl`, and that the current user owns the repository (exit 2 otherwise);
2. creates `.env` from `.env.example` with four generated secrets (`API_KEY`, `SESSION_SECRET`, `SEARXNG_SECRET`, `CRAWL4AI_API_TOKEN`), mode 600, if it is missing. It never overwrites `.env` and never prints a value;
3. creates `backups/` (mode 700);
4. copies changed files from `deploy/caddy/` to `/opt/caddy/` (never deleting anything there, never copying a `.env`). It writes `SITE_HOST` in `/opt/caddy/.env` from the repository's `.env`, the single source. The `.env.example` placeholder (`research.localhost`, in a new or never-edited `.env`) never replaces a different existing Caddy value; the JSON then carries a `site_host_note`. So set `SITE_HOST` in `.env` before the first run. When `/opt/caddy/.env` is missing, or its `LAB_SUBNET` is empty, it needs `LAB_SUBNET` (and optionally `TOOLBOX_HOST`) in the environment, and exits 2 saying so otherwise;
5. starts Caddy, which creates the `proxy` network. If the `Caddyfile`, `compose.yaml` or `.env` changed it recreates the container; if only `sites/` changed it reloads Caddy;
6. installs and enables the nightly backup timer with `sudo` (its only sudo use, which may ask for the password; skipped when the timer is already installed and running).

## Host-side checks

From the VM itself, requests through `127.0.0.1` get **403 by design**: Caddy sees the Docker bridge address, which is outside `LAB_SUBNET`. Use the VM's lab IP with `--resolve` instead:

```bash
curl --cacert root.crt --resolve "<SITE_HOST>:443:<vm-lab-ip>" "https://<SITE_HOST>/health"
```

`make health` and `make smoke` need neither: they run inside the containers.

## Dev stack on the live host (non-destructive)

Integration tests and fixture recording use a second stack in its own Compose project, `research-engine-dev`, so it runs beside the live stack and never replaces it:

1. Check memory first with `free -m`. The dev stack adds a second SearXNG and Crawl4AI (about 1.6 GB after a few crawls). Don't start it with less than about 6 GB available.
2. `docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml up -d --wait searxng crawl4ai`
3. `source <(scripts/dev_urls.sh)`, then `uv run pytest -m integration`.
4. `docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml down` (never `-v` on the live host without the owner; the `research-engine-dev_*` volumes are throwaway and may be left).

Always pass `-p research-engine-dev` with `compose.dev.yaml`. Without it, the override would apply to project `research-engine` and replace the live stack.

## Incident checklist

Start with `make status`, `make health` and `tail -n 5 deploys.jsonl`.

**Health is degraded or down.**
- `app_status` is `degraded` when SearXNG or Crawl4AI is down, and `down` (HTTP 503) when the database is down. `problems` names the unhealthy services.
- `make logs SERVICE=<service> SINCE=1h` for the failing one.
- Restart only that container by the name `make status` shows, for example `docker restart research-engine-crawl4ai-1`. After restarting `crawl4ai`, run `make sandbox`.
- `site_host_match: false` means `SITE_HOST` differs between `.env` and `/opt/caddy/.env`, so `/mcp` returns 421 through Caddy. Fix the Caddy copy (or re-run `make bootstrap`), then `docker compose up -d --force-recreate` in `/opt/caddy`.
- `weak_secrets` names secrets in `.env` that are missing, the `.env.example` placeholder or shorter than 32 characters. The app refuses to start with such an `API_KEY`, `SESSION_SECRET` or `CRAWL4AI_API_TOKEN`, and `make deploy` refuses (exit 2, nothing changed) while any of the four is weak. Replace each with `openssl rand -hex 32`, then `make deploy`. Rotating `SESSION_SECRET` logs every GUI session out.
- If it still fails, `make rollback`.

**A container keeps restarting.**
- `make status` shows `restarts`; `make logs SERVICE=<service> SINCE=2h` shows why.
- Out of memory: raise `<SERVICE>_MEM_LIMIT` in `.env` and run `make deploy`.
- Started after a deploy: `make rollback`.

**The disk is filling.**
- `df -h /`, `make status` (this project's volume sizes) and `du -sh backups .deploy`.
- Docker logs rotate (10 MB × 5 per container). Events older than `EVENT_RETENTION_DAYS` are deleted; cache entries expire.
- Lower `BACKUP_RETENTION_DAYS`, or ask the owner to delete old backups.
- Deploys keep 3 old worktrees and images. Old `research-engine-dev_*` volumes can go, but only the owner deletes volumes.
- Never run `docker system prune` or any VM-wide clean-up: other tools share the host.

**A deploy went bad.**
- A failed smoke or sandbox check rolls back by itself; the JSON line and `deploys.jsonl` show `detail`.
- If a deploy passed but the service misbehaves, `make rollback` (or `make rollback SHA=<sha>`).
- Fix forward with a new commit and `make deploy`. See [Recovery cases](#recovery-cases) for interrupted deploys.

**SearXNG engines are blocked.**
- Search responses list them in `unresponsive_engines`, and the GUI shows `search.engine_failed` events. `make logs SERVICE=searxng SINCE=1h` shows CAPTCHAs or rate limits.
- One failed engine never fails a search. Blocks are often temporary; wait an hour first.
- For a lasting block, change that intent's engines in `config/intents.yaml` (or the engine list in `deploy/searxng/settings.yml`), commit and `make deploy`.

## Guardrails (REQUIREMENTS §10)

- **Run freely (read-only):** `make status`, `make health`, `make logs`, `make version`, `make smoke`, `make sandbox`, `make backup` and `make test`.
- **Restart or restore the live service:** `make deploy`, `make rollback` and `make restore`. §10 requires the owner's approval each time. The owner has given Claude Code a standing approval for these three (2026-10-05); report every run. `make bootstrap` uses `sudo`, so the owner runs it.
- **Never, unless the owner explicitly asks:** `docker compose down -v`, deleting volumes or backups, or force-pushing. `.claude/settings.json` denies these.
- Secrets live only in `.env` (mode 600, git-ignored). Never print or commit them.
- Never touch other tools' directories under `/opt/`, other Compose projects or VM-wide Docker state, apart from this tool's Caddy site file and Caddy reloads.
- Take a Proxmox snapshot before VM-level changes (OS or Docker upgrades). A snapshot covers every tool on the VM.
