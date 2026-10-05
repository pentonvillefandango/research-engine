#!/usr/bin/env bash
# Recent logs: ops/logs.sh [SERVICE] [SINCE]. TAIL (env, default 500) caps lines. Read-only.
CMD=logs
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

service="${1:-app}"
since="${2:-30m}"
tail_n="${TAIL:-500}"
[[ "$since" =~ ^[0-9]+[smhd]$ ]] || fail 2 logs "invalid SINCE (use e.g. 30m, 2h)" service="$service"
[[ "$tail_n" =~ ^[0-9]+$ ]] || fail 2 logs "invalid TAIL (use a number)" service="$service"
services="$(dc config --services 2>/dev/null)" || fail 1 logs "cannot list compose services" service="$service"
found=0
while IFS= read -r s; do [ "$s" = "$service" ] && found=1; done <<<"$services"
[ "$found" -eq 1 ] || fail 2 logs "unknown service" service="$service"

tmp="$(mktemp)"
raw="$(mktemp)"
trap 'c=$?; rm -f "$tmp" "$raw"; (exit "$c"); _on_exit' EXIT
if ! dc logs --no-color --since "$since" --tail "$tail_n" "$service" >"$raw" 2>/dev/null; then
  fail 1 logs "docker compose logs failed" service="$service"
fi
sed -E 's/\x1b\[[0-9;?]*[A-Za-z]//g' "$raw" | redact >"$tmp"
cat "$tmp"
json_out ok:=true command=logs service="$service" lines:="$(wc -l <"$tmp" | tr -d ' ')"
