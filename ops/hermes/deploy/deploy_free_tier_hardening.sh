#!/usr/bin/env bash
# deploy_free_tier_hardening.sh - roll out the free-tier router hardening ON THE VPS (run with sudo).
#
# Ships: router.py + providers.free-tier.yaml -> /opt/hermes-llm-router, gtm_api.py -> the hermes-mcp module dir.
# Idempotent: every file is compared by sha256; identical files are skipped; if nothing changed the script
# exits 0 without touching or restarting anything.
#
# USAGE (on the VPS, from the directory holding the uploaded files, or with SRC_DIR=...)
#   sudo bash deploy_free_tier_hardening.sh [--dry-run] [--use-free-tier] [--restart-mcp] [--force-gtm]
#                                           [--allow-chaos] [-h|--help]
#
#   --dry-run        run the read-only pre-flight (stages + tests in a temp dir) and print every action; change nothing
#   --use-free-tier  also write the systemd drop-in 10-free-tier.conf (ROUTER_CONFIG -> providers.free-tier.yaml)
#   --restart-mcp    restart hermes-mcp.service if gtm_api.py changed (otherwise the command is only printed)
#   --force-gtm      install gtm_api.py even though the deployed consultation.py does not reference it
#   --allow-chaos    proceed although ROUTER_ENABLE_CHAOS=1 is active (drill mode on is normally a mistake)
#
# FROM A DEV MACHINE (host alias "vps" = port 2222, user deploy, your key - see deploy/README.md):
#   ssh vps 'mkdir -p /tmp/hermes-deploy/tests'
#   scp ops/hermes/llm-router/router.py ops/hermes/llm-router/providers.free-tier.yaml \
#       ops/hermes/gtm_api.py ops/hermes/deploy/deploy_free_tier_hardening.sh vps:/tmp/hermes-deploy/
#   scp ops/hermes/llm-router/tests/test_router.py vps:/tmp/hermes-deploy/tests/
#   ssh -t vps 'sudo bash /tmp/hermes-deploy/deploy_free_tier_hardening.sh --dry-run'
#   ssh -t vps 'sudo bash /tmp/hermes-deploy/deploy_free_tier_hardening.sh --use-free-tier'
#
# Inputs (env): SRC_DIR (default: this script's directory) must contain router.py, providers.free-tier.yaml,
#   gtm_api.py and optionally tests/test_router.py. HERMES_MCP_DIR (default /home/deploy/hermes_mcp, taken from
#   integration/README.md "cp ... ~/hermes_mcp/" with user deploy; ops/hermes/README.md does not pin it [VERIFY]).
#   Other overrides: ROUTER_DIR, ROUTER_SERVICE, HERMES_MCP_SERVICE, ROUTER_URL, BACKUP_ROOT, HEALTH_WAIT_S.
#
# SECURITY NOTE: /tmp/hermes-deploy is writable by the deploy user, which is also the Hermes agent's user. The script
#   therefore snapshots the inputs into a root-only temp dir first and checks/installs ONLY the snapshot (symlinks
#   refused), and prints each snapshot sha256 so you can compare with `sha256sum` on your dev machine. Upload and run
#   promptly. The router API key is read from router.env, never printed, never on a command line.
#
# DOWNTIME (honest): the router is one uvicorn worker on a fixed loopback port, so a restart is a gap of roughly 2 s
#   (RestartSec=2 + startup). It is NOT zero-downtime. Hermes's native break-glass fallback (Gemini direct, see
#   llm-router/README.md section 1) answers during the gap. The restart happens only if router files or the
#   drop-in actually changed.
#
# EXIT CODES
#   0  success, nothing to do, or --dry-run completed
#   2  usage error
#   3  pre-flight failed (nothing on the VPS was changed)
#   4  refused: not root, another run holds the lock, or ROUTER_ENABLE_CHAOS=1 without --allow-chaos
#   5  deployment failed, automatic rollback restored the old files and the old service is healthy again
#   6  deployment failed AND the rollback could not be verified - manual intervention needed (backup path printed)
#
# MANUAL ROLLBACK (B = the backup dir the script printed; assumes 10-free-tier.conf did not exist before this run):
#   B=/var/backups/hermes-free-tier-hardening/<UTC-ts>; sudo cp -p "$B"/router/router.py "$B"/router/providers.free-tier.yaml \
#     /opt/hermes-llm-router/ && sudo rm -f /etc/systemd/system/hermes-llm-router.service.d/10-free-tier.conf && \
#     sudo systemctl daemon-reload && sudo systemctl restart hermes-llm-router
#   gtm_api.py: sudo cp -p "$B"/mcp/gtm_api.py "$HERMES_MCP_DIR"/ && sudo systemctl restart hermes-mcp.service
#   (if the backup lacks a file, it did not exist before: delete the new one instead). state.json is never
#   restored automatically: the router tolerates old and new state files; copy it back only if you need to.

