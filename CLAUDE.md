# CLAUDE.md: Research Engine

A self-hosted web search and scraping service for research agents. It offers REST and MCP, typed Pydantic contracts, and a live GUI. The repo is **public**.

## Working method: Superpowers

**This project's working method is the Superpowers (extended) plugin** (`superpowers-extended-cc:*` skills). Follow its workflow; don't improvise your own.

- **Before building anything:**
  - New feature or behaviour change: `brainstorming` comes first. Ask the owner; don't assume.
  - Multi-step work: `writing-plans` produces a plan in `docs/plans/` before any code.
- **While building:**
  - Run plans with `subagent-driven-development` (or `executing-plans`).
  - Use `dispatching-parallel-agents` only for steps that touch disjoint files.
  - **TDD always** (`test-driven-development`): write a failing test, watch it fail, then write the minimal code to pass.
  - Bugs and unexpected behaviour: `systematic-debugging`. No guess-and-patch.
- **Before saying something is done:** `verification-before-completion`. Run the commands and show the output before claiming anything is done.
- **After each step:** `requesting-code-review`, then report to the owner with evidence, and **pause for confirmation** before the next step.
- **End of a branch:** `finishing-a-development-branch`.

The spec is `REQUIREMENTS.md` plus the design addendum `docs/superpowers/specs/2026-10-04-v1-design.md`, which wins where the two differ. The V1 plan is `docs/plans/2026-10-04-research-engine-v1.md`, with one file per step in `docs/plans/v1/`. Decisions are in `docs/adr/`. Record any new notable decision as an ADR.

Feature IDs (V1-01 and so on) are the ticket references. Put them in every commit message, e.g. `feat(search): SearXNG adapter (V1-01, V1-03)`. There's no Jira.

## Hard rules

- **No model or LLM calls in V1** (ADR-0005).
- **Never commit secrets or lab-specific values.** No IPs, real hostnames, usernames, keys or tokens. Everything comes from `.env`, which is git-ignored; only `.env.example` with placeholders is committed. gitleaks runs in pre-commit and CI. Never print the contents of `.env`.
- **Never configure Chromium with `--no-sandbox`** (ADR-0022).
- **Keep every external tool behind its adapter protocol** (`SearchProvider`, `Fetcher`, extractors). Only `app.py` wires concrete classes.
- **Every response is `Envelope[T]`** from `research_engine_client.models`. Optional fields are explicitly nullable, and enums are used over free strings.
- **Async throughout.** Blocking libraries run via `asyncio.to_thread`. Our HTTP code uses `httpx` (not httpx2; ADR-0023).
- **Treat all fetched content as untrusted.** Render it only through `render_untrusted_markdown`.

## Co-tenancy on `toolbox` (REQUIREMENTS §9)

This VM is the live host, and other tools will share it.
- Never publish fixed host ports, set `container_name`, run `docker system prune` or other VM-wide clean-ups, or write outside `/opt/research-engine/` and this project's volumes.
- Address the stack only as Compose project `research-engine`.

## Commands

Development:
- `uv sync`. Run tests with `uv run pytest`; integration tests are deselected by default.
- `uv run ruff check && uv run ruff format --check && uv run pyright`. Or use `make test`.
- When diagnosing test crashes, run with `PYTHONFAULTHANDLER=1` and keep the full output (redirect to a file); never pipe it through `| tail`, which discards the fatal-error dump.
- `uv run research-engine schemas export [--check]` regenerates `schemas/`, and CI checks that it's current.
- **Integration tests** run against the dev stack, which has no Caddy and uses ephemeral loopback ports:
  1. `docker compose -f compose.yaml -f compose.dev.yaml up -d --wait`
  2. `source <(scripts/dev_urls.sh)`, which exports `SEARXNG_LIVE_URL` and `CRAWL4AI_LIVE_URL`
  3. `uv run pytest -m integration`
- **Live-host warning (`toolbox`):** the dev override shares the `research-engine` project name with production, so `docker compose -f compose.yaml -f compose.dev.yaml up` REPLACES the live stack (app from a stale `:dev` image, off the `proxy` network, so Caddy returns 502). There, running the dev stack or integration tests needs owner approval like a deploy and must be followed by a redeploy, until step 9 provides a non-destructive way.
- Never run bare `docker compose up` on the live host without `GIT_SHA=$(git rev-parse --short HEAD)` exported (else the stale `:dev` image may be used). Step 9's ops scripts will wrap this.
- **Fixtures** come from real upstreams via `scripts/record_fixtures.py`. Scrub them for lab data before committing.

Operations (added in build step 9; see `docs/OPERATIONS.md`). Each command prints a final JSON line and exits non-zero on failure:
- **Read-only, run freely:** `make status`, `make health`, `make logs SERVICE=app SINCE=30m`, `make version`, `make smoke`, `make backup`.
- **Restart or restore the live service: ask the owner every time:** `make deploy`, `make rollback`, `make restore FILE=…`, `make bootstrap`. The project's `.claude/settings.json` makes these prompt.
- **Never, unless the owner explicitly asks:** `docker compose down -v`, deleting volumes or backups, or force-pushing. These are also denied in `.claude/settings.json`.
- Changes go live only by committing and then running `make deploy`.

## Git

- Step 0 goes on `main`. V1 steps 1–9 go on branch `v1`. Push only after the owner approves a step. One PR `v1`→`main` at the end, then tag `v1.0.0` (ADR-0024).
- Use conventional commit prefixes with feature IDs.
