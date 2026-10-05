#!/usr/bin/env bash
# Shared by ops/deploy.sh and ops/rollback.sh (V1-22, §10). Sourced AFTER ops/lib.sh.
# Deploys run from git worktrees under $REPO_DIR/.deploy/<short-sha>; .deploy/current points at the
# live one. The main working tree is never reset or checked out. deploys.jsonl is append-only.
# Functions here are called in `if`/`||` contexts, where bash ignores `set -e`: every step that
# can fail checks its own status and returns non-zero (with a reason) instead of relying on it.
DEPLOY_ROOT="$REPO_DIR/.deploy"
KEEP_RECENT=3 # worktrees and app images kept besides the current one
APP_IMAGE=research-engine-app

g() { git -C "$REPO_DIR" "$@"; }

short_sha() { g rev-parse --short="$SHORT_SHA_LEN" "$1^{commit}"; }

# resolve_commit sha: the full sha if it names a commit in this repo, else non-zero.
resolve_commit() { g rev-parse --verify --quiet "$1^{commit}"; }

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

# _is_worktree_at dir sha: dir is a real, clean worktree of its own (not a plain directory that
# `git -C` would resolve to the main repo) with HEAD at sha.
_is_worktree_at() {
  local dir=$1 sha=$2 real top
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 1
  real="$(cd "$dir" && pwd -P)" || return 1
  top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" || return 1
  [ "$top" = "$real" ] || return 1
  [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null)" = "$sha" ] || return 1
  [ -z "$(git -C "$dir" status --porcelain 2>/dev/null || echo dirty)" ]
}

# activate_worktree sha [branch]: create (or reuse a verified) .deploy/<short> at sha, optionally
# creating `branch` there first, point .deploy/current at it, and export DEPLOY_DIR/GIT_SHA for dc
# and the smoke/sandbox children. Never touches the main working tree. On failure returns 1 with
# ACTIVATE_ERROR set, and `current` is left exactly as it was.
activate_worktree() {
  local sha short wt
  ACTIVATE_ERROR=""
  sha="$(resolve_commit "$1")" || { ACTIVATE_ERROR="unknown commit $1"; return 1; }
  short="$(short_sha "$sha")" || { ACTIVATE_ERROR="cannot shorten $sha"; return 1; }
  wt="$DEPLOY_ROOT/$short"
  if [ -n "${2:-}" ]; then
    g branch "$2" "$sha" >&2 || { ACTIVATE_ERROR="could not create branch $2"; return 1; }
  fi
  # an empty directory (a killed `worktree add`) is not a worktree; it is safe to remove
  if [ -d "$wt" ] && [ ! -L "$wt" ] && [ -z "$(ls -A "$wt" 2>/dev/null)" ]; then
    rmdir "$wt" || { ACTIVATE_ERROR="could not remove empty .deploy/$short"; return 1; }
  fi
  if [ -e "$wt" ] || [ -L "$wt" ]; then
    _is_worktree_at "$wt" "$sha" || {
      ACTIVATE_ERROR="worktree .deploy/$short exists but is not a clean checkout of $short"
      return 1
    }
    log "reusing worktree .deploy/$short"
  else
    if [ -n "${2:-}" ]; then
      g worktree add "$wt" "$2" >&2 || { ACTIVATE_ERROR="git worktree add failed"; return 1; }
    else
      g worktree add --detach "$wt" "$sha" >&2 ||
        { ACTIVATE_ERROR="git worktree add failed"; return 1; }
    fi
    _is_worktree_at "$wt" "$sha" ||
      { ACTIVATE_ERROR="new worktree .deploy/$short failed verification"; return 1; }
  fi
  touch "$wt" || true # recency hint for retention only
  ln -sfn "$short" "$DEPLOY_ROOT/.current.tmp" &&
    mv -T "$DEPLOY_ROOT/.current.tmp" "$DEPLOY_ROOT/current" ||
    { ACTIVATE_ERROR="could not repoint .deploy/current"; return 1; }
  DEPLOY_DIR="$wt"
  GIT_SHA="$short"
  export DEPLOY_DIR GIT_SHA
}

