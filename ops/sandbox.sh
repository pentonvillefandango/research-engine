#!/usr/bin/env bash
# ADR-0022 check on the LIVE stack: Crawl4AI renders with Chromium's sandbox ON on both launch
# paths. Read-only apart from two crawls of https://example.com made inside the crawl4ai container
# (no published ports; the token stays in that container). Run after every deploy, because every
# deploy recreates crawl4ai. One implementation: scripts/check_sandbox.sh in prod mode, which
# follows the ops contract itself (one final JSON line; exit 0 ok, 1 failed, 2 usage).
CMD=sandbox
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
log "sandbox: checking Chromium's sandbox in crawl4ai (two crawls, ~10-30 s)"
exec "$REPO_DIR/scripts/check_sandbox.sh" prod
