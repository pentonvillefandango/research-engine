#!/usr/bin/env bash
# Shared helpers for ops scripts. Contract: progress on stderr; final stdout line is one JSON object.
# Exit codes: 0 ok, 1 check/command failed, 2 usage or precondition error.
set -euo pipefail
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
DEPLOYS_LOG="${DEPLOYS_LOG:-$REPO_DIR/deploys.jsonl}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"
CADDY_DIR="${CADDY_DIR:-/opt/caddy}"
PROJECT=research-engine
if [ -z "${DEPLOY_DIR:-}" ]; then
  if [ -L "$REPO_DIR/.deploy/current" ]; then
    DEPLOY_DIR="$(readlink -f "$REPO_DIR/.deploy/current")"
  else
    DEPLOY_DIR="$REPO_DIR"
  fi
fi

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

json_out() { # json_out key=value ... ; values that parse as JSON are embedded as JSON
  python3 - "$@" <<'PY'
import json, sys
out = {}
for arg in sys.argv[1:]:
    k, _, v = arg.partition("=")
    try:
        out[k] = json.loads(v)
    except ValueError:
        out[k] = v
print(json.dumps(out, sort_keys=True))
PY
}

fail() { # fail <exit-code> <command> <message> [extra key=value...]
  local code=$1 cmd=$2 msg=$3
  shift 3
  json_out ok=false command="$cmd" error="$msg" "$@"
  exit "$code"
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || fail 2 "${CMD:-ops}" "missing required command: $1"; }

env_get() { # read NAME from an env file (default $ENV_FILE) without sourcing it
  local file="${2:-$ENV_FILE}"
  [ -f "$file" ] || return 1
  grep -E "^$1=" "$file" | tail -n1 | cut -d= -f2-
}

redact() { sed -E 's/([A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*=)[^ "]*/\1***REDACTED***/g'; }

# GIT_SHA: never let an ops command start the app from a stale or :dev image.
if [ -z "${GIT_SHA:-}" ]; then
  GIT_SHA="$(git -C "$DEPLOY_DIR" rev-parse --short HEAD 2>/dev/null || true)"
  [ -n "$GIT_SHA" ] || fail 2 "${CMD:-ops}" "cannot determine GIT_SHA from $DEPLOY_DIR"
fi
export GIT_SHA

dc() {
  local files=(-f "$DEPLOY_DIR/compose.yaml")
  if [ -n "$(env_get APP_PORT || true)" ]; then files+=(-f "$DEPLOY_DIR/compose.debug.yaml"); fi
  docker compose -p "$PROJECT" --project-directory "$DEPLOY_DIR" "${files[@]}" --env-file "$ENV_FILE" "$@"
}

app_get() { # app_get /path -> body of an in-container GET to the app (no host port needed)
  dc exec -T app python -c "import sys,urllib.request;r=urllib.request.urlopen('http://127.0.0.1:8000$1',timeout=10);sys.stdout.write(r.read().decode())"
}

app_get_or_empty() { # like app_get but yields '' (not an exit) when the app is unreachable; 503 bodies kept
  app_get "$1" 2>/dev/null || true
}
