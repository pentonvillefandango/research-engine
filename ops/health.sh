#!/usr/bin/env bash
# /health plus container healthchecks plus SITE_HOST consistency with Caddy. Read-only.
CMD=health
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd python3

ids="$(dc ps -q)"
[ -n "$ids" ] || fail 1 health "no containers found for project $PROJECT"
# shellcheck disable=SC2086
inspect="$(docker inspect $ids)"
body="$(app_get_or_empty /health)"

# SITE_HOST must match the shared Caddy's, or /mcp returns 421. Compared, never printed.
site_match=null
mine="$(env_get SITE_HOST || true)"
theirs="$(env_get SITE_HOST "$CADDY_DIR/.env" || true)"
if [ -n "$mine" ] && [ -n "$theirs" ]; then
  if [ "$mine" = "$theirs" ]; then site_match=true; else site_match=false; fi
else
  log "site_host_match unknown: SITE_HOST not readable in both env files"
fi

result="$(INSPECT="$inspect" BODY="$body" SITE_MATCH="$site_match" python3 - <<'PY'
import json, os
containers = {
    c["Name"].lstrip("/"): (c["State"].get("Health") or {}).get("Status", "none")
    for c in json.loads(os.environ["INSPECT"])
}
try:
    app_status = json.loads(os.environ["BODY"])["data"]["status"]
except (ValueError, KeyError, TypeError):
    app_status = "unreachable"
site = json.loads(os.environ["SITE_MATCH"])
ok = app_status == "up" and all(v == "healthy" for v in containers.values()) and site is not False
print(json.dumps({"ok": ok, "command": "health", "app_status": app_status,
                  "containers": containers, "site_host_match": site}, sort_keys=True))
PY
)"
printf '%s\n' "$result"
[ "$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
