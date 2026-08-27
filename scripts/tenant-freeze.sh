#!/usr/bin/env bash
# ============================================================================
# tenant-freeze.sh — the operator's lever for a non-paying / breaching tenant.
#
#   tenant-freeze.sh <namespace> freeze   "<reason>"   # step 1: no NEW work
#   tenant-freeze.sh <namespace> stop     "<reason>"   # step 2: after grace,
#                                                      #   stop running work
#   tenant-freeze.sh <namespace> restore  "<reason>"   # lift the freeze
#
# freeze  = label arise.ai/suspended=true. Admission (arise-tenant-suspended)
#           refuses every new pod/job/deployment/PVC/service in the namespace
#           with a message naming the reason. Running work keeps running:
#           a late invoice is not a reason to kill a training run mid-epoch.
# stop    = after the grace period (a commercial number, D6): delete the
#           tenant's running pods, deployments and volcano jobs. Volumes are
#           NOT touched — data deletion is a separate, human, two-person step
#           (runbooks/incident-disk-full.md has the rules).
# restore = remove the label. Nothing is recreated; the tenant resubmits.
#
# Every action is annotated on the namespace with who/when/why, because the
# apiserver audit log records the label change but not the reason.
# ============================================================================
set -euo pipefail
NS="${1:?namespace}"; ACTION="${2:?freeze|stop|restore}"; REASON="${3:?reason (quoted)}"
CTX="${KUBE_CONTEXT:-kind-${CLUSTER_NAME:-b300-prelab}}"
K="kubectl --context $CTX"
WHO="${APPROVED_BY:-$(whoami)@$(hostname -s)}"
NOW=$(date -u +%FT%TZ)

$K get ns "$NS" -o jsonpath='{.metadata.labels.arise\.ai/tier}' 2>/dev/null | grep -q '^tenant$' \
  || { echo "$NS is not a tenant namespace (arise.ai/tier=tenant); refusing" >&2; exit 2; }

case "$ACTION" in
  freeze)
    $K label ns "$NS" arise.ai/suspended=true --overwrite >/dev/null
    $K annotate ns "$NS" arise.ai/suspended-by="$WHO" arise.ai/suspended-at="$NOW" \
       arise.ai/suspended-reason="$REASON" --overwrite >/dev/null
    echo "FROZEN $NS: new workloads refused at admission. Running work untouched."
    echo "Grace period is commercial (D6); when it ends: $0 $NS stop \"$REASON\""
    ;;
  stop)
    [[ "$($K get ns "$NS" -o jsonpath='{.metadata.labels.arise\.ai/suspended}')" == "true" ]] \
      || { echo "$NS is not frozen; freeze first (stop without freeze lets work return immediately)" >&2; exit 2; }
    echo "stopping running work in $NS (volumes untouched):"
    $K -n "$NS" get pods,deploy,jobs.batch.volcano.sh --no-headers 2>/dev/null | sed 's/^/  /' || true
    $K -n "$NS" delete jobs.batch.volcano.sh --all --wait=false >/dev/null 2>&1 || true
    $K -n "$NS" delete deploy --all --wait=false >/dev/null 2>&1 || true
    $K -n "$NS" delete pods --all --wait=false >/dev/null 2>&1 || true
    $K annotate ns "$NS" arise.ai/stopped-by="$WHO" arise.ai/stopped-at="$NOW" --overwrite >/dev/null
    echo "STOPPED $NS. Volumes retained; the metering ledger closes their intervals on the next poll."
    ;;
  restore)
    $K label ns "$NS" arise.ai/suspended- >/dev/null
    $K annotate ns "$NS" arise.ai/restored-by="$WHO" arise.ai/restored-at="$NOW" \
       arise.ai/restored-reason="$REASON" --overwrite >/dev/null
    echo "RESTORED $NS: admission accepts new workloads again. Nothing was recreated."
    ;;
  *) echo "action must be freeze|stop|restore" >&2; exit 2 ;;
esac
