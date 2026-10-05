#!/usr/bin/env bash
# Shared by ops/deploy.sh and ops/rollback.sh (V1-22, §10). Sourced AFTER ops/lib.sh.
# Deploys run from git worktrees under $REPO_DIR/.deploy/<short-sha>; .deploy/current points at the
# live one. The main working tree is never reset or checked out. deploys.jsonl is append-only.
DEPLOY_ROOT="$REPO_DIR/.deploy"
KEEP_RECENT=3 # worktrees and app images kept besides the current one
APP_IMAGE=research-engine-app

g() { git -C "$REPO_DIR" "$@"; }

short_sha() { g rev-parse --short "$1^{commit}"; }

# take_lock: one deploy or rollback at a time (flock on .deploy/lock; held until the process exits).
take_lock() {
  require_cmd flock
  mkdir -p "$DEPLOY_ROOT"
  exec 9>"$DEPLOY_ROOT/lock"
  flock -n 9 || fail 2 "$CMD" "another deploy or rollback is running (.deploy/lock is held)"
}

# last_good_sha [exclude]: the `to` of the newest ok deploy/rollback entry, skipping any sha equal
# to `exclude` (prefix match either way, so short and full shas compare). Prints "none" if absent.
last_good_sha() {
  python3 - "$DEPLOYS_LOG" "${1:-}" <<'PY'
import json, sys
path, exclude = sys.argv[1], sys.argv[2]
def same(a, b):
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    lines = []
for line in reversed(lines):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if not isinstance(e, dict) or e.get("result") != "ok":
        continue
    sha = e.get("to") or e.get("sha")  # "sha": pre-worktree entries
    if isinstance(sha, str) and sha and not same(sha, exclude):
        print(sha)
        break
else:
    print("none")
PY
}

# record action from to result smoke [key=value | key:=json ...]: append one UTC-stamped line.
record() {
  local action=$1 from=$2 to=$3 result=$4 smoke=$5
  shift 5
  python3 - "$DEPLOYS_LOG" action="$action" from="$from" to="$to" result="$result" \
    smoke="$smoke" "$@" <<'PY'
import datetime, json, sys
entry = {"ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
for arg in sys.argv[2:]:
    k, _, v = arg.partition("=")
    if k.endswith(":"):
        entry[k[:-1]] = json.loads(v)
    else:
        entry[k] = v
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry, sort_keys=True) + "\n")
PY
}

# activate_worktree sha [branch]: create (or reuse) .deploy/<short> at sha, optionally creating
# `branch` there first, point .deploy/current at it, and export DEPLOY_DIR/GIT_SHA for dc and the
# smoke/sandbox children. Never touches the main working tree.
activate_worktree() {
  local sha short wt
  sha="$(g rev-parse --verify "$1^{commit}")"
  short="$(short_sha "$sha")"
  wt="$DEPLOY_ROOT/$short"
  if [ -n "${2:-}" ]; then g branch "$2" "$sha" >&2; fi
  if [ -e "$wt" ]; then
    [ "$(git -C "$wt" rev-parse HEAD 2>/dev/null || true)" = "$sha" ] &&
      [ -z "$(git -C "$wt" status --porcelain 2>/dev/null || echo dirty)" ] ||
      fail 2 "$CMD" "worktree .deploy/$short exists but is not a clean checkout of $short"
    log "reusing worktree .deploy/$short"
  elif [ -n "${2:-}" ]; then
    g worktree add "$wt" "$2" >&2
  else
    g worktree add --detach "$wt" "$sha" >&2
  fi
  touch "$wt"
  ln -sfn "$short" "$DEPLOY_ROOT/.current.tmp"
  mv -T "$DEPLOY_ROOT/.current.tmp" "$DEPLOY_ROOT/current"
  DEPLOY_DIR="$wt"
  GIT_SHA="$short"
  export DEPLOY_DIR GIT_SHA
}

# bring_up short_sha: build and start the stack from the active worktree; waits for health.
# Runs with the worktree as cwd as well as --project-directory, so every relative path (build
# context, bind mounts, and security_opt's seccomp profile, which compose may read relative to
# the cwd) is the deployed commit's own file.
bring_up() {
  log "bringing up $PROJECT at $1 from .deploy/$1"
  (cd "$DEPLOY_DIR" && GIT_SHA="$1" dc up -d --build --wait --wait-timeout 300 2>&1 | redact >&2)
}

# _run_check <name> <command>: runs a check script, stores its final JSON line in CHECK_DETAIL.
CHECK_DETAIL='null'
_run_check() {
  local out code=0
  out="$("$2")" || code=$?
  CHECK_DETAIL="$(LINE="$(printf '%s\n' "$out" | tail -n 1)" python3 -c '
import json, os
try:
    d = json.loads(os.environ["LINE"])
    if not isinstance(d, dict):
        raise ValueError
except ValueError:
    d = {"ok": False, "error": "no result line"}
print(json.dumps(d, sort_keys=True))')"
  log "$1: exit $code"
  return "$code"
}

run_smoke() { _run_check smoke "${SMOKE_CMD:-$REPO_DIR/ops/smoke.sh}"; }
run_sandbox() { _run_check sandbox "${SANDBOX_CMD:-$REPO_DIR/ops/sandbox.sh}"; }

