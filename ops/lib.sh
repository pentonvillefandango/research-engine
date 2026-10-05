#!/usr/bin/env bash
# Shared helpers for ops scripts. Contract: progress on stderr; final stdout line is one JSON object.
# Exit codes: 0 ok, 1 check/command failed, 2 usage or precondition error.
set -euo pipefail

# python3 builds every JSON line; without it, emit a hand-built line (the one exception) and stop.
if ! command -v python3 >/dev/null 2>&1; then
  printf '{"command": "%s", "error": "missing required command: python3", "ok": false}\n' "${CMD:-ops}"
  exit 2
fi

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

FINAL_EMITTED=0

# Any unexpected failure still ends with exactly one JSON line (never the failing command line,
# which could carry secrets) and exit 1. A script that already emitted its JSON line is left alone.
# A script may define on_exit_hook (e.g. temp-file clean-up); it runs first, on every exit.
_on_exit() {
  local code=$?
  if declare -F on_exit_hook >/dev/null; then on_exit_hook || true; fi
  if [ "$code" -ne 0 ] && [ "$FINAL_EMITTED" -ne 1 ]; then
    FINAL_EMITTED=1
    python3 -c 'import json,sys; print(json.dumps({"ok": False, "command": sys.argv[1], "error": "unexpected failure (exit " + sys.argv[2] + ")"}, sort_keys=True))' "${CMD:-ops}" "$code" || true
    exit 1
  fi
}
trap _on_exit EXIT

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# json_out key=value ... (string) or key:=json ... (typed JSON). Prints the final line.
json_out() {
  python3 - "$@" <<'PY'
import json, sys
out = {}
for arg in sys.argv[1:]:
    k, sep, v = arg.partition("=")
    if k.endswith(":"):
        out[k[:-1]] = json.loads(v)
    else:
        out[k] = v
print(json.dumps(out, sort_keys=True))
PY
  FINAL_EMITTED=1
}

emit_json_line() { # emit_json_line '<json>' : print an already-built final line
  printf '%s\n' "$1"
  FINAL_EMITTED=1
}

fail() { # fail <exit-code> <command> <message> [extra key=value | key:=json ...]
  local code=$1 cmd=$2 msg=$3
  shift 3
  json_out ok:=false command="$cmd" error="$msg" "$@"
  exit "$code"
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || fail 2 "${CMD:-ops}" "missing required command: $1"; }

# env_get NAME [FILE]: read one value without sourcing. Handles `export `, quotes, CRLF, inline
# ` #` comments and `=` in the value. Exit 1 if the file or the name is absent.
env_get() {
  local file="${2:-$ENV_FILE}"
  [ -r "$file" ] || return 1
  python3 - "$1" "$file" <<'PY'
import re, sys
name, path = sys.argv[1], sys.argv[2]
found = None
with open(path, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        m = re.match(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=(.*)$", line.rstrip("\r\n"))
        if m:
            found = m.group(1).strip()
if found is None:
    sys.exit(1)
if found[:1] in "\"'" and found[:1]:
    q = found[0]
    end = found.find(q, 1)
    val = found[1:end] if end > 0 else found[1:]
else:
    val = re.split(r"\s+#", found, maxsplit=1)[0].strip()
print(val)
PY
}

# weak_secrets: names (space-separated, never values) of the secrets in $ENV_FILE that are missing,
# the .env.example placeholder, or shorter than 32 characters. The app refuses the first three
# itself (Settings); SEARXNG_SECRET is compose-only, so this is its only check.
SECRET_NAMES="API_KEY SESSION_SECRET CRAWL4AI_API_TOKEN SEARXNG_SECRET"
SECRET_HINT="each must be a random secret of at least 32 characters, not the .env.example placeholder; generate one with: openssl rand -hex 32"
weak_secrets() {
  local name value weak=()
  for name in $SECRET_NAMES; do
    value="$(env_get "$name" || true)"
    if [ "$value" = change-me ] || [ "${#value}" -lt 32 ]; then weak+=("$name"); fi
  done
  value=""
  printf '%s' "${weak[*]}"
}

# names_json "A B": '["A", "B"]' (for json_out key:=...)
names_json() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split()))' "$1"; }

# redact: stdin -> stdout, masks values of key/token/secret/password-like names (case-insensitive)
# in NAME=value and name: value (a quoted value, or an unquoted one up to the end of the line),
# JSON "name": "value", and X-API-Key / Authorization / Cookie / Set-Cookie headers.
_REDACT_PY='
import re, sys
M = "***REDACTED***"
kw = r"(?:key|token|secret|password|passwd)"
j = re.compile(r"(\"[^\"]*" + kw + r"[^\"]*\"\s*:\s*)(\"(?:[^\"\\]|\\.)*\"|[^,}\s]+)", re.I)
h = re.compile(r"(?<![\"\w-])((?:x-api-key|authorization|proxy-authorization|set-cookie|cookie)\s*:\s*)[^\r\n]*", re.I)
v = r"(\"(?:[^\"\\]|\\.)*\"|\x27[^\x27]*\x27|[^\r\n]*\S)"
e = re.compile(r"([A-Za-z0-9_.-]*" + kw + r"[A-Za-z0-9_.-]*\s*[=:]\s*)" + v, re.I)
for line in sys.stdin:
    line = j.sub(lambda m: m.group(1) + "\"" + M + "\"", line)
    line = h.sub(lambda m: m.group(1) + M, line)
    line = e.sub(lambda m: m.group(1) + M, line)
    sys.stdout.write(line)
'
redact() { python3 -c "$_REDACT_PY"; }

# GIT_SHA: never let an ops command start the app from a stale or :dev image. A fixed short
# length, so a commit's image tag and .deploy/ name never change as the repo grows.
SHORT_SHA_LEN=12
if [ -z "${GIT_SHA:-}" ]; then
  GIT_SHA="$(git -C "$DEPLOY_DIR" rev-parse --short=$SHORT_SHA_LEN HEAD 2>/dev/null || true)"
  [ -n "$GIT_SHA" ] || fail 2 "${CMD:-ops}" "cannot determine GIT_SHA from $DEPLOY_DIR"
fi
export GIT_SHA

dc() {
  local files=(-f "$DEPLOY_DIR/compose.yaml")
  if [ -n "$(env_get APP_PORT || true)" ]; then files+=(-f "$DEPLOY_DIR/compose.debug.yaml"); fi
  docker compose -p "$PROJECT" --project-directory "$DEPLOY_DIR" "${files[@]}" --env-file "$ENV_FILE" "$@"
}

# app_get /path -> body of an in-container GET to the app (no host port needed).
# HTTP error responses (e.g. /health 503) still print their body.
app_get() {
  dc exec -T app python -c "
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000$1', timeout=10)
except urllib.error.HTTPError as e:
    r = e
sys.stdout.write(r.read().decode())
"
}

app_get_or_empty() { app_get "$1" 2>/dev/null || true; }

# Fields needed per container, selected so Config.Env (secrets) is never read:
# name, state, health, restarts, compose service. Tab-separated, one line per container.
inspect_fields() {
  # shellcheck disable=SC2086
  docker inspect --format '{{.Name}}	{{.State.Status}}	{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}	{{.RestartCount}}	{{index .Config.Labels "com.docker.compose.service"}}' $1
}
