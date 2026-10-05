#!/usr/bin/env bash
# Online SQLite backup of the live app to $BACKUP_DIR, then retention (V1-22, §10).
# The app keeps running: `research-engine db backup` uses SQLite's backup API inside the container.
# Backups hold job data: the directory is 700 and every file 600.
CMD=backup
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

days="${BACKUP_RETENTION_DAYS-14}"
[[ "$days" =~ ^[0-9]+$ ]] && [ "$days" -ge 1 ] ||
  fail 2 backup "BACKUP_RETENTION_DAYS must be a whole number of days >= 1"
require_cmd docker

umask 077
mkdir -p "$BACKUP_DIR" || fail 1 backup "cannot create $BACKUP_DIR"
chmod 700 "$BACKUP_DIR" || fail 1 backup "cannot set mode 700 on $BACKUP_DIR"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
inner="/data/.backup-$ts.sqlite"
dest="$BACKUP_DIR/research-engine-$ts.sqlite"
part="$dest.part" # never matches research-engine-*.sqlite: retention and restore ignore it
[ ! -e "$dest" ] || fail 2 backup "a backup named $(basename "$dest") already exists"

INNER_LEFT=0
on_exit_hook() {
  if [ "$INNER_LEFT" = 1 ]; then dc exec -T app rm -f "$inner" >/dev/null 2>&1 || true; fi
  rm -f "$part"
}

log "backing up the app database"
INNER_LEFT=1
code=0
out="$(dc exec -T app research-engine db backup --out "$inner")" || code=$?
detail="$(LINE="$(printf '%s\n' "$out" | tail -n 1)" python3 -c '
import json, os
try:
    d = json.loads(os.environ["LINE"])
    if not isinstance(d, dict):
        raise ValueError
except ValueError:
    d = {"ok": False, "error": "no result line from research-engine db backup"}
print(json.dumps(d, sort_keys=True))')"
if [ "$code" -ne 0 ] || [ "$(printf '%s' "$detail" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ok") is True)')" != True ]; then
  fail 1 backup "in-container backup failed (exit $code)" detail:="$detail"
fi

log "copying it out to $dest"
dc cp "app:$inner" "$part" >&2 || fail 1 backup "docker compose cp failed"
dc exec -T app rm -f "$inner" >&2 || log "warning: could not remove $inner in the container"
INNER_LEFT=0
chmod 600 "$part" && mv -f "$part" "$dest" || fail 1 backup "cannot finalise $dest"
bytes="$(stat -c %s "$dest")"
[ "$bytes" -gt 0 ] || fail 1 backup "backup file is empty"

# Retention: only regular files named research-engine-*.sqlite directly in $BACKUP_DIR.
pruned="$(python3 - "$BACKUP_DIR" "$days" "$dest" <<'PY'
import json, os, re, sys, time
bdir, days, newest = sys.argv[1], int(sys.argv[2]), os.path.basename(sys.argv[3])
cutoff = time.time() - days * 86400
pat = re.compile(r"research-engine-.+\.sqlite")
pruned = []
for name in sorted(os.listdir(bdir)):
    path = os.path.join(bdir, name)
    if name == newest or not pat.fullmatch(name) or os.path.islink(path) or not os.path.isfile(path):
        continue
    if os.stat(path).st_mtime < cutoff:
        os.unlink(path)
        pruned.append(name)
print(json.dumps(pruned))
PY
)" || fail 1 backup "retention failed (the new backup was kept)" file="$dest"
[ "$pruned" = "[]" ] || log "pruned: $pruned"

json_out ok:=true command=backup file="$dest" bytes:="$bytes" pruned:="$pruned"