set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SELF")"

# ─────────────────────────────── configuration ───────────────────────────────
SRC_DIR="${SRC_DIR:-$SCRIPT_DIR}"
ROUTER_DIR="${ROUTER_DIR:-/opt/hermes-llm-router}"                 # hermes-llm-router.service header
ROUTER_SERVICE="${ROUTER_SERVICE:-hermes-llm-router.service}"
ROUTER_ENV_FILE="${ROUTER_ENV_FILE:-/etc/hermes-llm-router/router.env}"   # root:root 0600
ROUTER_URL="${ROUTER_URL:-http://127.0.0.1:47311}"                 # loopback only, port from the unit
VENV_PY="${VENV_PY:-$ROUTER_DIR/venv/bin/python}"
# DynamicUser + StateDirectory=hermes-llm-router: /var/lib/hermes-llm-router is a symlink into /var/lib/private.
STATE_FILE_CANDIDATES=(/var/lib/private/hermes-llm-router/state.json /var/lib/hermes-llm-router/state.json)
HERMES_MCP_DIR="${HERMES_MCP_DIR:-/home/deploy/hermes_mcp}"        # [VERIFY] from integration/README.md (~/hermes_mcp, user deploy)
HERMES_MCP_SERVICE="${HERMES_MCP_SERVICE:-hermes-mcp.service}"     # ops/hermes/README.md Install step 5
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/hermes-free-tier-hardening}"
LOCKFILE="${LOCKFILE:-/run/lock/hermes-free-tier-hardening.lock}"
HEALTH_WAIT_S="${HEALTH_WAIT_S:-20}"
DROPIN_DIR="/etc/systemd/system/${ROUTER_SERVICE}.d"
DROPIN_FILE="$DROPIN_DIR/10-free-tier.conf"
FREE_TIER_CFG="$ROUTER_DIR/providers.free-tier.yaml"
PLACEHOLDER_BUDGET=18                                              # providers.free-tier.yaml placeholder RPD

DRY_RUN=0; USE_FREE_TIER=0; RESTART_MCP=0; FORCE_GTM=0; ALLOW_CHAOS=0

# ─────────────────────────────── helpers ───────────────────────────────
say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
log()  { printf '   %s\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { local code="$1"; shift; printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit "$code"; }
dry()  { printf '   [dry-run] %s\n' "$*"; }

usage() { awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$SELF"; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --use-free-tier) USE_FREE_TIER=1 ;;
    --restart-mcp) RESTART_MCP=1 ;;
    --force-gtm) FORCE_GTM=1 ;;
    --allow-chaos) ALLOW_CHAOS=1 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done

# Re-exec via sudo when not root. Variables are passed explicitly (sudo -E may be refused by sudoers).
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || die 4 "must run as root and sudo is not available"
  pass=()
  for v in SRC_DIR ROUTER_DIR ROUTER_SERVICE ROUTER_ENV_FILE ROUTER_URL VENV_PY HERMES_MCP_DIR HERMES_MCP_SERVICE \
           BACKUP_ROOT LOCKFILE HEALTH_WAIT_S; do
    pass+=("$v=${!v}")
  done
  args=()
  (( DRY_RUN ))       && args+=(--dry-run)
  (( USE_FREE_TIER )) && args+=(--use-free-tier)
  (( RESTART_MCP ))   && args+=(--restart-mcp)
  (( FORCE_GTM ))     && args+=(--force-gtm)
  (( ALLOW_CHAOS ))   && args+=(--allow-chaos)
  echo "not root - re-executing via sudo"
  exec sudo env "${pass[@]}" bash "$SELF" "${args[@]+"${args[@]}"}"
fi

SRC_DIR="$(readlink -f "$SRC_DIR")"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMP=""
PHASE=none                  # none | router | mcp - decides what the EXIT trap rolls back
ROUTER_RESTARTED=0; MCP_RESTARTED=0
INSTALLED_ROUTER=(); INSTALLED_MCP=()
declare -A RB=()            # destination path -> backed-up copy ("" when the file did not exist before)
BK=""
ROUTER_KEY=""
LAST_CODE=000

