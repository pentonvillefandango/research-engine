#!/usr/bin/env bash
# Print export lines for the dev stack's ephemeral URLs. Usage: source <(scripts/dev_urls.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
dc() { docker compose -f compose.yaml -f compose.dev.yaml "$@"; }
echo "export SEARXNG_LIVE_URL=http://$(dc port searxng 8080)"
echo "export CRAWL4AI_LIVE_URL=http://$(dc port crawl4ai 11235)"
