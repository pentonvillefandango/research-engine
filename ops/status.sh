#!/usr/bin/env bash
# Containers, volumes, version, last deploy and last backup. Read-only.
CMD=status
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
require_cmd python3

ids="$(dc ps -q)"
[ -n "$ids" ] || fail 1 status "no containers found for project $PROJECT"
# shellcheck disable=SC2086
inspect="$(docker inspect $ids)"
# shellcheck disable=SC2086
stats="$(docker stats --no-stream --format '{{json .}}' $ids)"
df="$(docker system df -v --format json)"
version="$(app_get /version 2>/dev/null || true)"
last_deploy="$(tail -n1 "$DEPLOYS_LOG" 2>/dev/null || true)"

summary="$(
  INSPECT="$inspect" STATS="$stats" DF="$df" VERSION="$version" LAST_DEPLOY="$last_deploy" \
    BACKUP_DIR="$BACKUP_DIR" PROJECT="$PROJECT" python3 - <<'PY'
import datetime, json, os
from pathlib import Path

stats = {}
for line in os.environ["STATS"].splitlines():
    if line.strip():
        s = json.loads(line)
        stats[s["Name"]] = s
containers = []
for c in json.loads(os.environ["INSPECT"]):
    name = c["Name"].lstrip("/")
    st = c["State"]
    s = stats.get(name, {})
    containers.append(
        {
            "name": name,
            "state": st.get("Status"),
            "health": (st.get("Health") or {}).get("Status", "none"),
            "restarts": c.get("RestartCount", 0),
            "cpu": s.get("CPUPerc"),
            "memory": s.get("MemUsage"),
        }
    )
prefix = os.environ["PROJECT"] + "_"
volumes = [
    {"name": v["Name"], "size": v.get("Size")}
    for v in json.loads(os.environ["DF"]).get("Volumes") or []
    if v["Name"].startswith(prefix)
]
try:
    version = json.loads(os.environ["VERSION"])["data"]
except (ValueError, KeyError):
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
    last_backup = {
        "name": newest.name,
        "size": stt.st_size,
        "time": datetime.datetime.fromtimestamp(stt.st_mtime, datetime.timezone.utc).isoformat(),
    }
print(json.dumps({"containers": containers, "volumes": volumes, "version": version,
                  "last_deploy": last_deploy, "last_backup": last_backup}))
PY
)"
log "status: $(printf '%s' "$summary" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["containers"]))') containers"
python3 - "$summary" <<'PY'
import json, sys
out = {"ok": True, "command": "status", **json.loads(sys.argv[1])}
print(json.dumps(out, sort_keys=True))
PY
