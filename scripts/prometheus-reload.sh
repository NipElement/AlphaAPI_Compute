#!/usr/bin/env bash
# ============================================================================
# prometheus-reload.sh — make the RUNNING prometheus match its ConfigMaps.
#
# Three traps this exists for, all observed on the live lab (2026-08-30):
#   1. Prometheus reads its config file ONCE at start. A ConfigMap edit
#      changes nothing in the process — the lab ran 17 days with a config
#      that declared a metering scrape the process had never loaded.
#   2. A ConfigMap volume updates ASYNCHRONOUSLY (kubelet sync, up to ~1 min).
#      Reloading immediately after `kubectl apply` re-reads the OLD file and
#      reports success — which is how the first cut of this fix failed.
#   3. "I ran the reload" is not evidence. This script VERIFIES the loaded
#      jobs and rule groups against the ConfigMaps and exits non-zero if they
#      still differ after the deadline.
#
#   KUBE_CONTEXT=<ctx> scripts/prometheus-reload.sh [timeout-seconds]
# ============================================================================
set -uo pipefail
K="kubectl${KUBE_CONTEXT:+ --context=$KUBE_CONTEXT}"
DEADLINE=$(( $(date +%s) + ${1:-120} ))
$K -n monitoring get deploy prometheus >/dev/null 2>&1 || { echo "no prometheus in this cluster; nothing to reload"; exit 0; }

desired_jobs() {
  $K -n monitoring get cm prometheus-config -o jsonpath='{.data.prometheus\.yml}' 2>/dev/null \
    | grep -oE 'job_name: [^ ]+' | awk '{print $2}' | sort -u
}
# Group NAMES are not enough. Editing an alert's EXPRESSION leaves every group
# name identical, so the old check reported "4 rule groups LIVE" while
# prometheus was still evaluating the previous expression — observed
# 2026-08-31, when a repointed NodeQuarantined stayed silent through a
# reload this script called a success. Each alert is now fingerprinted by
# expression + `for` + severity: everything that decides whether it fires and
# who it wakes. (scripts/prometheus-rule-fingerprint.py)
FP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prometheus-rule-fingerprint.py"
desired_groups() {
  $K -n monitoring get cm prometheus-rules -o jsonpath='{.data}' 2>/dev/null \
    | python3 "$FP" desired 2>/dev/null
}
live_groups() {
  $K -n monitoring exec deploy/prometheus -- wget -qO- 'http://127.0.0.1:9090/api/v1/rules' 2>/dev/null \
    | python3 "$FP" live 2>/dev/null
}

live_jobs() {
  $K -n monitoring exec deploy/prometheus -- wget -qO- 'http://127.0.0.1:9090/api/v1/targets?state=any' 2>/dev/null \
    | python3 -c "import json,sys; print('\n'.join(sorted({t['labels'].get('job','') for t in json.load(sys.stdin)['data']['activeTargets']})))" 2>/dev/null
}

missing() {  # missing <desired-newline-list> <live-newline-list>
  local out="" x
  for x in $1; do printf '%s\n' $2 | grep -qx "$x" || out="$out $x"; done
  printf '%s' "$out"
}

DJ=$(desired_jobs); DG=$(desired_groups)
[[ -n "$DJ" ]] || { echo "prometheus-config has no scrape jobs — refusing to call this a success"; exit 1; }
RESTARTED=0
while :; do
  if ! $K -n monitoring exec deploy/prometheus -- wget --post-data='' -qO- http://127.0.0.1:9090/-/reload >/dev/null 2>&1; then
    # No lifecycle endpoint (or the pod is unreachable): fall back to a restart,
    # but AT MOST ONCE — a config prometheus rejects would otherwise make this
    # loop flap the deployment until the deadline instead of failing loudly.
    if (( RESTARTED )); then
      echo "reload endpoint still refusing after a restart; not restarting again" >&2
    else
      echo "reload endpoint refused (needs --web.enable-lifecycle); restarting prometheus instead"
      RESTARTED=1
      $K -n monitoring rollout restart deploy/prometheus >/dev/null
      $K -n monitoring rollout status deploy/prometheus --timeout=180s >/dev/null
    fi
  fi
  sleep 6
  MJ=$(missing "$DJ" "$(live_jobs)"); MG=$(missing "$DG" "$(live_groups)")
  if [[ -z "$MJ" && -z "$MG" ]]; then
    echo "prometheus reloaded: $(printf '%s' "$DJ" | wc -w) jobs, $(printf '%s\n' "$DG" | grep -c '^group:') rule groups, $(printf '%s\n' "$DG" | grep -cv '^group:') alert rules LIVE (expression + for + severity verified)"
    exit 0
  fi
  if (( $(date +%s) >= DEADLINE )); then
    echo "prometheus still does not match its ConfigMaps after the deadline:" >&2
    [[ -n "$MJ" ]] && echo "  scrape jobs not live:$MJ" >&2
    [[ -n "$MG" ]] && { echo "  rules not loaded as declared:" >&2; printf '    %s\n' $MG >&2; }
    echo "  (a ConfigMap volume can take ~1 min to sync; if this persists, restart prometheus)" >&2
    exit 1
  fi
  sleep 9
done
