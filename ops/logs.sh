#!/usr/bin/env bash
# Recent logs: ops/logs.sh [SERVICE] [SINCE]. Read-only.
CMD=logs
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

service="${1:-app}"
since="${2:-30m}"
[[ "$since" =~ ^[0-9]+[smhd]$ ]] || fail 2 logs "invalid SINCE (use e.g. 30m, 2h): $since" service="$service"
dc config --services 2>/dev/null | grep -qx -- "$service" || fail 2 logs "unknown service: $service" service="$service"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
dc logs --no-color --since "$since" "$service" 2>&1 | redact >"$tmp" || true
cat "$tmp"
json_out ok=true command=logs service="$service" lines="$(wc -l <"$tmp" | tr -d ' ')"