sha() { sha256sum "$1" | awk '{print $1}'; }
differs() { [[ ! -f "$2" ]] || [[ "$(sha "$1")" != "$(sha "$2")" ]]; }   # src dest

# Install through a temp name + rename so a reader never sees a half-written file.
atomic_install() {   # mode owner group src dest
  local mode=$1 owner=$2 group=$3 src=$4 dest=$5 tmp
  tmp="${dest}.new.$$"
  install -m "$mode" -o "$owner" -g "$group" "$src" "$tmp"
  mv -f "$tmp" "$dest"
}

active_env()    { systemctl show -p Environment "$ROUTER_SERVICE" 2>/dev/null | sed 's/^Environment=//' | tr -d '"' | tr ' ' '\n'; }
active_config() { active_env | sed -n 's/^ROUTER_CONFIG=//p' | tail -n1; }

load_router_key() {
  [[ -f "$ROUTER_ENV_FILE" ]] || die 3 "$ROUTER_ENV_FILE not found"
  ROUTER_KEY="$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?ROUTER_API_KEY=//p' "$ROUTER_ENV_FILE" | tail -n1 | tr -d '\r')"
  ROUTER_KEY="${ROUTER_KEY#\"}"; ROUTER_KEY="${ROUTER_KEY%\"}"; ROUTER_KEY="${ROUTER_KEY#\'}"; ROUTER_KEY="${ROUTER_KEY%\'}"
  (( ${#ROUTER_KEY} >= 24 )) || die 3 "ROUTER_API_KEY missing or shorter than 24 chars in $ROUTER_ENV_FILE"
}

# GET /healthz with the key supplied as a curl config on stdin (printf is a builtin: nothing shows up in ps).
healthz_fetch() {    # outfile -> prints the HTTP code
  local out=$1 k code
  k="${ROUTER_KEY//\\/\\\\}"; k="${k//\"/\\\"}"
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$k" \
          | curl -sS -K - -o "$out" -w '%{http_code}' --max-time 3 "$ROUTER_URL/healthz" 2>/dev/null)" || code=000
  printf '%s' "$code"
}

wait_healthz() {     # outfile -> 0 once HTTP 200 + valid JSON within HEALTH_WAIT_S
  local out=$1 deadline=$((SECONDS + HEALTH_WAIT_S)) code=000
  while :; do
    code="$(healthz_fetch "$out")"
    if [[ $code == 200 ]] && jq -e . "$out" >/dev/null 2>&1; then LAST_CODE=200; return 0; fi
    (( SECONDS >= deadline )) && break
    sleep 1
  done
  LAST_CODE=$code; return 1
}

# Strict verification of the NEW code. Prints problems, returns 1 if any.
verify_health() {    # healthz-json expected-config-basename
  local f=$1 expect=$2 rc=0 bad missing cfg
  if ! jq -e '(.providers | type == "array") and (.providers | length > 0)' "$f" >/dev/null 2>&1; then
    echo "   - /healthz has no providers array"; return 1
  fi
  bad="$(jq -r '.providers[] | select(.key_present != true) | .name' "$f" | paste -sd, -)"
  [[ -z $bad ]] || { echo "   - key_present is not true for: $bad (check $ROUTER_ENV_FILE)"; rc=1; }
  missing="$(jq -r '.providers[] | select((has("requests_today") and has("daily_request_budget")) | not) | .name' "$f" | paste -sd, -)"
  [[ -z $missing ]] || { echo "   - requests_today/daily_request_budget missing for: $missing (new code is NOT live)"; rc=1; }
  cfg="$(jq -r '.config // ""' "$f")"
  [[ $cfg == "$expect" ]] || { echo "   - config is '$cfg', expected '$expect'"; rc=1; }
  return $rc
}

print_table() {      # healthz-json
  local f=$1
  printf '   %-20s %14s %-14s %14s %8s\n' name cooling_down_s cooldown_kind requests_today budget
  jq -r '.providers[] | [.name, .cooling_down_s, (.cooldown_kind // ""), .requests_today, .daily_request_budget] | @tsv' "$f" \
    | awk -F'\t' '{printf "   %-20s %14s %-14s %14s %8s\n", $1, $2, $3, $4, $5}'
  printf '   config: %s\n' "$(jq -r '.config' "$f")"
}

# Restore one destination from RB (or delete it if it did not exist before).
restore_one() {      # dest
  local d=$1 b="${RB[$1]-}"
  if [[ -n $b && -e $b ]]; then cp -p "$b" "$d.rb.$$" && mv -f "$d.rb.$$" "$d" && echo "   restored $d"
  else rm -f "$d" && echo "   removed $d (did not exist before)"; fi
}

rollback_router() {
  set +e
  local d reload=0 out="$TMP/rb-healthz.json"
  warn "rolling back router changes"
  for d in ${INSTALLED_ROUTER[@]+"${INSTALLED_ROUTER[@]}"}; do
    restore_one "$d"; [[ $d == "$DROPIN_FILE" ]] && reload=1
  done
  [[ -d $DROPIN_DIR ]] && rmdir --ignore-fail-on-non-empty "$DROPIN_DIR" 2>/dev/null
  (( reload )) && systemctl daemon-reload
  if (( ROUTER_RESTARTED )); then
    systemctl restart "$ROUTER_SERVICE"
    if systemctl is-active --quiet "$ROUTER_SERVICE" && wait_healthz "$out"; then
      echo "   rollback verified: old router is up (healthz 200)"
      echo "   backup kept at: $BK"
      return 0
    fi
    echo "   rollback NOT verified: healthz=${LAST_CODE:-?}, unit state: $(systemctl is-active "$ROUTER_SERVICE")"
    echo "   inspect: journalctl -u $ROUTER_SERVICE -n 50 --no-pager -o cat   |   backup: $BK"
    return 1
  fi
  echo "   router was not restarted yet; files restored, running service untouched"
  return 0
}

rollback_mcp() {
  set +e
  local d
  warn "rolling back gtm_api.py"
  for d in ${INSTALLED_MCP[@]+"${INSTALLED_MCP[@]}"}; do restore_one "$d"; done
  if (( MCP_RESTARTED )); then
    systemctl restart "$HERMES_MCP_SERVICE"; sleep 3
    if systemctl is-active --quiet "$HERMES_MCP_SERVICE"; then echo "   rollback verified: $HERMES_MCP_SERVICE active"; return 0; fi
    echo "   rollback NOT verified: $HERMES_MCP_SERVICE is $(systemctl is-active "$HERMES_MCP_SERVICE")  (backup: $BK)"
    return 1
  fi
  return 0
}

on_exit() {
  local rc=$?
  set +e
  trap - EXIT
  if (( rc != 0 )); then
    case "$PHASE" in
      router) PHASE=none; if rollback_router; then rc=5; else rc=6; fi ;;
      mcp)    PHASE=none; if rollback_mcp;    then rc=5; else rc=6; fi ;;
    esac
    (( rc == 5 )) && printf '\n\033[31mDEPLOY FAILED - automatically rolled back (exit 5). Backup: %s\033[0m\n' "$BK" >&2
    (( rc == 6 )) && printf '\n\033[31mDEPLOY FAILED and ROLLBACK UNVERIFIED (exit 6). Backup: %s\033[0m\n' "$BK" >&2
  fi
  [[ -n $TMP && -d $TMP ]] && rm -rf "$TMP"
  exit "$rc"
}
trap on_exit EXIT

# ─────────────────────────────── lock ───────────────────────────────
mkdir -p "$(dirname "$LOCKFILE")"
exec 9>"$LOCKFILE"
flock -n 9 || die 4 "another run holds $LOCKFILE"

TMP="$(mktemp -d /tmp/hermes-hardening.XXXXXX)"; chmod 700 "$TMP"
STAGE="$TMP/stage"; mkdir -p "$STAGE/tests"

(( DRY_RUN )) && say "DRY RUN - read-only checks run for real, nothing on the VPS is changed" || say "Free-tier hardening deploy ($TS)"

# ─────────────────────────────── pre-flight ───────────────────────────────
say "Pre-flight"
for t in sha256sum install flock systemctl python3 curl jq awk sed mktemp; do
  command -v "$t" >/dev/null 2>&1 || die 3 "required tool missing: $t$([[ $t == jq ]] && echo ' (apt-get install jq; this script never installs packages)')"
done
[[ -x "$VENV_PY" ]] || die 3 "router venv python not found: $VENV_PY"
[[ -d "$ROUTER_DIR" ]] || die 3 "$ROUTER_DIR not found"
[[ "$(systemctl show -p LoadState --value "$ROUTER_SERVICE" 2>/dev/null)" == loaded ]] || die 3 "$ROUTER_SERVICE is not installed"
load_router_key

# Snapshot the inputs into the root-only stage dir; everything below uses the snapshot only.
for f in router.py providers.free-tier.yaml gtm_api.py tests/test_router.py; do
  s="$SRC_DIR/$f"
  if [[ -e $s || -L $s ]]; then
    [[ -L $s ]] && die 3 "refusing symlink: $s"
    [[ -f $s ]] || die 3 "not a regular file: $s"
    cp "$s" "$STAGE/$f"
  fi
done
[[ -f "$STAGE/router.py" ]]                 || die 3 "missing $SRC_DIR/router.py"
[[ -f "$STAGE/providers.free-tier.yaml" ]]  || die 3 "missing $SRC_DIR/providers.free-tier.yaml"
HAVE_GTM=0; [[ -f "$STAGE/gtm_api.py" ]] && HAVE_GTM=1
HAVE_TESTS=0; [[ -f "$STAGE/tests/test_router.py" ]] && HAVE_TESTS=1
(( HAVE_GTM )) || warn "gtm_api.py not found in $SRC_DIR - GTM part will be skipped"
for f in router.py providers.free-tier.yaml gtm_api.py tests/test_router.py; do
  [[ -f "$STAGE/$f" ]] && log "staged $f  sha256=$(sha "$STAGE/$f" | cut -c1-16)..."
done

# Refuse drill mode (unit/drop-ins via systemctl show, plus router.env).
chaos=0
env_now="$(active_env)"        # captured first: `active_env | grep -q` would trip pipefail via SIGPIPE
grep -qx 'ROUTER_ENABLE_CHAOS=1' <<<"$env_now" && chaos=1
grep -Eq "^[[:space:]]*(export[[:space:]]+)?ROUTER_ENABLE_CHAOS=[\"']?1" "$ROUTER_ENV_FILE" && chaos=1
if (( chaos )); then
  (( ALLOW_CHAOS )) && warn "ROUTER_ENABLE_CHAOS=1 is active - continuing because of --allow-chaos" \
    || die 4 "ROUTER_ENABLE_CHAOS=1 is active (drill mode). Disable it (systemctl revert $ROUTER_SERVICE) or pass --allow-chaos"
fi

# Syntax
"$VENV_PY" -B -c 'import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)' \
  "$STAGE/router.py" "$TMP/router.pyc" || die 3 "router.py does not compile (venv python)"
log "router.py compiles with the router venv python"
if (( HAVE_GTM )); then
  python3 -B -c 'import py_compile, sys; py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)' \
    "$STAGE/gtm_api.py" "$TMP/gtm_api.pyc" || die 3 "gtm_api.py does not compile (system python3)"
  log "gtm_api.py compiles with system python3 ($(python3 --version 2>&1))"
fi

# YAML must parse AND load into the NEW router's Provider dataclass (run from the stage dir so `import router` is the new code).
if ! yaml_out="$(cd "$STAGE" && env -u ROUTER_API_KEY -u ROUTER_CONFIG "$VENV_PY" -B - "$STAGE/providers.free-tier.yaml" "$PLACEHOLDER_BUDGET" <<'PY'
import os, sys, yaml, router
assert os.path.dirname(os.path.abspath(router.__file__)) == os.getcwd(), "imported the wrong router module"
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
provs = [router.Provider(**p) for p in cfg["providers"]]
assert provs, "no providers in yaml"
print("OK %d providers: %s" % (len(provs), ", ".join(p.name for p in provs)))
for p in provs:
    if p.daily_request_budget == int(sys.argv[2]):
        print("PLACEHOLDER " + p.name)
PY
)"; then
  die 3 "providers.free-tier.yaml failed to parse/load into router.Provider (see error above)"
