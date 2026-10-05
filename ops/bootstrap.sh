#!/usr/bin/env bash
# Idempotent VM setup (V1-22, §9, §10). Run by the owner: it asks no questions itself, but its one
# sudo step (installing the backup timer) may prompt for the owner's sudo password.
# Usage: ops/bootstrap.sh [--dry-run]
#
# 1. checks docker + docker compose, and that the repo is owned by the current user
# 2. creates .env from .env.example with generated secrets (mode 600; an existing .env is kept)
# 3. creates $BACKUP_DIR (mode 700)
# 4. syncs deploy/caddy/ into $CADDY_DIR, copying only changed files and never deleting anything
#    there; $CADDY_DIR/.env is never copied over. Its SITE_HOST is set from the repo .env (the
#    single source) - but never the .env.example placeholder (a .env just created from the
#    example, or one never edited) over a different existing value, on any run. LAB_SUBNET (and the optional TOOLBOX_HOST) come from the environment only when
#    the Caddy env lacks them. Existing values are never changed, and no value is ever printed.
# 5. starts Caddy (`docker compose up -d --wait` in $CADDY_DIR, which creates the `proxy` network).
#    The Caddyfile is a single-file bind mount: if it (or compose.yaml or .env) changed, the
#    container is recreated; if only sites/ changed, Caddy reloads.
# 6. installs and enables the nightly backup timer (the script's only sudo use, which may prompt
#    for the password; skipped, without sudo, when the installed units already match and the
#    timer is enabled and active). The unit runs `make` at its resolved path (MAKE_BIN overrides).
# --dry-run validates everything and prints the planned actions; it changes nothing, runs no
# sudo and no state-changing docker command.
CMD=bootstrap
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DRY=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=true ;;
    *) fail 2 bootstrap "usage: ops/bootstrap.sh [--dry-run]" ;;
  esac
done

SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
CADDY_SRC="$DEPLOY_DIR/deploy/caddy"
UNIT_SRC="$DEPLOY_DIR/deploy/systemd"
EXAMPLE="$DEPLOY_DIR/.env.example"
UNITS=(research-engine-backup.service research-engine-backup.timer)
TIMER=research-engine-backup.timer
SECRET_NAMES=(API_KEY SESSION_SECRET SEARXNG_SECRET CRAWL4AI_API_TOKEN)
RUN_USER="$(id -un)"

ACTIONS=()
act() { ACTIONS+=("$(json_out "$@")"); } # act step=... action=... [key=value | key:=json ...]
actions_json() {
  local IFS=,
  printf '[%s]' "${ACTIONS[*]}"
}
json_list() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@"; }
cdc() { (cd "$CADDY_DIR" && docker compose "$@"); }
die() { fail "$1" bootstrap "$2" dry_run:="$DRY" actions:="$(actions_json)"; }

TMP=""
on_exit_hook() { if [ -n "$TMP" ]; then rm -rf -- "$TMP"; fi; }

