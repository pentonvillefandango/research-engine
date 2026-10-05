# ADR-0019: Claude Code operates the live stack through scripted ops commands

## Status

Accepted (2026-10-04). Source: D19.

## Context

The owner uses Claude Code on `toolbox` both to build and to operate the service.

## Decision

Every operator action is a scripted, idempotent, non-interactive command with a final JSON line and meaningful exit codes. `deploy`, `rollback`, `restore` and `bootstrap` need the owner's approval each time; the project's `.claude/settings.json` makes them prompt. Destructive commands (`down -v`, deleting volumes, force-pushing) are denied. Membership of the `docker` group is effectively root on the VM, and this is accepted for the home lab.

## Consequences

Operations are safe to verify for an agent on the same host it develops on. The permission rules are part of the repo.

## Amendment (2026-10-05)

The owner gave Claude Code a standing approval for `make deploy`, `make rollback` and `make restore`; Claude reports every run. `make bootstrap` is run by the owner, because it uses `sudo`, and sudo's own prompt gates it, so `.claude/settings.json` no longer has an ask rule for it. The deny rules for destructive commands are unchanged. See `docs/OPERATIONS.md` (Guardrails).