fi
log "$(grep '^OK ' <<<"$yaml_out")"
while read -r _ pname; do
  warn "provider '$pname' still has daily_request_budget: $PLACEHOLDER_BUDGET (PLACEHOLDER) - set the real AI Studio RPD (minus ~10%) before relying on it"
done < <(grep '^PLACEHOLDER ' <<<"$yaml_out" || true)

# Test suite against the staged copy
if (( HAVE_TESTS )); then
  if "$VENV_PY" -c 'import pytest' 2>/dev/null; then
    log "running test_router.py against the staged router.py"
    (cd "$STAGE" && env -u ROUTER_API_KEY -u ROUTER_CONFIG -u ROUTER_ENABLE_CHAOS "$VENV_PY" -B -m pytest tests -q -p no:cacheprovider) \
      || die 3 "test suite failed on the staged copy - nothing was deployed"
  else
    warn "pytest is not importable in $VENV_PY - tests NOT run (this script never pip-installs)"
  fi
else
  log "no tests/test_router.py supplied - skipping the suite"
fi

# ─────────────────────────────── what would change ───────────────────────────────
say "Comparing with what is deployed"
CUR_CFG="$(active_config || true)"
TARGET_CFG_NAME="$(basename "${CUR_CFG:-providers.yaml}")"
log "active ROUTER_CONFIG now: ${CUR_CFG:-<not set; router default providers.yaml>}"

