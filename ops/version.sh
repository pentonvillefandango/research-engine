#!/usr/bin/env bash
# Running commit, tag and image versions. Read-only.
CMD=version
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

body="$(app_get /version 2>/dev/null || true)"
images="$(dc images --format json)"
result="$(BODY="$body" IMAGES="$images" python3 - <<'PY'
import json, os
try:
    v = json.loads(os.environ["BODY"])["data"]
except (ValueError, KeyError, TypeError):
    v = {}
raw = os.environ["IMAGES"].strip()
rows = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x.strip()]
images = [{"container": r.get("ContainerName"), "repository": r.get("Repository"),
           "tag": r.get("Tag"), "id": r.get("ID")} for r in rows]
print(json.dumps({"ok": bool(v), "command": "version", "version": v.get("version"),
                  "git_sha": v.get("git_sha"), "schema_version": v.get("schema_version"),
                  "images": images}, sort_keys=True))
PY
)"
emit_json_line "$result"
[ "$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
