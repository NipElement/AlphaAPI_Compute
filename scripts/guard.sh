#!/usr/bin/env bash
# ============================================================================
# guard.sh — WAIVER-2026-08-11-001 compensating control C-5 / C-8 / C-9
#
# This host is a SHARED development box carrying 735 GB of production MongoDB
# data and live business code. There is NO EBS snapshot rollback point.
# Every dangerous stage MUST be bracketed by this guard.
#
#   ./scripts/guard.sh baseline   capture protected-asset fingerprint
#   ./scripts/guard.sh check      assert critical assets intact; assert disk
#
# Exit codes: 0 ok | 2 disk stop-line | 3 CRITICAL asset changed | 4 setup err
#
# ---------------------------------------------------------------------------
# TWO TIERS, AND WHY
# ---------------------------------------------------------------------------
# CRITICAL — things this lab could actually destroy and that are irreplaceable
#   (no snapshot exists). Any change here is a HARD STOP. These paths are
#   quiet by nature: mongod is stopped, backups are static. A change means
#   something went wrong.
#
# OBSERVED — directories where humans are actively working right now. The lab
#   never writes to them, but colleagues do, constantly. Changes here are
#   REPORTED into the evidence trail and do NOT block.
#
# Putting an active working directory in the hard-stop tier was the original
# design error (see evidence/<run_id>/security/guard-incident-2026-08-11.md):
# it fired because someone edited an unrelated project. A guard that blocks on
# unrelated activity gets bypassed, and a bypassed guard protects nothing. The
# safety property that actually matters — "the lab cannot silently damage the
# database or the backups" — is preserved in full by the CRITICAL tier.
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$(cat "$REPO/.run_id" 2>/dev/null || echo UNKNOWN)"
GUARD_DIR="$REPO/evidence/$RUN_ID/security"
BASELINE="$GUARD_DIR/guard-baseline.txt"

# --- Waiver W-3: absolute free-space thresholds replace percentage gates -----
STOP_FREE_GIB=40
WARN_FREE_GIB=60
STOP_INODE_PCT=85

# --- CRITICAL: change here is a hard stop -----------------------------------
CRITICAL_DIRS=(
  /var/lib/mongodb
  /var/log/mongodb
  /mnt/backup
  /mnt/backup2
)

# --- OBSERVED: reported, never blocking -------------------------------------
OBSERVED_DIRS=(
  /home/ubuntu/final_release
  /home/ubuntu/yuansheng
  /home/ubuntu/hengxin
  /home/ubuntu/xueguang
  /home/ubuntu/data_online
  /home/ubuntu/mongo_debug_data
)

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

# Cheap, deterministic fingerprint of a directory's CONTENTS.
#
# `-A` (almost-all), NOT `-a`: `-a` emits the `..` entry whose mtime belongs to
# the PARENT directory, so every path under /home/ubuntu would change whenever
# anything touched /home/ubuntu itself.
#
# The lab's own repository subtree is excluded — we write there by design, and
# a guard that reacts to its own work is measuring nothing.
fingerprint_dir() {
  local d="$1" exclude=""
  [[ -e "$d" ]] || { echo "ABSENT"; return; }
  if [[ "$REPO/" == "$d/"* ]]; then
    exclude="${REPO#"$d"/}"; exclude="${exclude%%/*}"
  fi
  if [[ -n "$exclude" ]]; then
    sudo -n ls -lA --time-style=+%s "$d" 2>/dev/null \
      | grep -v " ${exclude}\$" | sha256sum | cut -d' ' -f1
  else
    sudo -n ls -lA --time-style=+%s "$d" 2>/dev/null | sha256sum | cut -d' ' -f1
  fi
}