CH_ROUTER=0; CH_YAML=0; CH_DROPIN=0; CH_GTM=0
differs "$STAGE/router.py" "$ROUTER_DIR/router.py" && CH_ROUTER=1
differs "$STAGE/providers.free-tier.yaml" "$FREE_TIER_CFG" && CH_YAML=1
(( CH_ROUTER )) && log "router.py: CHANGED" || log "router.py: identical, skipping"
(( CH_YAML ))   && log "providers.free-tier.yaml: CHANGED" || log "providers.free-tier.yaml: identical, skipping"

# Drop-in (opt-in)
if (( USE_FREE_TIER )); then
  printf '[Service]\nEnvironment=ROUTER_CONFIG=%s\n' "$FREE_TIER_CFG" > "$TMP/10-free-tier.conf"
  if [[ "$CUR_CFG" == "$FREE_TIER_CFG" && ! -f $DROPIN_FILE ]]; then
    log "free-tier profile already active (set in the unit itself) - no drop-in needed"
  elif differs "$TMP/10-free-tier.conf" "$DROPIN_FILE"; then
    CH_DROPIN=1; log "drop-in 10-free-tier.conf: will be written"
  else
    log "drop-in 10-free-tier.conf: identical, skipping"
    [[ "$CUR_CFG" == "$FREE_TIER_CFG" ]] || warn "drop-in is present but ROUTER_CONFIG is '$CUR_CFG' - a higher-precedence override (e.g. override.conf) wins; fix with 'systemctl cat $ROUTER_SERVICE'"
  fi
  TARGET_CFG_NAME="providers.free-tier.yaml"
