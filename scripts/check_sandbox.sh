#!/usr/bin/env bash
# Verifies Crawl4AI renders with Chromium's sandbox ON (ADR-0022). Prints one JSON line; exit 0
# only if everything holds:
#   - the add-on announced itself ("active") in the container logs since start, with no WARNING;
#   - for each launch path (default Playwright launch, and browser_mode=builtin, which goes through
#     ManagedBrowser's own Popen), a crawl of https://example.com succeeds with non-empty markdown,
#     and at least one renderer NEW during that crawl is sandboxed: Seccomp: 2, more seccomp
#     filters than its own parent browser process (Docker's filter alone already gives every
#     process Seccomp: 2, so the extra filter is Chromium's seccomp-bpf), and a user namespace
#     different from that browser's;
#   - no Chromium process carries --no-sandbox at any sample during either crawl.
set -euo pipefail
cd "$(dirname "$0")/.."
source <(scripts/dev_urls.sh)
TOKEN=$(grep -E '^CRAWL4AI_API_TOKEN=' .env | cut -d= -f2-)
dc() { docker compose -f compose.yaml -f compose.dev.yaml "$@"; }
out=$(mktemp)
trap 'rm -f "$out"' EXIT

# In-container probe. `python3 -I` ignores PYTHONPATH, so the add-on is not loaded here. It snapshots
# the renderer PIDs, prints "ready", then samples /proc every 50 ms until it reads a line on stdin,
# and prints {"no_sandbox":N,"new_renderers":N,"sandboxed":N}. Matching is on argv0 only.
# argv[1] picks the launch path: renderers count only if their own browser process is of that kind
# (Playwright's launch() uses --remote-debugging-pipe; ManagedBrowser uses --remote-debugging-port=).
probe='
import json, os, select, sys, time
def chrom():
    res = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            argv = open(f"/proc/{p}/cmdline", "rb").read().decode(errors="replace").split("\0")
        except OSError:
            continue
        line = " ".join(a for a in argv if a)
        if line and "chrom" in os.path.basename(line.split(" ")[0]):
            res[int(p)] = line
    return res
def status(p):
    d = {}
    for ln in open(f"/proc/{p}/status"):
        k, _, v = ln.partition(":")
        d[k] = v.strip()
    return d
def browser_of(p, procs):
    while p > 1:
        p = int(status(p)["PPid"])
        if p in procs and "--type=" not in procs[p]:
            return p
    return None
KIND = {"default": "--remote-debugging-pipe", "builtin": "--remote-debugging-port="}[sys.argv[1]]
def check(p, procs):
    b = browser_of(p, procs)
    if b is None or KIND not in procs[b]:
        return None  # renderer of another browser (other launch path, pool churn): not counted
    r, bs = status(p), status(b)
    return (r.get("Seccomp") == "2"
            and int(r.get("Seccomp_filters", 0)) > int(bs.get("Seccomp_filters", 0))
            and os.readlink(f"/proc/{p}/ns/user") != os.readlink(f"/proc/{b}/ns/user"))
base = {p for p, c in chrom().items() if "--type=renderer" in c}
print("ready", flush=True)
nosb, seen, stop = set(), {}, False
while True:
    procs = chrom()
    nosb |= {p for p, c in procs.items() if "--no-sandbox" in c.split(" ")}
    for p, c in procs.items():
        # a renderer installs its seccomp-bpf shortly after fork, so it counts as sandboxed once
        # any sample sees it fully sandboxed; one never seen sandboxed counts as a failure
        if "--type=renderer" in c and p not in base and not seen.get(p):
            try:
                ok = check(p, procs)
            except (OSError, KeyError, ValueError):
                ok = None
            if ok is not None:
                seen[p] = ok
    if stop:
        break
    if select.select([sys.stdin], [], [], 0.05)[0]:
        stop = True
print(json.dumps({"no_sandbox": len(nosb), "new_renderers": len(seen),
                  "sandboxed": sum(seen.values())}), flush=True)
'

