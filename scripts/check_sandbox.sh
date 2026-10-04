#!/usr/bin/env bash
# Verifies Crawl4AI renders with Chromium's sandbox ON. Prints one JSON line; exit 0 only if all hold:
#   - no Chromium process carries --no-sandbox while a crawl is in flight;
#   - positive evidence from every renderer: Seccomp: 2, more seccomp filters than the browser
#     process (Docker's default filter puts Seccomp: 2 on *every* process, so the extra filter is
#     Chromium's own seccomp-bpf), and a user namespace different from the browser's;
#   - the crawl of https://example.com succeeds with non-empty markdown.
set -euo pipefail
cd "$(dirname "$0")/.."
source <(scripts/dev_urls.sh)
TOKEN=$(grep -E '^CRAWL4AI_API_TOKEN=' .env | cut -d= -f2-)
dc() { docker compose -f compose.yaml -f compose.dev.yaml "$@"; }
out=$(mktemp)
trap 'rm -f "$out"' EXIT
# Prints "<no_sandbox_procs> <renderers> <renderers_sandboxed>" for Chromium processes in the container.
# Matches on argv0 only, so this probe shell (whose own cmdline contains these strings) is never counted.
# shellcheck disable=SC2016
probe='
ns=0; r=0; ok=0; bf=""; bu=""
for d in /proc/[0-9]*; do
  c=$(tr "\0" " " 2>/dev/null < "$d/cmdline") || continue
  case "${c%% *}" in *chrom*) ;; *) continue;; esac
  case "$c" in *--no-sandbox*) ns=$((ns+1));; esac
  case "$c" in *--type=*) ;; *) bf=$(awk "/^Seccomp_filters:/{print \$2}" "$d/status" 2>/dev/null); bu=$(readlink "$d/ns/user" 2>/dev/null);; esac
done
for d in /proc/[0-9]*; do
  c=$(tr "\0" " " 2>/dev/null < "$d/cmdline") || continue
  case "${c%% *}" in *chrom*) ;; *) continue;; esac
  case "$c" in *--type=renderer*) ;; *) continue;; esac
  r=$((r+1))
  s=$(awk "/^Seccomp:/{print \$2}" "$d/status" 2>/dev/null); f=$(awk "/^Seccomp_filters:/{print \$2}" "$d/status" 2>/dev/null)
  u=$(readlink "$d/ns/user" 2>/dev/null)
  if [ "$s" = 2 ] && [ -n "$bf" ] && [ "${f:-0}" -gt "$bf" ] && [ -n "$u" ] && [ "$u" != "$bu" ]; then ok=$((ok+1)); fi
done
echo "$ns $r $ok"'
# start a crawl in the background so Chromium is running while we inspect processes
curl -s -m 90 -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d '{"urls":["https://example.com"],"crawler_config":{"type":"CrawlerRunConfig","params":{"cache_mode":"bypass"}}}' \
  "$CRAWL4AI_LIVE_URL/crawl" > "$out" &
pid=$!
procs=0; renderers=0; sandboxed=0
for _ in $(seq 1 30); do
  read -r procs renderers sandboxed < <(dc exec -T crawl4ai sh -c "$probe" 2>/dev/null) ||
    { procs=0; renderers=0; sandboxed=0; }
  if [ "$renderers" -gt 0 ]; then break; fi
  sleep 1
done
wait "$pid" || true
ok=$(python3 -c 'import json,sys
try:
    r=json.load(open(sys.argv[1]))["results"][0]; print(str(bool(r["success"] and r["markdown"])).lower())
except Exception:
    print("false")' "$out")
seccomp=$([ "$renderers" -gt 0 ] && [ "$sandboxed" -eq "$renderers" ] && echo true || echo false)
sandbox=$([ "$procs" -eq 0 ] && [ "$seccomp" = true ] && echo on || echo off)
echo "{\"sandbox\":\"$sandbox\",\"no_sandbox_procs\":$procs,\"renderers\":$renderers,\"renderer_seccomp_userns\":$seccomp,\"crawl_ok\":$ok}"
[ "$sandbox" = on ] && [ "$ok" = true ]
