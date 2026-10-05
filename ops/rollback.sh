#!/usr/bin/env bash
# Redeploy a previous good commit (V1-22, §10). Approval required (make rollback [SHA=<sha>]).
# Target: $SHA if given, else the newest ok entry's `to` in deploys.jsonl that differs from the
# current deploy. Creates branch rollback/<UTC-ts> at it (no work is lost), a worktree, repoints
# .deploy/current, then up --wait, smoke and sandbox, and appends the rollback entry. Never resets
# or checks out the main working tree. Exit 0 ok, 1 failed, 2 precondition (bad SHA, no target).
CMD=rollback
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
. "$(dirname "${BASH_SOURCE[0]}")/_deploy_lib.sh"
require_cmd git
require_cmd docker
take_lock

if [ -L "$DEPLOY_ROOT/current" ]; then
  CURRENT="$(git -C "$DEPLOY_ROOT/current" rev-parse --short HEAD 2>/dev/null || echo none)"
else
  CURRENT="$(last_good_sha)"
fi

if [ -n "${SHA:-}" ]; then
  [[ "$SHA" =~ ^[0-9a-f]{4,40}$ ]] || fail 2 rollback "SHA must be a hex commit id"
  g rev-parse --verify --quiet "$SHA^{commit}" >/dev/null || fail 2 rollback "unknown commit"
  TARGET="$SHA"
else
  TARGET="$(last_good_sha "$CURRENT")"
  [ "$TARGET" != none ] || fail 2 rollback "no previous good deploy to roll back to" \
    current="$CURRENT"
  g rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null ||
    fail 2 rollback "last good commit is not in this repository" target="$TARGET"
fi

if rollback_to "$TARGET" "$CURRENT"; then
  json_out ok:=true command=rollback from="$CURRENT" to="$ROLLBACK_TO" \
    branch="$ROLLBACK_BRANCH" smoke="$SMOKE" sandbox="$SANDBOX"
  exit 0
fi
json_out ok:=false command=rollback error="rollback failed" from="$CURRENT" to="$ROLLBACK_TO" \
  branch="$ROLLBACK_BRANCH" smoke="$SMOKE" sandbox="$SANDBOX" detail:="$DETAIL"
exit 1
