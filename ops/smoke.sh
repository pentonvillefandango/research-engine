#!/usr/bin/env bash
# Demo set against the live API, <2 min (V1-22, V1-21). Read-only apart from the requests it
# makes. Runs `research-engine smoke` INSIDE the app container against http://127.0.0.1:8000, so
# no host port or TLS trust is needed; API_KEY and SITE_HOST come from the container's own
# environment (never from a command line). The CLI bounds itself with a 110 s deadline.
# Prints the CLI's JSON line; exit 0 only if the CLI exited 0 and reported ok.
CMD=smoke
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker

log "smoke: running the demo set inside the app container"
code=0
out="$(dc exec -T app research-engine smoke --url http://127.0.0.1:8000 2>/dev/null)" || code=$?

# The CLI's result is its last stdout line; anything else (a traceback, a docker error) is
# reported only as a generic failure, never echoed.
line="$(printf '%s\n' "$out" | tail -n 1 | redact)"
result="$(LINE="$line" CODE="$code" python3 - <<'PY'
import json, os
try:
    d = json.loads(os.environ["LINE"])
    if not isinstance(d, dict) or not isinstance(d.get("ok"), bool):
        raise ValueError
except ValueError:
    d = {"ok": False, "error": f"no smoke result from the app container (exit {os.environ['CODE']})"}
d["command"] = "smoke"
if os.environ["CODE"] != "0":
    d["ok"] = False
print(json.dumps(d, sort_keys=True))
PY
)"
emit_json_line "$result"
[ "$code" -eq 0 ] && [ "$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["ok"])')" = "True" ] || exit 1