# current_sha: the short sha .deploy/current points at, or "none".
current_sha() {
  local link="$DEPLOY_ROOT/current"
  [ -L "$link" ] && git -C "$link" rev-parse --short="$SHORT_SHA_LEN" HEAD 2>/dev/null || echo none
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
# lost), worktree, repoint current, up + smoke + sandbox, append the rollback entry (also when
# switching to the target fails). Sets ROLLBACK_TO (the target), ROLLBACK_BRANCH,
# ROLLBACK_ACTIVATED (true once `current` points at the target) and ROLLBACK_ERROR.
# Returns 0 only if the rolled-back stack passed.
rollback_to() {
  local sha=$1 from=$2 full
  ROLLBACK_ACTIVATED=false ROLLBACK_ERROR="" ROLLBACK_TO="$sha" ROLLBACK_BRANCH=""
  UP=skipped SMOKE=skipped SANDBOX=skipped DETAIL='null'
  if ! full="$(resolve_commit "$sha")" || ! ROLLBACK_TO="$(short_sha "$full")"; then
    ROLLBACK_ERROR="rollback target $sha is not a commit in this repository"
  else
    ROLLBACK_BRANCH="rollback/$(date -u +%Y%m%dT%H%M%SZ)"
    while g show-ref --verify --quiet "refs/heads/$ROLLBACK_BRANCH"; do
      ROLLBACK_BRANCH="${ROLLBACK_BRANCH%Z*}Z-$RANDOM"
    done
    log "rolling back to $ROLLBACK_TO on branch $ROLLBACK_BRANCH"
    if activate_worktree "$full" "$ROLLBACK_BRANCH"; then
      ROLLBACK_ACTIVATED=true
    else
      ROLLBACK_ERROR="$ACTIVATE_ERROR"
    fi
  fi
  if [ "$ROLLBACK_ACTIVATED" != true ]; then
    log "rollback to $ROLLBACK_TO failed: $ROLLBACK_ERROR (current is unchanged)"
    DETAIL="$(python3 -c 'import json,sys; print(json.dumps({"step": "worktree", "error": sys.argv[1]}))' "$ROLLBACK_ERROR")"
    record rollback "$from" "$ROLLBACK_TO" failed "$SMOKE" sandbox="$SANDBOX" \
      branch="$ROLLBACK_BRANCH" detail:="$DETAIL"
    return 1
  fi
  if up_and_check "$ROLLBACK_TO"; then
    record rollback "$from" "$ROLLBACK_TO" ok "$SMOKE" sandbox="$SANDBOX" \
      branch="$ROLLBACK_BRANCH" detail:="$DETAIL"
    return 0
  fi
  ROLLBACK_ERROR="rolled-back stack failed (up=$UP smoke=$SMOKE sandbox=$SANDBOX)"
  record rollback "$from" "$ROLLBACK_TO" failed "$SMOKE" sandbox="$SANDBOX" \
    branch="$ROLLBACK_BRANCH" detail:="$DETAIL"
  return 1
}

# seeded_running_sha: the pre-worktree version recorded on the first deploy (from_source=running),
# or nothing. Used to suggest `make rollback SHA=<it>`.
seeded_running_sha() {
  python3 - "$DEPLOYS_LOG" <<'PY'
import json, sys
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
except FileNotFoundError:
    lines = []
for line in reversed(lines):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if isinstance(e, dict) and e.get("from_source") == "running" and isinstance(e.get("from"), str):
        print(e["from"])
        break
PY
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
    sha="$(short_sha "$sha" 2>/dev/null || printf '%s' "$sha")"
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
    g worktree remove "$DEPLOY_ROOT/$name" >&2 || log "warning: could not remove .deploy/$name (left as is)"
  done
}

# _same_sha a b: equal as commit ids (one is a prefix of the other; log and tags may hold a 7-char
# pre-worktree sha such as the seeded running version, whose image tag is 7 chars too).
_same_sha() { [ -n "$1" ] && [ -n "$2" ] && { [[ "$1" == "$2"* ]] || [[ "$2" == "$1"* ]]; }; }
_kept() { # _kept tag keep...: tag matches one of the kept shas
  local tag=$1 k
  shift
  for k in "$@"; do _same_sha "$tag" "$k" && return 0; done
  return 1
}

# prune_images: keep research-engine-app:<sha> for current plus the last KEEP_RECENT ok deploys;
# remove other sha-tagged app images by explicit tag, never one a container uses, never prune.
prune_images() {
  local cur keep=() n=0 sha tag in_use
  cur="$(readlink "$DEPLOY_ROOT/current" 2>/dev/null || true)"
  [ -n "$cur" ] && keep+=("$cur")
  while IFS= read -r sha; do
    [ -n "$sha" ] && ! _same_sha "$sha" "$cur" || continue
    [ "$n" -lt "$KEEP_RECENT" ] || break
    keep+=("$sha")
    n=$((n + 1))
  done < <(_recent_shas ok)
  in_use="$(docker ps -a --format '{{.Image}}' 2>/dev/null || true)"
  for tag in $(docker image ls "$APP_IMAGE" --format '{{.Tag}}' 2>/dev/null || true); do
    [[ "$tag" =~ ^[0-9a-f]{7,40}$ ]] || continue
    _kept "$tag" "${keep[@]}" && continue
    grep -qxF "$APP_IMAGE:$tag" <<<"$in_use" && continue
    log "removing old image $APP_IMAGE:$tag"
    docker image rm "$APP_IMAGE:$tag" >&2 || log "warning: could not remove $APP_IMAGE:$tag (left as is)"
  done
}