# ---- 1. preflight ----------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die 2 "docker is not installed"
docker compose version >/dev/null 2>&1 || die 2 "docker compose is not available"
[ "$(stat -c %u "$REPO_DIR")" = "$(id -u)" ] || die 2 "$REPO_DIR is not owned by $RUN_USER"
# REPO_DIR and the user land in systemd unit lines: no whitespace and no `%` (a specifier there).
[[ "$REPO_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] ||
  die 2 "REPO_DIR must be an absolute path of letters, digits and . _ / - only (systemd units)"
[[ "$RUN_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die 2 "user name $RUN_USER is not usable in a systemd unit"
MAKE_BIN="${MAKE_BIN:-$(command -v make || true)}"
[ -n "$MAKE_BIN" ] && [ -x "$MAKE_BIN" ] && [ ! -d "$MAKE_BIN" ] ||
  die 2 "make is not installed (the backup timer runs make backup)"
[[ "$MAKE_BIN" =~ ^/[A-Za-z0-9._/-]+$ ]] || die 2 "make must be at an absolute path without spaces or %"
command -v systemctl >/dev/null 2>&1 || die 2 "systemctl is not available"

# ---- plan (validates every precondition before anything changes) ------------------------------
if [ -e "$ENV_FILE" ]; then
  env_action=keep
  site="$(env_get SITE_HOST "$ENV_FILE" || true)"
else
  [ -f "$EXAMPLE" ] || die 2 "$EXAMPLE is missing"
  command -v openssl >/dev/null 2>&1 || die 2 "openssl is needed to generate secrets"
  env_action=create
  site="$(env_get SITE_HOST "$EXAMPLE" || true)"
fi
[ -n "$site" ] || die 2 "SITE_HOST is empty: set it in $ENV_FILE"

if [ -d "$BACKUP_DIR" ]; then backups_action=keep; else backups_action=create; fi

[ -d "$CADDY_SRC" ] || die 2 "$CADDY_SRC is missing"
if [ -d "$CADDY_DIR" ]; then
  [ -w "$CADDY_DIR" ] || die 2 "$CADDY_DIR is not writable by $RUN_USER"
else
  [ -w "$(dirname "$CADDY_DIR")" ] ||
    die 2 "$CADDY_DIR does not exist: create it for $RUN_USER first (sudo install -d -o $RUN_USER $CADDY_DIR)"
fi
# Changed files: in deploy/caddy/ but absent or different in $CADDY_DIR (any .env excluded).
mapfile -t changed < <(
  cd "$CADDY_SRC" && find . -type f ! -name .env -printf '%P\n' | LC_ALL=C sort |
    while IFS= read -r rel; do
      if [ ! -f "$CADDY_DIR/$rel" ] || ! cmp -s "$rel" "$CADDY_DIR/$rel"; then printf '%s\n' "$rel"; fi
    done
)
if [ "${#changed[@]}" -gt 0 ]; then files_action=update; else files_action=keep; fi

cenv="$CADDY_DIR/.env"
cenv_keys=()
if [ ! -e "$cenv" ]; then
  [ -n "${LAB_SUBNET:-}" ] || die 2 "$cenv is missing: set LAB_SUBNET (one or more space-separated CIDRs of the lab clients; optionally TOOLBOX_HOST) in the environment and re-run"
  cenv_action=create
  cenv_keys=(SITE_HOST LAB_SUBNET)
  [ -z "${TOOLBOX_HOST:-}" ] || cenv_keys+=(TOOLBOX_HOST)
else
  [ -r "$cenv" ] || die 2 "$cenv is not readable by $RUN_USER"
  site_note=""
  caddy_site="$(env_get SITE_HOST "$cenv" || true)"
  placeholder="$(env_get SITE_HOST "$EXAMPLE" 2>/dev/null || true)"
  if [ "$caddy_site" != "$site" ]; then
    if [ "$env_action" = create ] || { [ -n "$placeholder" ] && [ "$site" = "$placeholder" ]; }; then
      # The repo .env still holds the .env.example placeholder (just created, or never edited):
      # on any run, it never replaces a different, real Caddy SITE_HOST (nor recreates Caddy).
      site_note="SITE_HOST not changed: the repo .env still has the .env.example placeholder; set SITE_HOST in $ENV_FILE and re-run"
    else
      cenv_keys+=(SITE_HOST)
    fi
  fi
  if [ -z "$(env_get LAB_SUBNET "$cenv" || true)" ]; then
    [ -n "${LAB_SUBNET:-}" ] || die 2 "LAB_SUBNET is empty in $cenv (Caddy would refuse every client): set LAB_SUBNET in the environment and re-run"
    cenv_keys+=(LAB_SUBNET)
  fi
  if [ -n "${TOOLBOX_HOST:-}" ] && [ -z "$(env_get TOOLBOX_HOST "$cenv" || true)" ]; then
    cenv_keys+=(TOOLBOX_HOST)
  fi
  if [ "${#cenv_keys[@]}" -gt 0 ]; then cenv_action=update; else cenv_action=keep; fi
fi
for v in "${LAB_SUBNET:-}" "${TOOLBOX_HOST:-}" "$site"; do
  [[ "$v" != *$'\n'* && "$v" != *$'\r'* ]] || die 2 "LAB_SUBNET, TOOLBOX_HOST and SITE_HOST must be single-line values"
done

caddy_running=false
if [ -f "$CADDY_DIR/compose.yaml" ] && [ -n "$(cdc ps -q caddy 2>/dev/null || true)" ]; then
  caddy_running=true
fi
recreate=false reload=false
for rel in "${changed[@]}"; do
  case "$rel" in
    Caddyfile | compose.yaml) recreate=true ;;
    sites/*) reload=true ;;
  esac
done
[ "$cenv_action" = keep ] || recreate=true # the container env is read only at creation
if [ "$caddy_running" != true ]; then
  caddy_action=up # a fresh start reads every file
elif [ "$recreate" = true ]; then
  caddy_action=recreate
elif [ "$reload" = true ]; then
  caddy_action=reload
else
  caddy_action=up # no change: `up -d --wait` is a no-op that confirms Caddy and `proxy`
fi

render_unit() { # render_unit name: the unit with REPO_DIR and the user substituted, on stdout
  python3 - "$UNIT_SRC/$1" "$REPO_DIR" "$RUN_USER" "$MAKE_BIN" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read()
text = text.replace("@REPO_DIR@", sys.argv[2]).replace("@USER@", sys.argv[3])
sys.stdout.write(text.replace("@MAKE@", sys.argv[4]))
PY
}
units_changed=()
for u in "${UNITS[@]}"; do
  [ -f "$UNIT_SRC/$u" ] || die 2 "$UNIT_SRC/$u is missing"
  if [ ! -f "$SYSTEMD_DIR/$u" ] || ! cmp -s <(render_unit "$u") "$SYSTEMD_DIR/$u"; then
    units_changed+=("$u")
  fi
done
timer_on=false
if systemctl is-enabled --quiet "$TIMER" 2>/dev/null && systemctl is-active --quiet "$TIMER" 2>/dev/null; then
  timer_on=true
fi
if [ "${#units_changed[@]}" -gt 0 ]; then
  units_action=install
elif [ "$timer_on" != true ]; then
  units_action=enable
else
  units_action=keep
fi
if [ "$units_action" != keep ] && [ "$DRY" != true ]; then
  command -v sudo >/dev/null 2>&1 || die 2 "sudo is needed to install the systemd units"
fi

# ---- apply -----------------------------------------------------------------------------------
if [ "$DRY" = true ]; then
  log "dry run: nothing is changed"
fi

# 2. .env
if [ "$env_action" = create ] && [ "$DRY" != true ]; then
  log "creating .env from .env.example with generated secrets (mode 600)"
  # Values reach python only through its environment (exported by the shell builtin: never on
  # any command line, never on stdout or stderr).
  (
    for n in "${SECRET_NAMES[@]}"; do
      s="$(openssl rand -hex 32)" && [ "${#s}" -eq 64 ] || exit 1
      export "GEN_$n=$s"
    done
    exec python3 - "$EXAMPLE" "$ENV_FILE" "${SECRET_NAMES[@]}" <<'PY'
import os, re, sys
example, dest, names = sys.argv[1], sys.argv[2], sys.argv[3:]
vals = {n: os.environ["GEN_" + n] for n in names}
out, seen = [], set()
for line in open(example, encoding="utf-8"):
    m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    if m and m.group(1) in vals:
        out.append(f"{m.group(1)}={vals[m.group(1)]}\n")
        seen.add(m.group(1))
    else:
        out.append(line if line.endswith("\n") else line + "\n")
out += [f"{n}={vals[n]}\n" for n in names if n not in seen]
tmp = f"{dest}.bootstrap-tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
    os.link(tmp, dest)  # complete file or nothing; fails rather than overwrite
finally:
    os.unlink(tmp)
PY
  ) || die 1 "could not create $ENV_FILE"
fi
act step=env action="$env_action"
[ "$env_action" = keep ] || [ "$DRY" = true ] || log "edit SITE_HOST in $ENV_FILE if it is still the example value"

# 3. backups dir
if [ "$backups_action" = create ] && [ "$DRY" != true ]; then
  (umask 077 && mkdir -p "$BACKUP_DIR") || die 1 "could not create $BACKUP_DIR"
fi
act step=backup_dir action="$backups_action"

# 4. Caddy files and env
if [ "$files_action" = update ] && [ "$DRY" != true ]; then
  log "updating $CADDY_DIR: ${changed[*]}"
  (
    umask 022
    for rel in "${changed[@]}"; do
      # errexit is off inside a `|| die` subshell: every step checks its own status
      mkdir -p "$(dirname "$CADDY_DIR/$rel")" || exit 1
      cp --preserve=mode "$CADDY_SRC/$rel" "$CADDY_DIR/$rel.bootstrap-tmp" || exit 1
      mv -f "$CADDY_DIR/$rel.bootstrap-tmp" "$CADDY_DIR/$rel" || exit 1 # atomic per file
    done
  ) || die 1 "could not update $CADDY_DIR"
fi
act step=caddy_files action="$files_action" changed:="$(json_list "${changed[@]}")"

if [ "$cenv_action" != keep ] && [ "$DRY" != true ]; then
  log "$cenv_action $cenv: ${cenv_keys[*]} (values not shown)"
  BOOT_SITE_HOST="$site" python3 - "$cenv" "${cenv_keys[@]}" <<'PY' ||
import os, re, sys
path, keys = sys.argv[1], sys.argv[2:]
vals = {"SITE_HOST": os.environ["BOOT_SITE_HOST"], "LAB_SUBNET": os.environ.get("LAB_SUBNET", ""),
        "TOOLBOX_HOST": os.environ.get("TOOLBOX_HOST", "")}
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    lines = ["# Shared Caddy env, written by research-engine ops/bootstrap.sh. Keep mode 600."]
out, done = [], set()
for line in lines:
    m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    if m and m.group(1) in keys:
        if m.group(1) not in done:  # first occurrence replaced in place, duplicates dropped
            out.append(f"{m.group(1)}={vals[m.group(1)]}")
            done.add(m.group(1))
        continue
    out.append(line)
out += [f"{k}={vals[k]}" for k in keys if k not in done]
tmp = f"{path}.bootstrap-tmp"
if os.path.lexists(tmp):
    os.unlink(tmp)
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
os.replace(tmp, path)
PY
    die 1 "could not write $cenv"
fi
if [ -n "${site_note:-}" ]; then
  act step=caddy_env action="$cenv_action" keys:="$(json_list "${cenv_keys[@]}")" site_host_note="$site_note"
else
  act step=caddy_env action="$cenv_action" keys:="$(json_list "${cenv_keys[@]}")"
fi

# 5. Caddy up / recreate / reload
if [ "$DRY" != true ]; then
  case "$caddy_action" in
    recreate)
      log "Caddyfile, compose.yaml or .env changed: recreating Caddy"
      cdc up -d --wait --force-recreate >&2 || die 1 "docker compose up --force-recreate failed in $CADDY_DIR"
      ;;
    *)
      log "starting Caddy (no-op when it is already up)"
      cdc up -d --wait >&2 || die 1 "docker compose up failed in $CADDY_DIR"
      if [ "$caddy_action" = reload ]; then
        log "sites/ changed: reloading Caddy"
        cdc exec -T caddy caddy reload --config /etc/caddy/Caddyfile >&2 || die 1 "caddy reload failed"
      fi
      ;;
  esac
fi
act step=caddy action="$caddy_action" running:="$caddy_running"

# 6. systemd units (the only sudo use)
if [ "$units_action" != keep ] && [ "$DRY" != true ]; then
  TMP="$(mktemp -d)"
  for u in "${units_changed[@]}"; do
    render_unit "$u" >"$TMP/$u"
    log "installing $SYSTEMD_DIR/$u (sudo)"
    sudo install -m 644 "$TMP/$u" "$SYSTEMD_DIR/$u" >&2 || die 1 "sudo install of $u failed"
  done
  sudo systemctl daemon-reload >&2 || die 1 "systemctl daemon-reload failed"
  sudo systemctl enable --now "$TIMER" >&2 || die 1 "systemctl enable --now $TIMER failed"
fi
act step=systemd action="$units_action" units:="$(json_list "${units_changed[@]}")"

json_out ok:=true command=bootstrap dry_run:="$DRY" actions:="$(actions_json)"