collect() {
  echo "# guard baseline  run_id=$RUN_ID  captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# repo subtree excluded from fingerprints: $REPO"
  for d in "${CRITICAL_DIRS[@]}"; do
    printf 'CRITICAL\t%s\t%s\n' "$d" "$(fingerprint_dir "$d")"
  done
  # mongod must stay stopped and disabled; a listener on 27017 would mean the
  # database came back up underneath us.
  printf 'CRITICAL\t%s\t%s\n' "__mongod_active"   "$(systemctl is-active mongod 2>&1)"
  printf 'CRITICAL\t%s\t%s\n' "__mongod_enabled"  "$(systemctl is-enabled mongod 2>&1)"
  printf 'CRITICAL\t%s\t%s\n' "__mongo_listener"  "$(ss -lnt 2>/dev/null | grep -c ':27017')"
  printf 'CRITICAL\t%s\t%s\n' "__user_crontab"    "$(crontab -l 2>/dev/null | sha256sum | cut -d' ' -f1)"
  for d in "${OBSERVED_DIRS[@]}"; do
    printf 'OBSERVED\t%s\t%s\n' "$d" "$(fingerprint_dir "$d")"
  done
}

check_disk() {
  local rc=0
  read -r free_gib use_pct < <(df -B1 / | awk 'NR==2{printf "%.1f %s", $4/1073741824, $5}')
  local inode_pct; inode_pct="$(df -i / | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
  echo "disk: free=${free_gib}GiB used=${use_pct} inode_used=${inode_pct}%"

  if (( $(echo "$free_gib < $STOP_FREE_GIB" | bc -l) )); then
    red "STOP: free ${free_gib}GiB < ${STOP_FREE_GIB}GiB stop-line (waiver W-3)."
    red "      Halt all writes. MongoDB data shares this volume."
    rc=2
  elif (( $(echo "$free_gib < $WARN_FREE_GIB" | bc -l) )); then
    ylw "WARN: free ${free_gib}GiB < ${WARN_FREE_GIB}GiB warn-line (waiver W-3)."
  fi
  if (( inode_pct >= STOP_INODE_PCT )); then
    red "STOP: inode ${inode_pct}% >= ${STOP_INODE_PCT}%."; rc=2
  fi
  return $rc
}

case "${1:-check}" in
  baseline)
    mkdir -p "$GUARD_DIR" || { red "cannot create $GUARD_DIR"; exit 4; }
    collect > "$BASELINE"
    grn "baseline written: $BASELINE"
    echo "  CRITICAL paths: ${#CRITICAL_DIRS[@]} (+mongod state, crontab)"
    echo "  OBSERVED paths: ${#OBSERVED_DIRS[@]} (reported, non-blocking)"
    check_disk; exit $?
    ;;
  check)
    [[ -f "$BASELINE" ]] || { red "no baseline; run: $0 baseline"; exit 4; }
    now="$(mktemp)"; collect > "$now"
    rc=0

    # --- CRITICAL: any difference is a hard stop --------------------------
    if ! diff <(grep '^CRITICAL' "$BASELINE") <(grep '^CRITICAL' "$now") \
         > "$GUARD_DIR/guard-diff-critical.txt" 2>&1; then
      red "CRITICAL ASSET CHANGED — refusing to continue (waiver C-5/C-8)."
      cat "$GUARD_DIR/guard-diff-critical.txt"
      rm -f "$now"; exit 3
    fi
    grn "critical assets intact (${#CRITICAL_DIRS[@]} paths + mongod + crontab)"

    # --- OBSERVED: report into the evidence trail, never block ------------
    if ! diff <(grep '^OBSERVED' "$BASELINE") <(grep '^OBSERVED' "$now") \
         > "$GUARD_DIR/guard-diff-observed.txt" 2>&1; then
      changed=$(grep '^> OBSERVED' "$GUARD_DIR/guard-diff-observed.txt" \
                | awk '{print $3}' | paste -sd, -)
      ylw "note: activity in co-resident working dirs (not lab-caused): $changed"
      {
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) observed-change: $changed"
      } >> "$GUARD_DIR/observed-activity.log"
    fi

    rm -f "$now"
    check_disk || rc=$?
    exit $rc
    ;;
  *)
    echo "usage: $0 {baseline|check}"; exit 4
    ;;
esac
