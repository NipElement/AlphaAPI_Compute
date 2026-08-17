#!/usr/bin/env bash
# ============================================================================
# onboard-node.sh — register a node into the fleet (the "机器注册" path)
#
#   ./scripts/onboard-node.sh <node-name> gpu <dgxNN> <pair>      # DGX node
#   ./scripts/onboard-node.sh <node-name> cpu <cpuNN>             # CPU pool
#   ./scripts/onboard-node.sh <node-name> storage <storNN>        # storage
#   ./scripts/onboard-node.sh <node-name> deregister              # strip all
#
# This is the exact flow that runs when real machines arrive: the machine
# joins the cluster (kubeadm join on hardware; already-present in kind), and
# THIS script turns a bare node into a fleet member — labels, taints, and for
# GPU nodes the NodeOwnership object that puts it under the state machine.
# NODE-01 tests the full deregister→onboard round trip, so the path the real
# hardware will take is exercised on every matrix run, not written and hoped.
#
# What it deliberately does NOT do:
#   - it never touches a node that still runs tenant workloads (checked)
#   - it does not install drivers/agents — host-layer work belongs to the
#     Ansible baseline (see runbooks/machine-registration.md)
#   - deregister refuses on a node whose NodeOwnership still shows contracts
# ============================================================================
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"
CTX="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
K="kubectl --context $CTX"

NODE="${1:?node name required}"
ROLE="${2:?role required: gpu|cpu|storage|deregister}"

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }

$K get node "$NODE" >/dev/null || { red "node $NODE not found in cluster"; exit 1; }

tenant_pods_on() {
  $K get pods -A --field-selector "spec.nodeName=$1" -o json 2>/dev/null | python3 -c '
import json, sys
n = 0
for p in json.load(sys.stdin)["items"]:
    if p["metadata"]["namespace"].startswith("tenant-") and \
       p.get("status", {}).get("phase") not in ("Succeeded", "Failed"):
        n += 1
print(n)'
}

case "$ROLE" in
  gpu)
    NID="${3:?logical id required, e.g. dgx05}"
    PAIR="${4:?pair required, e.g. 05-06}"
    $K label node "$NODE" \
      "arise.ai/node-id=$NID" "arise.ai/pair=$PAIR" \
      "arise.ai/role=gpu" "arise.ai/owner=ARISE" \
      "arise.ai/hw-profile=dgx-b300" --overwrite
    # Registering a GPU node means putting it under the ownership state
    # machine from minute one — an unmanaged GPU node is exactly the kind of
    # asset that ends up double-committed.
    cat <<Y | $K apply -f -
apiVersion: infrastructure.arise.ai/v1alpha1
kind: NodeOwnership
metadata:
  name: $NID
spec:
  desiredOwner: ARISE
  transitionId: onboard-$NID-$(date +%s)
  requireSanitization: true
  pair: "$PAIR"
  approvedBy: onboard-node.sh
  reason: "initial registration"
Y
    grn "onboarded $NODE as $NID (pair $PAIR, owner state machine engaged)"
    ;;
  cpu)
    NAME="${3:?aux name required, e.g. cpu03}"
    $K label node "$NODE" \
      "arise.ai/aux-name=$NAME" "arise.ai/role=cpu" "arise.ai/owner=ARISE" \
      "arise.ai/hw-profile=generic-cpu" --overwrite
    grn "onboarded $NODE as CPU-pool node $NAME"
    ;;
  storage)
    NAME="${3:?aux name required, e.g. stor02}"
    $K label node "$NODE" \
      "arise.ai/aux-name=$NAME" "arise.ai/role=storage" \
      "arise.ai/hw-profile=storage-server" --overwrite
    $K taint node "$NODE" "arise.ai/storage-only=true:NoSchedule" --overwrite
    grn "onboarded $NODE as storage node $NAME (tenant-tainted)"
    ;;
  deregister)
    # Refuse while the node carries anything a tenant is paying for.
    NID=$($K get node "$NODE" -o jsonpath='{.metadata.labels.arise\.ai/node-id}')
    if [[ -n "$NID" ]]; then
      CONTRACTS=$($K get nodeownership "$NID" -o jsonpath='{.status.activeContracts}' 2>/dev/null || echo 0)
      if [[ "${CONTRACTS:-0}" != "0" ]]; then
        red "refusing: $NID has $CONTRACTS active contract(s); reclaim through the state machine first"
        exit 1
      fi
    fi
    LIVE=$(tenant_pods_on "$NODE")
    if [[ "$LIVE" != "0" ]]; then
      red "refusing: $LIVE tenant pod(s) still on $NODE; drain via the state machine first"
      exit 1
    fi
    [[ -n "$NID" ]] && $K delete nodeownership "$NID" --ignore-not-found >/dev/null
    $K label node "$NODE" \
      arise.ai/node-id- arise.ai/pair- arise.ai/role- \
      arise.ai/owner- arise.ai/aux-name- arise.ai/hw-profile- 2>/dev/null || true
    $K taint node "$NODE" arise.ai/storage-only- 2>/dev/null || true
    $K taint node "$NODE" arise.ai/vast-owned- 2>/dev/null || true
    $K taint node "$NODE" arise.ai/direct-owned- 2>/dev/null || true
    $K uncordon "$NODE" >/dev/null 2>&1 || true
    grn "deregistered $NODE (bare node, out of every pool)"
    ;;
  *)
    red "unknown role '$ROLE'"; exit 1 ;;
esac

$K get node "$NODE" \
  -o custom-columns='NODE:.metadata.name,ROLE:.metadata.labels.arise\.ai/role,ID:.metadata.labels.arise\.ai/node-id,AUX:.metadata.labels.arise\.ai/aux-name,OWNER:.metadata.labels.arise\.ai/owner'
