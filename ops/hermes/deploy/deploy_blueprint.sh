#!/usr/bin/env bash
# deploy_blueprint.sh — push the Hermes blueprint (ops/hermes/home) to the VPS.
#
# Run from the repo root on YOUR machine (WSL or Git Bash), where your SSH key lives:
#
#   HERMES_HOST=<vps-ip> bash ops/hermes/deploy/deploy_blueprint.sh            # deploy
#   HERMES_HOST=<vps-ip> bash ops/hermes/deploy/deploy_blueprint.sh --dry-run  # show plan only
#   HERMES_HOST=<vps-ip> bash ops/hermes/deploy/deploy_blueprint.sh --rollback <backup.tgz>
#
# What it does:
#   1. preflight (local files, SSH, remote dirs, service exists)
#   2. backs up every path it will touch → ~/hermes-backups/blueprint-<ts>.tgz (+ manifest)
#   3. uploads to a staging dir and self-tests the linter ON THE VPS before installing
#   4. installs: SOUL.md (replace), MEMORY.md (merge — see below), memories/*.md
#      (never overwrites live ledgers), skills/gtm-container-linter (replace)
#   5. restarts the service, waits for "active", shows status + recent logs
#   6. verifies deployed checksums; prints the rollback command
#
# MEMORY.md policy (--memory):
#   merge (default) first deploy: blueprint + the old MEMORY.md preserved under a heading.
#                   later deploys: live file kept (Hermes may have written facts);
#                   the new version lands as MEMORY.blueprint.md for a manual diff.
#   replace         overwrite (old copy is in the backup tarball)
#   keep            never touch MEMORY.md; write MEMORY.blueprint.md
#
# Env overrides: HERMES_SSH_PORT (2222) HERMES_SSH_USER (deploy) HERMES_SSH_KEY (~/.ssh/id_ed25519)
#                HERMES_SERVICE (hermes-mcp.service) HERMES_REMOTE_HOME (/home/deploy/.hermes)

set -euo pipefail

HOST="${HERMES_HOST:-}"
PORT="${HERMES_SSH_PORT:-2222}"
SSH_USER="${HERMES_SSH_USER:-deploy}"
KEY="${HERMES_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SERVICE="${HERMES_SERVICE:-hermes-mcp.service}"
REMOTE_HOME="${HERMES_REMOTE_HOME:-/home/deploy/.hermes}"
SRC="ops/hermes/home"
PATHS=(SOUL.md MEMORY.md memories/client-ledger.md memories/gtm-patterns.md memories/incident-log.md skills/gtm-container-linter)

MODE=deploy
MEMORY_MODE=merge
RESTART=1
ROLLBACK_FILE=""

usage() { sed -n '2,30p' "$0"; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) MODE=dry-run ;;
    --rollback) MODE=rollback; ROLLBACK_FILE="${2:?--rollback needs a backup path}"; shift ;;
    --memory) MEMORY_MODE="${2:?--memory needs merge|replace|keep}"; shift ;;
    --no-restart) RESTART=0 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done
[[ -n "$HOST" ]] || { echo "Set HERMES_HOST=<vps-ip>" >&2; exit 2; }
[[ "$MEMORY_MODE" =~ ^(merge|replace|keep)$ ]] || { echo "--memory must be merge|replace|keep" >&2; exit 2; }

SSH=(ssh -p "$PORT" -i "$KEY" -o ConnectTimeout=10 -o ServerAliveInterval=15 "$SSH_USER@$HOST")
SSH_TTY=(ssh -t -p "$PORT" -i "$KEY" -o ConnectTimeout=10 "$SSH_USER@$HOST")
TS="$(date -u +%Y%m%dT%H%M%SZ)"
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

