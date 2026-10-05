#!/usr/bin/env bash
# Deploy exactly the committed HEAD (V1-22, §10). Approval required (make deploy).
# Worktree .deploy/<short> -> .deploy/current -> `up --build --wait` -> smoke -> sandbox (ADR-0022:
# every deploy recreates crawl4ai). On any failure: record it, then roll back automatically to the
# last good commit; with none, leave the failed stack up for diagnosis. Every step is appended to
# deploys.jsonl. Final stdout line: one JSON object. Exit 0 ok, 1 failed (rolled back or not),
# 2 precondition (dirty tree, lock held).
CMD=deploy
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/_deploy_lib.sh"
require_cmd git
require_cmd docker

[ -z "$(g status --porcelain)" ] || fail 2 deploy "working tree has uncommitted changes"
take_lock

TARGET="$(g rev-parse HEAD)"
SHORT="$(short_sha "$TARGET")"
FROM="$(last_good_sha)"
from_extra=()
if [ "$FROM" = none ]; then
  # First worktree deploy: name the running version (informational only; it has no worktree, so
  # it is never a rollback target).
  running="$(app_get_or_empty /version | python3 -c '
import json, re, sys
try:
    s = json.load(sys.stdin)["data"]["git_sha"]
except Exception:
    s = ""
print(s if isinstance(s, str) and re.fullmatch(r"[0-9a-f]{4,40}", s) else "")')"
  if [ -n "$running" ]; then
    FROM="$running"
    from_extra=(from_source=running)
  fi
fi
PREV_GOOD="$(last_good_sha "$TARGET")" # rollback target: never the sha that just failed

log "deploying $SHORT (from $FROM)"
activate_worktree "$TARGET"

if up_and_check "$SHORT"; then
  record deploy "$FROM" "$SHORT" ok "$SMOKE" sandbox="$SANDBOX" "${from_extra[@]}"
  prune_worktrees
  prune_images
  json_out ok:=true command=deploy from="$FROM" to="$SHORT" smoke="$SMOKE" sandbox="$SANDBOX"
  exit 0
fi

log "deploy of $SHORT failed (up=$UP smoke=$SMOKE sandbox=$SANDBOX)"
record deploy "$FROM" "$SHORT" failed "$SMOKE" sandbox="$SANDBOX" detail:="$DETAIL" \
  "${from_extra[@]}"
failed=(command=deploy from="$FROM" to="$SHORT" smoke="$SMOKE" sandbox="$SANDBOX"
  detail:="$DETAIL" error="deploy failed")

if [ "$PREV_GOOD" = none ]; then
  log "no previous good deploy: leaving the failed stack up for diagnosis"
  json_out ok:=false "${failed[@]}" rolled_back_to:=null
  exit 1
fi

rb_ok=true
rollback_to "$PREV_GOOD" "$SHORT" || rb_ok=false
json_out ok:=false "${failed[@]}" rolled_back_to="$ROLLBACK_TO" rollback_ok:=$rb_ok \
  rollback_branch="$ROLLBACK_BRANCH"
exit 1
