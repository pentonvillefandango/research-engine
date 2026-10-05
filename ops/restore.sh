#!/usr/bin/env bash
# Restore a backup into the live app (V1-22, §10). Stops the app, so the owner approves every run.
# Usage: ops/restore.sh FILE   (a research-engine-*.sqlite directly inside $BACKUP_DIR)
#
# While the app still runs: validate FILE (check-only CLI run), then take a pre-restore safety
# backup (research-engine-<ts>-prerestore.sqlite, restorable like any other). Either failing
# aborts with the app untouched. Only then: stop app, restore, start app, health.
CMD=restore
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ -n "${1:-}" ] || fail 2 restore "usage: ops/restore.sh FILE (a research-engine-*.sqlite in $BACKUP_DIR)"

# FILE must resolve (symlinks and .. included) to a regular file directly inside BACKUP_DIR
# whose name is research-engine-<stamp>.sqlite. Prints the resolved path, or the reason (exit 1).
reason_or_path="$(python3 - "$1" "$BACKUP_DIR" <<'PY'
import os, re, stat, sys
arg, bdir = sys.argv[1], sys.argv[2]
real, rdir = os.path.realpath(arg), os.path.realpath(bdir)
def no(msg):
    print(msg)
    sys.exit(1)
if not os.path.lexists(arg):
    no(f"no such file: {arg}")
if os.path.dirname(real) != rdir:
    no(f"not directly inside the backup directory {bdir}: {arg}")
if not re.fullmatch(r"research-engine-[0-9A-Za-z][0-9A-Za-z._:-]*\.sqlite", os.path.basename(real)):
    no(f"not a research-engine-*.sqlite backup: {arg}")
try:
    st = os.stat(real)
except OSError:
    no(f"cannot stat {arg}")
if not stat.S_ISREG(st.st_mode):
    no(f"not a regular file: {arg}")
print(real)
PY
)" || fail 2 restore "$reason_or_path"
src="$reason_or_path"
name="$(basename "$src")"
# Only the main file is staged and mounted: a WAL-mode copy's -wal content would be lost.
if [ -s "$src-wal" ]; then
  fail 2 restore "$name has a non-empty -wal file beside it; checkpoint it first, or restore a file made by make backup"
fi
require_cmd docker
require_cmd flock

# One restore, deploy or rollback at a time (they share .deploy/lock).
mkdir -p "$REPO_DIR/.deploy"
exec 9>"$REPO_DIR/.deploy/lock"
flock -n 9 || fail 2 restore "a deploy, rollback or restore is running (.deploy/lock is held)"

# The app runs as uid 10001 and the backup is 600 (owner only). Stage a 644 copy in a fresh 700
# directory inside the (700) backup dir: the container can read it through the bind mount, while
# on the host it stays reachable by the owner only. The backup itself is never loosened.
stage=""
APP_STOPPED=0
on_exit_hook() {
  if [ "$APP_STOPPED" = 1 ]; then dc start app >/dev/null 2>&1 || true; fi
  if [ -n "$stage" ]; then rm -rf -- "$stage"; fi
}
stage="$(mktemp -d "$BACKUP_DIR/.restore.XXXXXX")" || fail 1 restore "cannot create a staging dir"
chmod 700 "$stage"
install -m 644 "$src" "$stage/restore.sqlite" || fail 1 restore "cannot stage $name"

# last_json <output>: the output's last line if it is a JSON object, else {"ok": false, ...}
last_json() {
  LINE="$(printf '%s\n' "$1" | tail -n 1)" python3 -c '
import json, os
try:
    d = json.loads(os.environ["LINE"])
    if not isinstance(d, dict):
        raise ValueError
except ValueError:
    d = {"ok": False, "error": "no result line"}
print(json.dumps(d, sort_keys=True))'
}
json_field() { printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get(sys.argv[1])))' "$2"; }
restore_cli() { # restore_cli [--check]: the CLI in a one-off app container, the staged copy mounted ro
  dc run --rm --no-deps -T -v "$stage/restore.sqlite:/restore.sqlite:ro" app \
    research-engine db restore "$@" --from /restore.sqlite
}

log "validating $name (the app keeps running)"
code=0
out="$(restore_cli --check)" || code=$?
detail="$(last_json "$out")"
if [ "$code" -ne 0 ] || [ "$(json_field "$detail" ok)" != true ]; then
  fail 1 restore "$name failed validation; nothing was changed" restored:=null detail:="$detail"
fi

log "taking a pre-restore safety backup"
code=0
out="$(OPS_LOCK_HELD=1 "${BACKUP_CMD:-$REPO_DIR/ops/backup.sh}" --tag prerestore)" || code=$?
safety="$(last_json "$out")"
safety_file="$(json_field "$safety" file)" # JSON: a quoted path, or null
if [ "$code" -ne 0 ] || [ "$(json_field "$safety" ok)" != true ] || [ "$safety_file" = null ]; then
  fail 1 restore "pre-restore safety backup failed; nothing was changed" restored:=null \
    detail:="$safety"
fi

log "stopping app"
dc stop app >&2 ||
  fail 1 restore "could not stop the app" restored:=null safety_backup:="$safety_file"
APP_STOPPED=1

log "restoring $name"
code=0
out="$(restore_cli)" || code=$?
detail="$(last_json "$out")"
# The CLI reports replaced=false on every failure (all happen before its final atomic rename).
if [ "$(json_field "$detail" ok)" = true ]; then
  replaced=true
elif [ "$(json_field "$detail" replaced)" = false ]; then
  replaced=false
else
  replaced=unknown
fi

log "starting app"
started=true
dc start --wait --wait-timeout 300 app >&2 || started=false
APP_STOPPED=0

hcode=0
hout="$("$REPO_DIR/ops/health.sh")" || hcode=$?
health="$(last_json "$hout")"
log "health: exit $hcode"

common=(health:="$health" safety_backup:="$safety_file" detail:="$detail")
case "$replaced" in
  false)
    fail 1 restore "restore failed (exit $code); the previous database is still in place" \
      restored:=null "${common[@]}"
    ;;
  unknown)
    fail 1 restore "restore failed (exit $code) without a result; the database may or may not have been replaced: check detail and health, the safety backup is in safety_backup" \
      restored:=null "${common[@]}"
    ;;
esac
if [ "$code" -ne 0 ]; then
  fail 1 restore "the database was replaced, but docker compose run exited $code" \
    restored="$name" "${common[@]}"
fi
if [ "$started" != true ] || [ "$hcode" -ne 0 ]; then
  fail 1 restore "restored, but the app is not healthy" restored="$name" "${common[@]}"
fi
json_out ok:=true command=restore restored="$name" health:="$health" safety_backup:="$safety_file"