restart_and_check() {
  say "Restarting $SERVICE"
  # -t: lets sudo ask for a password if deploy's sudo is not passwordless
  "${SSH_TTY[@]}" "sudo systemctl restart '$SERVICE'" || die "restart command failed"
  local state=""
  for _ in $(seq 1 15); do
    state="$("${SSH[@]}" "systemctl is-active '$SERVICE'" 2>/dev/null || true)"
    [[ "$state" == "active" ]] && break
    sleep 2
  done
  "${SSH[@]}" "systemctl status '$SERVICE' --no-pager -n 0 | head -12; echo; journalctl -u '$SERVICE' -n 30 --no-pager 2>/dev/null || sudo -n journalctl -u '$SERVICE' -n 30 --no-pager 2>/dev/null || true"
  [[ "$state" == "active" ]] || return 1
}

# ─────────────────────────────── rollback ───────────────────────────────
if [[ "$MODE" == rollback ]]; then
  say "Rolling back from $ROLLBACK_FILE"
  "${SSH[@]}" "bash -s" -- "$REMOTE_HOME" "$ROLLBACK_FILE" <<'REMOTE'
set -euo pipefail
HOME_DIR="$1"; BK="$2"; MANIFEST="${BK%.tgz}.manifest"
[[ -f "$BK" && -f "$MANIFEST" ]] || { echo "backup or manifest missing: $BK / $MANIFEST" >&2; exit 1; }
cd "$HOME_DIR"
while read -r existed path; do
  [[ -z "$path" ]] && continue
  rm -rf -- "$path"                       # remove what the deploy installed
  echo "removed  $path (existed before: $existed)"