else
  if [[ "$CUR_CFG" == "$FREE_TIER_CFG" ]]; then log "free-tier profile is the ACTIVE config"
  else
    log "new providers.free-tier.yaml is installed but INACTIVE (active: ${CUR_CFG:-providers.yaml}); pass --use-free-tier to switch"
  fi
fi

# gtm_api.py decision
GTM_OWNER=""; GTM_GROUP=""
if (( HAVE_GTM )); then
  if [[ ! -d $HERMES_MCP_DIR ]]; then
    warn "HERMES_MCP_DIR=$HERMES_MCP_DIR does not exist - skipping gtm_api.py (set HERMES_MCP_DIR)"
  else
    ref="$HERMES_MCP_DIR/consultation.py"
    if [[ -f $ref ]]; then GTM_OWNER="$(stat -c %u "$ref")"; GTM_GROUP="$(stat -c %g "$ref")"
    else GTM_OWNER="$(stat -c %u "$HERMES_MCP_DIR")"; GTM_GROUP="$(stat -c %g "$HERMES_MCP_DIR")"
         warn "$ref not found - using the directory's owner for gtm_api.py"; fi
    if [[ -f $ref ]] && ! grep -q 'gtm_api' "$ref"; then
      if (( FORCE_GTM )); then warn "deployed consultation.py does not reference gtm_api - installing anyway (--force-gtm); it stays unused until the matching consultation.py is deployed"
      else warn "deployed consultation.py does not reference gtm_api: the new module would be UNUSED until the matching consultation.py is deployed - skipping gtm_api.py (use --force-gtm to install anyway)"
           HAVE_GTM=0; fi
    elif [[ ! -f $ref ]] && (( ! FORCE_GTM )); then
      warn "cannot confirm consultation.py references gtm_api (file missing) - skipping gtm_api.py (use --force-gtm)"; HAVE_GTM=0
    fi
    if (( HAVE_GTM )); then
      differs "$STAGE/gtm_api.py" "$HERMES_MCP_DIR/gtm_api.py" && CH_GTM=1
      (( CH_GTM )) && log "gtm_api.py: CHANGED (owner ${GTM_OWNER}:${GTM_GROUP})" || log "gtm_api.py: identical, skipping"
    fi
  fi
