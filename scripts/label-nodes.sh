#!/usr/bin/env bash
# ============================================================================
# label-nodes.sh — apply the logical DGX mapping (plan §7.5)
#
# Reads kind/node-map.yaml so the mapping has exactly one source of truth.
# Idempotent: --overwrite, and re-running produces no change.
#
# NOTE ON OWNERSHIP: this sets the INITIAL owner only. Once the Capacity
# Controller is running it becomes the sole writer of arise.ai/owner; a human
# editing that label afterwards is drift and will be corrected or quarantined
# within one reconcile cycle (plan §8.3, test OWN-06).
# ============================================================================
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"
CTX="kind-${CLUSTER_NAME}"
EV="$REPO/evidence/$(cat "$REPO/.run_id")/deploy"
mkdir -p "$EV"

mapfile -t ROWS < <(python3 - "$REPO/kind/node-map.yaml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
for n in doc["spec"]["nodes"]:
    print(f'{n["kindNode"]}\t{n["nodeId"]}\t{n["pair"]}\t{n["initialOwner"]}')
PY
)

echo "applying labels from kind/node-map.yaml (${#ROWS[@]} nodes)"
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r kindnode nodeid pair owner <<< "$row"
  kubectl --context "$CTX" label node "$kindnode" \
    "arise.ai/node-id=${nodeid}" \
    "arise.ai/pair=${pair}" \
    "arise.ai/owner=${owner}" \
    --overwrite
done

# ---- auxiliary nodes (CPU pool + storage) ----------------------------------
# These are fleet members but NOT DGX nodes: no node-id, so the ownership
# state machine and the VAST lifecycle ignore them by construction.
mapfile -t AUX < <(python3 - "$REPO/kind/node-map.yaml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
for n in doc["spec"].get("auxNodes", []):
    print(f'{n["kindNode"]}\t{n["name"]}\t{n["role"]}\t{n.get("owner","-")}\t{n.get("taint","-")}')
PY
)
echo "applying aux-node labels (${#AUX[@]} nodes)"
for row in "${AUX[@]}"; do
  IFS=$'\t' read -r kindnode name role owner taintspec <<< "$row"
  hw=$([[ "$role" == "cpu" ]] && echo generic-cpu || echo storage-server)
  kubectl --context "$CTX" label node "$kindnode" \
    "arise.ai/aux-name=${name}" "arise.ai/role=${role}" "arise.ai/hw-profile=${hw}" --overwrite
  if [[ "$owner" != "-" ]]; then
    kubectl --context "$CTX" label node "$kindnode" "arise.ai/owner=${owner}" --overwrite
  fi
  if [[ "$taintspec" != "-" ]]; then
    kubectl --context "$CTX" taint node "$kindnode" "$taintspec" --overwrite
  fi
done
# GPU workers get an explicit role too, so "role=gpu" selectors work uniformly.
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r kindnode _ _ _ <<< "$row"
  kubectl --context "$CTX" label node "$kindnode" \
    arise.ai/role=gpu arise.ai/hw-profile=dgx-b300 --overwrite
done

# The control-plane must never carry a node-id: it exposes zero fake GPUs and
# must never be a transition target (plan §8.2).
CP="${CLUSTER_NAME}-control-plane"
kubectl --context "$CTX" label node "$CP" \
  arise.ai/node-id- arise.ai/pair- arise.ai/owner- 2>/dev/null || true

kubectl --context "$CTX" get nodes \
  -L arise.ai/node-id,arise.ai/pair,arise.ai/owner \
  | tee "$EV/node-labels.txt"
