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
- **After each step:** `requesting-code-review`, then report to the owner with evidence, and continue to the next step. Don't pause for confirmation (owner's standing approval, 2026-10-05).
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
- `make test` runs `uv run ruff check`, `uv run ruff format --check`, `uv run pyright` and `uv run pytest`.
- When diagnosing test crashes, run with `PYTHONFAULTHANDLER=1` and keep the full output (redirect to a file); never pipe it through `| tail`, which discards the fatal-error dump.
- `uv run research-engine schemas export [--check]` regenerates `schemas/`, and CI checks that it's current.
- **Integration tests** run against the dev stack, which has its own Compose project `research-engine-dev`, no Caddy and ephemeral loopback ports:
  1. `docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml up -d --wait searxng crawl4ai`
  2. `source <(scripts/dev_urls.sh)`, which exports `SEARXNG_LIVE_URL` and `CRAWL4AI_LIVE_URL`
  3. `uv run pytest -m integration` (includes `scripts/check_sandbox.sh dev`)
  4. `docker compose -p research-engine-dev -f compose.yaml -f compose.dev.yaml down` (never `-v`; its volumes `research-engine-dev_*` are throwaway and may be left)
- **Live-host note (`toolbox`):** always pass `-p research-engine-dev` with `compose.dev.yaml`. Without it the dev override would use the `research-engine` project and REPLACE the live stack. With it the dev stack runs alongside production and never touches it. It costs RAM: a second SearXNG and Crawl4AI (about 1.6 GB together after a few crawls; Crawl4AI is limited to 4 GB). Check `free -m` first and don't start it with less than ~6 GB available.
- Never run `docker compose up` yourself against the live stack (project `research-engine`): use the make targets. They run Compose from `.deploy/current` with the right `GIT_SHA`, so the app never starts from a stale or `:dev` image.
- **Fixtures** come from real upstreams via `scripts/record_fixtures.py`. Scrub them for lab data before committing.
- `tests/test_docs.py` keeps the docs honest: `docs/USING.md` (the agent usage guide, at most 1,800 words) must name every MCP tool and `/v1` route, `docs/OPERATIONS.md` every make target, and the README's configuration table every `.env.example` key. Update the docs in the same commit as the code.

Operations (see `docs/OPERATIONS.md`, the runbook). Each command prints a final JSON line (`ok`, `command`, …) and the script exits 0 ok, 1 failed, 2 usage or precondition error (`make` itself then exits 2):
- **Read-only, run freely:** `make status`, `make health`, `make logs SERVICE=app SINCE=30m`, `make version`, `make smoke`, `make sandbox`, `make backup`, `make test`, `make help`.
- **Restart or restore the live service:** `make deploy`, `make rollback [SHA=…]`, `make restore FILE=…` are pre-approved by the owner (2026-10-05). Report what you ran. `make bootstrap` uses `sudo`, so the owner runs it (sudo's own prompt gates it; `ops/bootstrap.sh --dry-run` is safe to run).
- **Never, unless the owner explicitly asks:** `docker compose down -v`, deleting volumes or backups, or force-pushing. `.claude/settings.json` denies force-push (`git push --force`, `-f`, `+ref`), `docker compose down -v`/`--volumes`, `docker volume rm`/`remove`/`prune` and `docker system prune`.
- Changes go live only by committing and then running `make deploy`.
- Host-side checks through Caddy must use the VM's lab IP with `curl --resolve <SITE_HOST>:443:<lab-ip>`: `127.0.0.1` gets 403 by design. `make health` and `make smoke` run inside the containers and need neither.

## Git

- Step 0 goes on `main`. V1 steps 1–9 go on branch `v1`. Push after each step's review passes. One PR `v1`→`main` at the end, merged once CI is green, then tag `v1.0.0` (ADR-0024). The owner pre-approved the push, PR, merge and tag (2026-10-05).
- Use conventional commit prefixes with feature IDs.
