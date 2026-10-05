#!/usr/bin/env bash
# /health, every compose service running and healthy, SITE_HOST consistent with Caddy, and no
# placeholder/short secrets in .env (names only, never values). Read-only.
CMD=health
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

expected="$(dc config --services)" || fail 1 health "cannot list compose services"
ids="$(dc ps -a -q)"
inspect=""
[ -n "$ids" ] && inspect="$(inspect_fields "$ids")"
body="$(app_get_or_empty /health)"

# SITE_HOST must match the shared Caddy's, or /mcp returns 421. Compared (case-insensitively),
# never printed. No Caddy env file: not a failure (null + reason). Anything else wrong: failure.
site_match=null
site_reason=null
if [ ! -e "$CADDY_DIR/.env" ]; then
  site_reason='"no caddy env"'
else
  mine="$(env_get SITE_HOST || true)"
  if [ ! -r "$CADDY_DIR/.env" ]; then
    site_match=false; site_reason='"unreadable caddy env"'
  elif [ -z "$mine" ]; then
    site_match=false; site_reason='"missing SITE_HOST in env"'
  elif ! theirs="$(env_get SITE_HOST "$CADDY_DIR/.env")" || [ -z "$theirs" ]; then
    site_match=false; site_reason='"missing SITE_HOST in caddy env"'
  elif [ "${mine,,}" = "${theirs,,}" ]; then
    site_match=true
  else
    site_match=false; site_reason='"mismatch"'
  fi
fi

# Placeholder, short or missing secrets in .env (names only; values are never printed).
weak="$(weak_secrets)"

result="$(WEAK="$weak" SECRET_HINT="$SECRET_HINT" INSPECT="$inspect" EXPECTED="$expected" BODY="$body" SITE_MATCH="$site_match" \
  SITE_REASON="$site_reason" python3 - <<'PY'
import json, os
containers, good = {}, set()
for line in os.environ["INSPECT"].splitlines():
    if not line.strip():
        continue
    name, state, health, _restarts, service = line.split("\t")
    containers[name.lstrip("/")] = health if state == "running" else state
    if state == "running" and health == "healthy":
        good.add(service)
problems = [f"{svc}: not running and healthy" for svc in os.environ["EXPECTED"].split() if svc not in good]
weak = os.environ["WEAK"].split()
if weak:
    problems.append(f"weak secrets in .env: {' '.join(weak)} ({os.environ['SECRET_HINT']})")
try:
    app_status = json.loads(os.environ["BODY"])["data"]["status"]
except (ValueError, KeyError, TypeError):
    app_status = "unreachable"
site = json.loads(os.environ["SITE_MATCH"])
ok = app_status == "up" and not problems and site is not False
print(json.dumps({"ok": ok, "command": "health", "app_status": app_status, "containers": containers,
                  "problems": problems, "site_host_match": site, "weak_secrets": weak,
                  "site_host_reason": json.loads(os.environ["SITE_REASON"])}, sort_keys=True))
PY
)"
emit_json_line "$result"
[ "$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
