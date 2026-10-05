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

CURRENT="$(current_sha)"
[ "$CURRENT" != none ] || CURRENT="$(last_good_sha)"

if [ -n "${SHA:-}" ]; then
  [[ "$SHA" =~ ^[0-9a-f]{4,40}$ ]] || fail 2 rollback "SHA must be a hex commit id"
  full="$(resolve_commit "$SHA")" || fail 2 rollback "unknown commit"
  TARGET="$(short_sha "$full")" # normalised: a 7-char SHA=67374da works too
else
  TARGET="$(last_good_sha "$CURRENT")"
  if [ "$TARGET" = none ]; then
    hint="$(seeded_running_sha)"
    if [ -n "$hint" ]; then
      fail 2 rollback "no previous good deploy to roll back to; the version running before the \
first worktree deploy was $hint: make rollback SHA=$hint" current="$CURRENT" suggested_sha="$hint"
    fi
    fail 2 rollback "no previous good deploy to roll back to" current="$CURRENT"
  fi
  resolve_commit "$TARGET" >/dev/null ||
    fail 2 rollback "last good commit is not in this repository" target="$TARGET"
fi

if rollback_to "$TARGET" "$CURRENT"; then
  json_out ok:=true command=rollback from="$CURRENT" to="$ROLLBACK_TO" \
    branch="$ROLLBACK_BRANCH" smoke="$SMOKE" sandbox="$SANDBOX"
  exit 0
fi
json_out ok:=false command=rollback error="rollback failed: $ROLLBACK_ERROR" from="$CURRENT" \
  to="$ROLLBACK_TO" switched:=$ROLLBACK_ACTIVATED current="$(current_sha)" \
  branch="$ROLLBACK_BRANCH" smoke="$SMOKE" sandbox="$SANDBOX" detail:="$DETAIL"
exit 1
