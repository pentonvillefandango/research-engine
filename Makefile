.PHONY: help bootstrap deploy rollback status health smoke logs backup restore version test
SERVICE ?= app
SINCE ?= 30m

help:            ## List ops commands
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:[ ]*## /\t/'
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