# run_path <default|builtin> <crawl body JSON>: prints "<crawl_ok> <no_sandbox> <new_renderers> <sandboxed>"
run_path() {
  local kind=$1 body=$2 ready="" result="" ok
  coproc PROBE { dc exec -T crawl4ai python3 -I -c "$probe" "$kind" 2>/dev/null; }
  local pin=${PROBE[1]} pout=${PROBE[0]} ppid=$PROBE_PID
  read -r -t 60 ready <&"$pout" || true
  # token goes in through stdin (-H @-), never onto a command line
  printf 'Authorization: Bearer %s\n' "$TOKEN" |
    curl -s -m 120 -H @- -H 'Content-Type: application/json' -d "$body" \
      "$CRAWL4AI_LIVE_URL/crawl" > "$out" || true
  sleep 1  # let the probe catch the tail of the crawl
  echo stop >&"$pin" || true
  read -r -t 30 result <&"$pout" || true
  wait "$ppid" 2>/dev/null || true
  ok=$(python3 -c 'import json,sys
try:
    r=json.load(open(sys.argv[1]))["results"][0]; print(str(bool(r["success"] and r["markdown"])).lower())
except Exception:
    print("false")' "$out")
  python3 -c 'import json,sys
ok, ready, raw = sys.argv[1:4]
try:
    d = json.loads(raw) if ready == "ready" else {}
except ValueError:
    d = {}
print(ok, d.get("no_sandbox", -1), d.get("new_renderers", 0), d.get("sandboxed", 0))' \
    "$ok" "$ready" "$result"
}

path_ok() { # crawl_ok no_sandbox new sandboxed
  [ "$1" = true ] && [ "$2" -eq 0 ] && [ "$3" -gt 0 ] && [ "$4" -eq "$3" ] && echo true || echo false
}

# A per-run viewport_width (an allowlisted, harmless BrowserConfig field) gives each crawl a new
# pool signature, so Crawl4AI does a FRESH launch on both paths and the renderers checked are
# guaranteed new. Pooled browsers would otherwise reuse renderers (Chrome reuses same-site
# processes). The extra browsers are reaped by Crawl4AI's pool janitor (idle_ttl_sec: 300).
W=$((1000 + RANDOM % 900))
CFG='"crawler_config":{"type":"CrawlerRunConfig","params":{"cache_mode":"bypass"}}'
URL='"urls":["https://example.com"]'
read -r d_crawl d_ns d_new d_sb < <(run_path default \
  "{$URL,$CFG,\"browser_config\":{\"type\":\"BrowserConfig\",\"params\":{\"viewport_width\":$W}}}")
read -r b_crawl b_ns b_new b_sb < <(run_path builtin \
  "{$URL,$CFG,\"browser_config\":{\"type\":\"BrowserConfig\",\"params\":{\"browser_mode\":\"builtin\",\"viewport_width\":$W}}}")
default_ok=$(path_ok "$d_crawl" "$d_ns" "$d_new" "$d_sb")
builtin_ok=$(path_ok "$b_crawl" "$b_ns" "$b_new" "$b_sb")

started=$(docker inspect -f '{{.State.StartedAt}}' "$(dc ps -q crawl4ai)" 2>/dev/null || true)
logs=$(dc logs --no-color ${started:+--since "$started"} crawl4ai 2>&1 || true)
addon_active=$(grep -qF '[research-engine sandbox add-on] active' <<< "$logs" && echo true || echo false)
addon_warnings=$(grep -cF '[research-engine sandbox add-on] WARNING' <<< "$logs" || true)

no_sandbox=$(( (d_ns < 0 || b_ns < 0) ? -1 : d_ns + b_ns ))
crawl_ok=$([ "$d_crawl" = true ] && [ "$b_crawl" = true ] && echo true || echo false)
sandbox=off
if [ "$default_ok" = true ] && [ "$builtin_ok" = true ] && [ "$addon_active" = true ] &&
  [ "$addon_warnings" -eq 0 ] && [ "$no_sandbox" -eq 0 ]; then
  sandbox=on
fi
echo "{\"sandbox\":\"$sandbox\",\"no_sandbox_procs\":$no_sandbox,\"default_ok\":$default_ok,\"default_new_renderers\":$d_new,\"builtin_ok\":$builtin_ok,\"builtin_new_renderers\":$b_new,\"addon_active\":$addon_active,\"addon_warnings\":$addon_warnings,\"crawl_ok\":$crawl_ok}"
[ "$sandbox" = on ] && [ "$crawl_ok" = true ]