fi

if (( ! CH_ROUTER && ! CH_YAML && ! CH_DROPIN && ! CH_GTM )); then
  say "Nothing to do"
  echo "All requested files are already identical to the deployed ones. No backup taken, nothing restarted."
  exit 0
fi
NEED_ROUTER_RESTART=0; (( CH_ROUTER || CH_YAML || CH_DROPIN )) && NEED_ROUTER_RESTART=1
# Note: a YAML change only matters at runtime if it is the active config, but a restart is harmless and keeps state simple.

# ─────────────────────────────── backup ───────────────────────────────
say "Backup"
BK="$BACKUP_ROOT/$TS"
if (( DRY_RUN )); then
  dry "mkdir -m 700 $BK; copy router.py, providers*.yaml, unit + drop-ins (+ systemctl cat), state.json, gtm_api.py, consultation.py"
  dry "router.env: record sha256 only (never copied)"
else
  install -d -m 700 "$BACKUP_ROOT"; install -d -m 700 "$BK" "$BK/router" "$BK/systemd" "$BK/mcp"
  for f in "$ROUTER_DIR/router.py" "$ROUTER_DIR"/providers*.yaml; do
    [[ -f $f ]] && { cp -p "$f" "$BK/router/"; RB["$f"]="$BK/router/$(basename "$f")"; }
  done
  for s in "${STATE_FILE_CANDIDATES[@]}"; do
    [[ -f $s ]] && { cp -p "$s" "$BK/router/state.json"; log "state.json backed up from $s"; break; }
  done
  systemctl cat "$ROUTER_SERVICE" > "$BK/systemd/systemctl-cat.txt" 2>&1 || true
  [[ -f "/etc/systemd/system/$ROUTER_SERVICE" ]] && cp -p "/etc/systemd/system/$ROUTER_SERVICE" "$BK/systemd/"
  if [[ -d $DROPIN_DIR ]]; then
    cp -a "$DROPIN_DIR" "$BK/systemd/"
    [[ -f $DROPIN_FILE ]] && RB["$DROPIN_FILE"]="$BK/systemd/$(basename "$DROPIN_DIR")/10-free-tier.conf"
  fi
  if [[ -d $HERMES_MCP_DIR ]]; then
    for f in gtm_api.py consultation.py; do
      [[ -f "$HERMES_MCP_DIR/$f" ]] && { cp -p "$HERMES_MCP_DIR/$f" "$BK/mcp/"; RB["$HERMES_MCP_DIR/$f"]="$BK/mcp/$f"; }
    done
  fi
  echo "router.env sha256: $(sha "$ROUTER_ENV_FILE")  (content deliberately NOT backed up)" > "$BK/router.env.sha256"
  chmod -R go-rwx "$BK"
  echo "   backup: $BK"
fi