# up_and_check short_sha: bring_up, smoke, sandbox. Sets UP SMOKE SANDBOX (pass|fail|skipped) and
# DETAIL (JSON: the failing step's result). Returns 0 only if all three passed.
up_and_check() {
  UP=fail SMOKE=skipped SANDBOX=skipped DETAIL='null'
  if ! bring_up "$1"; then
    DETAIL='{"error": "docker compose up failed", "step": "up"}'
    return 1
  fi
  UP=pass
  if ! run_smoke; then
    SMOKE=fail DETAIL="$CHECK_DETAIL"
    return 1
  fi
  SMOKE=pass
  if ! run_sandbox; then
    SANDBOX=fail DETAIL="$CHECK_DETAIL"
    return 1
  fi
  SANDBOX=pass
}

# rollback_to sha from: the rollback routine. Branch rollback/<UTC-ts> at sha (§10: no work is
# lost), worktree, repoint current, up + smoke + sandbox, append the rollback entry.
# Sets ROLLBACK_BRANCH and ROLLBACK_TO; returns 0 only if the rolled-back stack passed.
rollback_to() {
  local sha=$1 from=$2 result=ok rc=0
  ROLLBACK_BRANCH="rollback/$(date -u +%Y%m%dT%H%M%SZ)"
  while g show-ref --verify --quiet "refs/heads/$ROLLBACK_BRANCH"; do
    ROLLBACK_BRANCH="${ROLLBACK_BRANCH%Z*}Z-$RANDOM"
  done
  ROLLBACK_TO="$(short_sha "$sha")"
  log "rolling back to $ROLLBACK_TO on branch $ROLLBACK_BRANCH"
  activate_worktree "$sha" "$ROLLBACK_BRANCH"
  up_and_check "$ROLLBACK_TO" || { result=failed rc=1; }
  record rollback "$from" "$ROLLBACK_TO" "$result" "$SMOKE" sandbox="$SANDBOX" \
    branch="$ROLLBACK_BRANCH" detail:="$DETAIL"
  return "$rc"
}

# _recent_shas [ok]: distinct deployed shas from deploys.jsonl, newest first: each entry's `to`
# (only ok entries with "ok"), plus a seeded `from` (from_source=running) as an older one.
_recent_shas() {
  python3 - "$DEPLOYS_LOG" "${1:-}" <<'PY'
import json, sys
seen = []
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
except FileNotFoundError:
    lines = []
for line in reversed(lines):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if not isinstance(e, dict):
        continue
    shas = [e.get("to")] if sys.argv[2] != "ok" or e.get("result") == "ok" else []
    if e.get("from_source") == "running":  # the pre-worktree deploy, seeded on the first deploy
        shas.append(e.get("from"))
    for sha in shas:
        if isinstance(sha, str) and sha and sha not in seen:
            seen.append(sha)
print("\n".join(seen))
PY
}

# prune_worktrees: keep current plus the KEEP_RECENT most recently deployed others; remove the
# rest with `git worktree remove` (never --force), and only directories directly under .deploy/.
prune_worktrees() {
  local cur keep=() name n=0 sha
  cur="$(readlink "$DEPLOY_ROOT/current" 2>/dev/null || true)"
  [ -n "$cur" ] && keep+=("$cur")
  while IFS= read -r sha; do
    [ -n "$sha" ] && [ "$sha" != "$cur" ] && [ -d "$DEPLOY_ROOT/$sha" ] || continue
    [ "$n" -lt "$KEEP_RECENT" ] || break
    keep+=("$sha")
    n=$((n + 1))
  done < <(_recent_shas)
  # worktrees not in the log yet: newest by mtime, until the quota is full
  while IFS= read -r name; do
    [ "$n" -lt "$KEEP_RECENT" ] || break
    [[ " ${keep[*]} " == *" $name "* ]] && continue
    keep+=("$name")
    n=$((n + 1))
  done < <(cd "$DEPLOY_ROOT" && ls -1t 2>/dev/null | grep -E '^[0-9a-f]{4,40}$' || true)
  for name in $(cd "$DEPLOY_ROOT" && ls -1 | grep -E '^[0-9a-f]{4,40}$' || true); do
    [[ " ${keep[*]} " == *" $name "* ]] && continue
    [ -d "$DEPLOY_ROOT/$name" ] && [ ! -L "$DEPLOY_ROOT/$name" ] || continue
    log "removing old worktree .deploy/$name"
    g worktree remove "$DEPLOY_ROOT/$name" >&2 || log "could not remove .deploy/$name (left as is)"
  done
}

# prune_images: keep research-engine-app:<sha> for current plus the last KEEP_RECENT ok deploys;
# remove other sha-tagged app images by explicit tag, never one a container uses, never prune.
prune_images() {
  local cur keep=() n=0 sha tag in_use
  cur="$(readlink "$DEPLOY_ROOT/current" 2>/dev/null || true)"
  [ -n "$cur" ] && keep+=("$cur")
  while IFS= read -r sha; do
    [ -n "$sha" ] && [ "$sha" != "$cur" ] || continue
    [ "$n" -lt "$KEEP_RECENT" ] || break
    keep+=("$sha")
    n=$((n + 1))
  done < <(_recent_shas ok)
  in_use="$(docker ps -a --format '{{.Image}}' 2>/dev/null || true)"
  for tag in $(docker image ls "$APP_IMAGE" --format '{{.Tag}}' 2>/dev/null || true); do
    [[ "$tag" =~ ^[0-9a-f]{7,40}$ ]] || continue
    [[ " ${keep[*]} " == *" $tag "* ]] && continue
    grep -qxF "$APP_IMAGE:$tag" <<<"$in_use" && continue
    log "removing old image $APP_IMAGE:$tag"
    docker image rm "$APP_IMAGE:$tag" >&2 || log "could not remove $APP_IMAGE:$tag (left as is)"
  done
}
