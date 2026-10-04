# ADR-0024: Git workflow and deploy-from-worktree

## Status

Accepted (2026-10-04). Source: B5.

## Context

The repo is public. Each build step pauses for the owner's approval, and the live stack must always run a committed version.

## Decision

Step 0 (repo hygiene) commits straight to `main`. Steps 1–9 go on the `v1` branch, with a push after each approved step and CI on every push, then one PR `v1`→`main` and the tag `v1.0.0`. `make deploy` refuses a dirty tree and builds from a git worktree of the target commit under `.deploy/`. Rollbacks create a `rollback/<ts>` branch at the good commit, and nothing in the main working tree is reset.

## Consequences

The live stack equals the commit exactly, and no work is lost on rollback. `.deploy/` holds up to three old worktrees and is git-ignored.