# ─────────────────────────────── deploy: router ───────────────────────────────
if (( NEED_ROUTER_RESTART )); then
  say "Deploy router"
  if (( DRY_RUN )); then
    (( CH_ROUTER )) && dry "install -m 644 -o root -g root router.py -> $ROUTER_DIR/router.py"
    (( CH_YAML ))   && dry "install -m 644 -o root -g root providers.free-tier.yaml -> $FREE_TIER_CFG"
    (( CH_DROPIN )) && dry "write $DROPIN_FILE (Environment=ROUTER_CONFIG=$FREE_TIER_CFG); systemctl daemon-reload"
    dry "systemctl restart $ROUTER_SERVICE   (~2 s gap; Hermes break-glass fallback covers it)"
    dry "poll $ROUTER_URL/healthz up to ${HEALTH_WAIT_S}s; jq-verify key_present, requests_today, daily_request_budget, config=$TARGET_CFG_NAME"
    dry "on any failure: restore backed-up files + drop-in state, daemon-reload, restart, re-check healthz, exit 5"
  else
    PHASE=router
    if (( CH_ROUTER )); then atomic_install 644 root root "$STAGE/router.py" "$ROUTER_DIR/router.py"; INSTALLED_ROUTER+=("$ROUTER_DIR/router.py"); log "installed router.py"; fi
    if (( CH_YAML ));   then atomic_install 644 root root "$STAGE/providers.free-tier.yaml" "$FREE_TIER_CFG"; INSTALLED_ROUTER+=("$FREE_TIER_CFG"); log "installed providers.free-tier.yaml"; fi
    if (( CH_DROPIN )); then
      install -d -m 755 -o root -g root "$DROPIN_DIR"
      atomic_install 644 root root "$TMP/10-free-tier.conf" "$DROPIN_FILE"; INSTALLED_ROUTER+=("$DROPIN_FILE"); log "wrote $DROPIN_FILE"
      systemctl daemon-reload
      now_cfg="$(active_config || true)"
      [[ "$now_cfg" == "$FREE_TIER_CFG" ]] || die 1 "after daemon-reload ROUTER_CONFIG is '$now_cfg', not $FREE_TIER_CFG (another drop-in overrides it) - rolling back"
    fi

    say "Restart $ROUTER_SERVICE and verify"
    log "single uvicorn worker on a fixed port: expect ~2 s without a router; Hermes falls back to Gemini direct meanwhile"
    ROUTER_RESTARTED=1
    systemctl restart "$ROUTER_SERVICE" || die 1 "systemctl restart $ROUTER_SERVICE failed"
    wait_healthz "$TMP/healthz.json" || die 1 "/healthz did not return HTTP 200 within ${HEALTH_WAIT_S}s (last code: $LAST_CODE; unit: $(systemctl is-active "$ROUTER_SERVICE"))"
    if ! vout="$(verify_health "$TMP/healthz.json" "$TARGET_CFG_NAME")"; then
      echo "$vout"; die 1 "health verification failed"
    fi
    echo "   HTTP 200, all providers key_present, new fields present (new code is live)"
    print_table "$TMP/healthz.json"
    PHASE=none
  fi
else
  say "Router"
  log "no router file or drop-in changed - router not restarted"
fi

# ─────────────────────────────── deploy: gtm_api / hermes-mcp ───────────────────────────────
if (( CH_GTM )); then
  say "Deploy gtm_api.py"
  if (( DRY_RUN )); then
    dry "install -m 644 -o $GTM_OWNER -g $GTM_GROUP gtm_api.py -> $HERMES_MCP_DIR/gtm_api.py (owner/group copied from consultation.py)"
    if (( RESTART_MCP )); then dry "systemctl restart $HERMES_MCP_SERVICE; require is-active (else restore gtm_api.py and restart)"
    else dry "would print: sudo systemctl restart $HERMES_MCP_SERVICE"; fi
  else
    PHASE=mcp
    atomic_install 644 "$GTM_OWNER" "$GTM_GROUP" "$STAGE/gtm_api.py" "$HERMES_MCP_DIR/gtm_api.py"; INSTALLED_MCP+=("$HERMES_MCP_DIR/gtm_api.py")
    log "installed gtm_api.py (owner ${GTM_OWNER}:${GTM_GROUP})"
    if (( RESTART_MCP )); then
      MCP_RESTARTED=1
      systemctl restart "$HERMES_MCP_SERVICE" || die 1 "systemctl restart $HERMES_MCP_SERVICE failed"
      deadline=$((SECONDS + 15))
      until systemctl is-active --quiet "$HERMES_MCP_SERVICE"; do
        (( SECONDS < deadline )) || die 1 "$HERMES_MCP_SERVICE is not active after restart"
        sleep 1
      done
      sleep 3   # catch a crash loop that briefly reports active
      systemctl is-active --quiet "$HERMES_MCP_SERVICE" || die 1 "$HERMES_MCP_SERVICE died right after restart"
      log "$HERMES_MCP_SERVICE active after restart"
    else
      log "hermes-mcp NOT restarted (no --restart-mcp). The new module loads on the next restart:"
      log "    sudo systemctl restart $HERMES_MCP_SERVICE"
    fi
    PHASE=none
  fi
fi

# ─────────────────────────────── summary ───────────────────────────────
say "Done"
if (( DRY_RUN )); then
  echo "Dry run complete - nothing was changed."
else
  echo "Backup  : $BK"
  echo "Rollback: B=$BK; sudo cp -p \"\$B\"/router/router.py \"\$B\"/router/providers.free-tier.yaml $ROUTER_DIR/ && sudo systemctl daemon-reload && sudo systemctl restart $ROUTER_SERVICE"
  (( CH_DROPIN )) && echo "          (also: sudo rm -f $DROPIN_FILE before the daemon-reload)"
  echo "Verify  : journalctl -u $ROUTER_SERVICE -n 5 --no-pager -o cat"
fi
exit 0
