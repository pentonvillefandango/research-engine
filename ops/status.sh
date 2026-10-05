#!/usr/bin/env bash
# Containers, volumes, version, last deploy and last backup. Read-only.
CMD=status
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

ids="$(dc ps -a -q)"
expected="$(dc config --services)"
inspect=""
stats=""
if [ -n "$ids" ]; then
  inspect="$(inspect_fields "$ids")"
  # shellcheck disable=SC2086
  stats="$(docker stats --no-stream --format '{{json .}}' $ids 2>/dev/null || true)"
fi
df="$(docker system df -v --format json)"
version=""
[ -n "$ids" ] && version="$(app_get /version 2>/dev/null || true)"
last_deploy="$(grep -v '^[[:space:]]*$' "$DEPLOYS_LOG" 2>/dev/null | tail -n1 || true)"

summary="$(
  INSPECT="$inspect" STATS="$stats" DF="$df" VERSION="$version" LAST_DEPLOY="$last_deploy" \
    EXPECTED="$expected" BACKUP_DIR="$BACKUP_DIR" PROJECT="$PROJECT" python3 - <<'PY'
import datetime, json, os
from pathlib import Path

stats = {}
for line in os.environ["STATS"].splitlines():
    if line.strip():
        s = json.loads(line)
        stats[s["Name"]] = s
containers, running = [], set()
for line in os.environ["INSPECT"].splitlines():
    if not line.strip():
        continue
    name, state, health, restarts, service = line.split("\t")
    name = name.lstrip("/")
    s = stats.get(name, {})
    if state == "running":
        running.add(service)
    containers.append({"name": name, "state": state, "health": health,
                       "restarts": int(restarts), "cpu": s.get("CPUPerc"), "memory": s.get("MemUsage")})
missing = [x for x in os.environ["EXPECTED"].split() if x not in running]
prefix = os.environ["PROJECT"] + "_"
volumes = [{"name": v["Name"], "size": v.get("Size")}
           for v in json.loads(os.environ["DF"]).get("Volumes") or [] if v["Name"].startswith(prefix)]
try:
    version = json.loads(os.environ["VERSION"])["data"]
except (ValueError, KeyError, TypeError):
    version = None
try:
    last_deploy = json.loads(os.environ["LAST_DEPLOY"])
except ValueError:
    last_deploy = None
last_backup = None
bdir = Path(os.environ["BACKUP_DIR"])
files = [p for p in bdir.iterdir() if p.is_file()] if bdir.is_dir() else []
if files:
    newest = max(files, key=lambda p: p.stat().st_mtime)
    stt = newest.stat()
    last_backup = {"name": newest.name, "size": stt.st_size,
                   "time": datetime.datetime.fromtimestamp(stt.st_mtime, datetime.timezone.utc).isoformat()}
print(json.dumps({"ok": not missing, "command": "status", "containers": containers,
                  "not_running": missing, "volumes": volumes, "version": version,
                  "last_deploy": last_deploy, "last_backup": last_backup}, sort_keys=True))
PY
)"
emit_json_line "$summary"
[ "$(printf '%s' "$summary" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