done < "$MANIFEST"
tar -xzf "$BK" -C "$HOME_DIR"             # restore what existed before
rm -f MEMORY.blueprint.md memories/*.blueprint
echo "restored from $BK"
REMOTE
  if [[ $RESTART == 1 ]]; then restart_and_check || die "$SERVICE is not active after rollback"; fi
  say "Rollback complete"
  exit 0
fi

# ─────────────────────────────── preflight ──────────────────────────────
say "Local preflight"
[[ -f "$SRC/SOUL.md" && -d "$SRC/skills/gtm-container-linter" ]] || die "run from the repo root (missing $SRC)"
[[ -f "$KEY" ]] || die "SSH key not found: $KEY"
# Find a Python that actually runs. On Windows, `python3` is often the Microsoft Store
# placeholder: it exists on PATH but only prints "Python was not found".
PY=()
for candidate in python3 python "py -3"; do
  read -r -a cmd <<< "$candidate"
  if command -v "${cmd[0]}" >/dev/null 2>&1 \
     && "${cmd[@]}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    PY=("${cmd[@]}"); break
  fi
done
if [[ ${#PY[@]} -gt 0 ]]; then
  "${PY[@]}" "$SRC/skills/gtm-container-linter/scripts/lint_container.py" \
    "$SRC/skills/gtm-container-linter/tests/fixtures/sample_container.json" --fail-on never >/dev/null \
    && echo "linter runs locally (${PY[*]}): ok" || die "linter failed locally"
else
  echo "note: no working local Python — skipping the local check (the VPS self-test still gates the install)"
fi
for p in "${PATHS[@]}"; do [[ -e "$SRC/$p" ]] || die "missing $SRC/$p"; done
echo "files: ${PATHS[*]}"

say "Remote preflight ($SSH_USER@$HOST:$PORT)"
"${SSH[@]}" "bash -s" -- "$REMOTE_HOME" "$SERVICE" <<'REMOTE' || die "remote preflight failed"
set -euo pipefail
HOME_DIR="$1"; SERVICE="$2"
[[ -d "$HOME_DIR" ]] || { echo "no $HOME_DIR" >&2; exit 1; }
systemctl cat "$SERVICE" >/dev/null 2>&1 || { echo "service $SERVICE not found" >&2; exit 1; }
echo "hermes home : $HOME_DIR ($(du -sh "$HOME_DIR" 2>/dev/null | cut -f1))"
echo "service     : $SERVICE ($(systemctl is-active "$SERVICE" || true))"
echo "python3     : $(python3 --version 2>&1)"
echo "disk free   : $(df -h "$HOME_DIR" | awk 'NR==2{print $4}')"
for p in SOUL.md MEMORY.md memories skills/gtm-container-linter; do
  if [[ -e "$HOME_DIR/$p" ]]; then echo "exists      : $p ($(du -sh "$HOME_DIR/$p" | cut -f1))"; else echo "new         : $p"; fi
done
REMOTE

if [[ "$MODE" == dry-run ]]; then
  say "Dry run — nothing changed. MEMORY.md policy would be: $MEMORY_MODE"
  exit 0
fi

# ─────────────────────────────── backup ─────────────────────────────────
say "Backing up paths that will change"
BACKUP="$("${SSH[@]}" "bash -s" -- "$REMOTE_HOME" "$TS" "${PATHS[@]}" <<'REMOTE'
set -euo pipefail
HOME_DIR="$1"; TS="$2"; shift 2
mkdir -p "$HOME/hermes-backups"; chmod 700 "$HOME/hermes-backups"
BK="$HOME/hermes-backups/blueprint-$TS.tgz"; MANIFEST="${BK%.tgz}.manifest"
cd "$HOME_DIR"
existing=()
: > "$MANIFEST"
for p in "$@"; do
  if [[ -e "$p" ]]; then existing+=("$p"); echo "yes $p" >> "$MANIFEST"; else echo "no $p" >> "$MANIFEST"; fi
done
if [[ ${#existing[@]} -gt 0 ]]; then tar -czf "$BK" "${existing[@]}"; else tar -czf "$BK" --files-from /dev/null; fi
chmod 600 "$BK" "$MANIFEST"
echo "$BK"
REMOTE
)"
BACKUP="$(echo "$BACKUP" | tail -1)"
echo "backup: $BACKUP"

# ─────────────────────────────── upload ─────────────────────────────────
STAGE="$REMOTE_HOME/.blueprint-staging-$TS"
say "Uploading to staging ($STAGE)"
tar -C "$SRC" --exclude='__pycache__' --exclude='.pytest_cache' -czf - "${PATHS[@]}" \
  | "${SSH[@]}" "mkdir -p '$STAGE' && tar -xzf - -C '$STAGE'" || die "upload failed"

# ─────────────────────────────── install ────────────────────────────────
say "Self-test on VPS, then install"
"${SSH[@]}" "bash -s" -- "$REMOTE_HOME" "$STAGE" "$MEMORY_MODE" "$TS" <<'REMOTE' || die "install failed (nothing restarted; staging left for inspection)"
set -euo pipefail
HOME_DIR="$1"; STAGE="$2"; MEMORY_MODE="$3"; TS="$4"
LINTER="$STAGE/skills/gtm-container-linter"

# 1) linter must run with the VPS python and catch the planted defects
python3 "$LINTER/scripts/lint_container.py" "$LINTER/tests/fixtures/sample_container.json" --fail-on never \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); c=r["counts"]; assert c["critical"]==1 and c["high"]>=8, c; print("linter self-test: ok", c)'
if python3 -c 'import pytest' 2>/dev/null; then
  (cd "$LINTER" && python3 -m pytest tests -q -p no:cacheprovider 2>&1 | tail -1)
fi

cd "$HOME_DIR"
mkdir -p memories skills

# 2) SOUL.md — replace (backed up)
install -m 644 "$STAGE/SOUL.md" SOUL.md && echo "installed SOUL.md ($(wc -c < SOUL.md) bytes)"

# 3) MEMORY.md
NEW="$STAGE/MEMORY.md"
if [[ "$MEMORY_MODE" == replace || ! -s MEMORY.md ]]; then
  install -m 644 "$NEW" MEMORY.md && echo "installed MEMORY.md"
elif [[ "$MEMORY_MODE" == keep ]] || head -1 MEMORY.md | grep -q '^# MEMORY — Hot Index'; then
  install -m 644 "$NEW" MEMORY.blueprint.md
  echo "kept live MEMORY.md; new version at MEMORY.blueprint.md (diff it by hand)"
else
  { cat "$NEW"; printf '\n---\n\n## Preserved pre-blueprint memory (%s)\n\n' "$TS"; cat MEMORY.md; } > MEMORY.md.tmp
  mv MEMORY.md.tmp MEMORY.md; chmod 644 MEMORY.md
  echo "merged MEMORY.md (blueprint + previous content preserved below it)"
fi
SIZE=$(wc -c < MEMORY.md)
[[ $SIZE -gt 3500 ]] && echo "WARNING: MEMORY.md is $SIZE bytes (> 3.5 KB budget) — trim the preserved section"

# 4) memories/ — never overwrite live ledgers
for f in client-ledger.md gtm-patterns.md incident-log.md; do
  if [[ -e "memories/$f" ]] && ! cmp -s "$STAGE/memories/$f" "memories/$f"; then
    install -m 644 "$STAGE/memories/$f" "memories/$f.blueprint"
    echo "kept live memories/$f; new version at memories/$f.blueprint"
  else
    install -m 644 "$STAGE/memories/$f" "memories/$f" && echo "installed memories/$f"
  fi
done

# 5) skill — replace atomically (old copy is in the backup)
rm -rf "skills/.gtm-container-linter.old"
[[ -d skills/gtm-container-linter ]] && mv skills/gtm-container-linter skills/.gtm-container-linter.old
mv "$LINTER" skills/gtm-container-linter
rm -rf "skills/.gtm-container-linter.old"
find skills/gtm-container-linter -type d -exec chmod 755 {} + ; find skills/gtm-container-linter -type f -exec chmod 644 {} +
echo "installed skills/gtm-container-linter"

# 6) ownership: only the paths we touched (uploaded as deploy, so usually a no-op)
sudo -n chown -R "$(id -un):$(id -gn)" SOUL.md MEMORY.md memories skills/gtm-container-linter 2>/dev/null \
  || echo "note: passwordless sudo unavailable for chown — files are already owned by $(id -un)"
rm -rf "$STAGE"
REMOTE

# ─────────────────────────────── restart ────────────────────────────────
if [[ $RESTART == 1 ]]; then
  restart_and_check || die "$SERVICE not active after restart. Roll back with:
  HERMES_HOST=$HOST bash $0 --rollback $BACKUP"
fi

# ─────────────────────────────── verify ─────────────────────────────────
say "Verifying deployed files"
LOCAL_SUMS="$(cd "$SRC" && find SOUL.md skills/gtm-container-linter -type f ! -path '*__pycache__*' -print0 | sort -z | xargs -0 sha256sum)"
REMOTE_SUMS="$("${SSH[@]}" "cd '$REMOTE_HOME' && find SOUL.md skills/gtm-container-linter -type f ! -path '*__pycache__*' -print0 | sort -z | xargs -0 sha256sum")"
if [[ "$LOCAL_SUMS" == "$REMOTE_SUMS" ]]; then echo "checksums: SOUL.md + skill match the repo"; else
  diff <(echo "$LOCAL_SUMS") <(echo "$REMOTE_SUMS") || true; die "checksum mismatch"; fi
"${SSH[@]}" "cd '$REMOTE_HOME' && ls -la SOUL.md MEMORY.md MEMORY.blueprint.md memories/ skills/gtm-container-linter/ 2>/dev/null; echo; ss -ltn 2>/dev/null | grep -E ':58775\b' || echo 'note: nothing listening on :58775'"

say "Done"
echo "Backup : $BACKUP"
echo "Undo   : HERMES_HOST=$HOST bash $0 --rollback $BACKUP"
