#!/usr/bin/env bash
# ============================================================================
# verify-dgx.sh — the DGX completion gate (Day-0 step 11).
#
# verify.sh is the LAB gate: it asserts simulated GPUs exist and that real ones
# do NOT. This is its mirror image. Every assertion here is the inverse or the
# hardware equivalent, and the two must never be confused — running the lab
# gate against hardware would "pass" a cluster with no GPUs at all.
#
# What this gate does NOT do: it does not prove NCCL bandwidth, NVLink
# topology, IB link rate or storage throughput. Those are the HW-* acceptance
# matrix, run after this gate, with their own thresholds. This answers the
# narrower question the Day-0 operator needs answered before letting anything
# else run: "is the control plane on this hardware the one we rehearsed?"
#
# USAGE:  KUBE_CONTEXT=arise-dgx ./scripts/verify-dgx.sh
#         GPU_NODES=4 GPU_PER_NODE=8 KUBE_CONTEXT=... ./scripts/verify-dgx.sh
#
# Writes a machine-parsable result next to the lab gate's, so a Day-0 run
# leaves the same kind of evidence a rehearsal does.
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"

# On hardware there is no kind context to fall back to: refusing to guess is
# the point, since the default would silently target the prelab cluster.
if [[ -z "${KUBE_CONTEXT:-}" ]]; then
  echo "KUBE_CONTEXT is unset. Refusing to guess — the fallback would be the" >&2
  echo "kind prelab cluster, and 'the hardware gate passed' would be a lie." >&2
  echo "Usage: KUBE_CONTEXT=<dgx context> $0" >&2
  exit 2
fi
CTX="$KUBE_CONTEXT"
K="kubectl --context $CTX"

GPU_NODES="${GPU_NODES:-4}"
GPU_PER_NODE="${GPU_PER_NODE:-${HW_GPU_PER_NODE:-8}}"
EXPECT_TOTAL=$((GPU_NODES * GPU_PER_NODE))
# Under evidence/RUN-*/ so .gitignore covers it: this is a RUN RESULT for one
# cluster at one moment, not source. Archive it out-of-band if a Day-0 run
# needs to be preserved (same rule as the lab evidence packs).
OUT="${VERIFY_DGX_OUT:-$REPO/evidence/RUN-dgx/verify-dgx.json}"

PASS=0; FAIL=0; WARN=0
declare -a RESULTS
chk() {  # chk <id> <description> <condition-exit-code>
  if [[ $3 -eq 0 ]]; then
    printf '  \033[32mPASS\033[0m %-10s %s\n' "$1" "$2"; PASS=$((PASS+1))
    RESULTS+=("{\"id\":\"$1\",\"status\":\"PASS\",\"desc\":\"$2\"}")
  else
    printf '  \033[31mFAIL\033[0m %-10s %s\n' "$1" "$2"; FAIL=$((FAIL+1))
    RESULTS+=("{\"id\":\"$1\",\"status\":\"FAIL\",\"desc\":\"$2\"}")
  fi
}
warn() {  # warn <id> <description>  — not yet installed, not a failure
  printf '  \033[33mWARN\033[0m %-10s %s\n' "$1" "$2"; WARN=$((WARN+1))
  RESULTS+=("{\"id\":\"$1\",\"status\":\"WARN\",\"desc\":\"$2\"}")
}

echo "=== DGX completion gate (context: $CTX) ==="

# --- the cluster ------------------------------------------------------------
READY=$($K get nodes --no-headers 2>/dev/null | grep -c ' Ready ')
[[ "$READY" -ge $((GPU_NODES + 1)) ]]
chk DGX-01 "at least $((GPU_NODES + 1)) nodes Ready — $GPU_NODES GPU + head (got $READY)" $?

LABELLED=$($K get nodes -l arise.ai/node-id --no-headers 2>/dev/null | wc -l)
[[ "$LABELLED" == "$GPU_NODES" ]]
chk DGX-02 "$GPU_NODES nodes carry arise.ai/node-id (got $LABELLED) — run label-nodes.sh" $?

# --- REAL GPUs are present and complete ------------------------------------
TOTAL=0; PERNODE_OK=1; MISSING=""
for n in $($K get nodes -l arise.ai/node-id --no-headers -o custom-columns=N:.metadata.name 2>/dev/null); do
  c=$($K get node "$n" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
  c=${c:-0}
  [[ "$c" == "$GPU_PER_NODE" ]] || { PERNODE_OK=0; MISSING="$MISSING $n=$c"; }
  TOTAL=$((TOTAL + c))
done
[[ $PERNODE_OK -eq 1 ]]
chk DGX-03 "every GPU node allocatable nvidia.com/gpu=$GPU_PER_NODE${MISSING:+ (off:$MISSING)}" $?
[[ "$TOTAL" == "$EXPECT_TOTAL" ]]
chk DGX-04 "fleet nvidia.com/gpu=$EXPECT_TOTAL (got $TOTAL)" $?

# A GPU count of zero with everything else green is the failure mode this gate
# exists to catch: the GPU Operator did not install, or the driver did not load,
# and every other assertion would still look fine.
[[ "$TOTAL" -gt 0 ]]
chk DGX-05 "GPUs are actually schedulable (not a driverless cluster)" $?

# --- NO simulated resources anywhere ---------------------------------------
# The mirror of the lab's SEC-03a. A single fake GPU here means the lab overlay
# was applied to hardware — workloads would believe they are in a sandbox while
# running on billable, contractually committed machines.
SIM=$($K get nodes -o json 2>/dev/null | python3 -c "
import json,sys
tot=0
for n in json.load(sys.stdin)['items']:
    for k,v in (n['status'].get('allocatable') or {}).items():
        if k.startswith('arise.dev/'):
            tot += int(v)
print(tot)" 2>/dev/null || echo 0)
[[ "${SIM:-0}" == "0" ]]
chk DGX-06 "zero arise.dev/* simulated capacity on the fleet (got $SIM)" $?

$K get ds -n platform-system fake-gpu-plugin >/dev/null 2>&1 && FAKE_DS=1 || FAKE_DS=0
[[ "$FAKE_DS" == "0" ]]
chk DGX-07 "the simulated device plugin is NOT installed" $?

$K get ns vast-mock >/dev/null 2>&1 && MOCK_NS=1 || MOCK_NS=0
[[ "$MOCK_NS" == "0" ]]
chk DGX-08 "the vast-mock namespace does NOT exist on hardware" $?

# --- the platform itself ----------------------------------------------------
for d in capacity-controller ops-console tenant-portal platform-gateway metering; do
  $K -n platform-system rollout status "deploy/$d" --timeout=120s >/dev/null 2>&1
  chk DGX-09 "platform-system/$d Available" $?
done

# --- the gates that fence customer contracts --------------------------------
for pol in arise-deny-simulated-gpu arise-tenant-owner-gate \
           arise-tenant-host-isolation arise-queue-binding \
           arise-priority-binding arise-flavor-quantization \
           arise-storage-quantization; do
  $K get validatingadmissionpolicybinding "$pol" >/dev/null 2>&1
  chk DGX-10 "admission binding $pol present" $?
done

$K get crd nodeownerships.infrastructure.arise.ai >/dev/null 2>&1
chk DGX-11 "NodeOwnership CRD established" $?

# --- posture: the switches that must be right before customers arrive -------
ADAPTER=$($K -n platform-system get deploy capacity-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VAST_ADAPTER")].value}' 2>/dev/null)
[[ "$ADAPTER" == "none" || "$ADAPTER" == "production-v1" ]]
chk DGX-12 "capacity-controller adapter is 'none' (or an approved production one); got '$ADAPTER'" $?

PRODFLAG=$($K -n platform-system get deploy capacity-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VAST_PRODUCTION_ADAPTER_ENABLED")].value}' 2>/dev/null)
[[ "$PRODFLAG" == "false" ]]
chk DGX-13 "production marketplace adapter flag=false (VST-06); got '$PRODFLAG'" $?

GPURES=$($K -n platform-system get deploy capacity-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FAKE_GPU_RESOURCE")].value}' 2>/dev/null)
[[ "$GPURES" == "nvidia.com/gpu" ]]
chk DGX-14 "drain gate watches nvidia.com/gpu (got '$GPURES')" $?

PUBLIC=$($K -n platform-system get deploy platform-gateway \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="GW_PUBLIC_MODE")].value}' 2>/dev/null)
[[ "$PUBLIC" == "true" ]]
chk DGX-15 "gateway runs in public mode (fail-closed credentials); got '$PUBLIC'" $?

$K -n platform-system get secret platform-gateway-auth >/dev/null 2>&1
chk DGX-16 "Secret platform-gateway-auth exists (make dgx-gateway-secret)" $?

# --- tenant quotas bound to the REAL resource -------------------------------
for ns in tenant-arise tenant-direct; do
  Q=$($K -n "$ns" get resourcequota -o json 2>/dev/null | python3 -c "
import json,sys
items=json.load(sys.stdin).get('items',[])
print(any('requests.nvidia.com/gpu' in (i['spec'].get('hard') or {}) for i in items))" 2>/dev/null)
  [[ "$Q" == "True" ]]
  chk DGX-17 "$ns quota bounds requests.nvidia.com/gpu" $?
done

# --- storage ----------------------------------------------------------------
for sc in arise-shared arise-longterm; do
  $K get storageclass "$sc" >/dev/null 2>&1
  chk DGX-18 "StorageClass $sc present" $?
done

# --- the tenant register the services actually read -------------------------
$K -n platform-system get configmap platform-tenants >/dev/null 2>&1
chk DGX-19 "ConfigMap platform-tenants present (make dgx-code)" $?

# Three states, each answered honestly: no config at all (FAIL — alertmanager
# cannot even start), config routing to the local sink (WARN — legal at
# bring-up, illegal at launch: every alert pages NOBODY), config routing to a
# real receiver (PASS). The earlier cut collapsed "no config" into "real
# receiver" because grep -c over empty input is 0 — a false pass on the worst
# of the three states.
if ! $K -n monitoring get secret alertmanager-config >/dev/null 2>&1; then
  chk DGX-21 "Secret alertmanager-config exists (make dgx-code seeds it)" 1
  chk DGX-22 "Alertmanager receiver unknowable — no config Secret" 1
else
  chk DGX-21 "Secret alertmanager-config exists" 0
  AMCFG=$($K -n monitoring get secret alertmanager-config \
    -o jsonpath='{.data.alertmanager\.yml}' 2>/dev/null | base64 -d 2>/dev/null)
  if printf '%s' "$AMCFG" | grep -q "name: local-sink"; then
    warn DGX-22 "Alertmanager routes to the LOCAL SINK — every alert pages NOBODY. Wire it: make dgx-alert-receiver WEBHOOK_URL=https://..."
  else
    chk DGX-22 "Alertmanager routes to a real receiver" 0
  fi
fi

# --- Day-0 retags: sentinels are legal at bring-up, not at launch ---------
DEVBOX=$($K -n platform-system get deploy tenant-portal \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DEVBOX_IMAGE")].value}' 2>/dev/null)
if [[ "$DEVBOX" == day0-registry.invalid/* || -z "$DEVBOX" ]]; then
  warn DGX-23 "DEVBOX_IMAGE is still the day0 sentinel ('$DEVBOX') — customers cannot create SSH dev machines until registry-mirror.sh runs and tenant-portal.yaml is retagged"
else
  chk DGX-23 "DEVBOX_IMAGE points at the registry ($DEVBOX)" 0
fi
WEBIMG=$($K -n platform-system get deploy platform-gateway \
  -o jsonpath='{.spec.template.spec.initContainers[0].image}' 2>/dev/null)
if [[ "$WEBIMG" == day0-registry.invalid/* || -z "$WEBIMG" ]]; then
  warn DGX-24 "gateway SPA image is still the day0 sentinel ('$WEBIMG') — the console cannot start until retagged"
else
  chk DGX-24 "gateway SPA image points at the registry" 0
fi

# --- things that are expected LATER: warn, never fail -----------------------
# DCGM arrives with the GPU Operator. Its absence before that step is normal;
# its absence AFTER it means GPU health is unobserved, which is why this is
# surfaced rather than silently skipped.
if $K get ns gpu-operator >/dev/null 2>&1; then
  if $K -n gpu-operator get svc nvidia-dcgm-exporter >/dev/null 2>&1; then
    chk DGX-20 "dcgm-exporter present (GPU health is observable)" 0
  else
    warn DGX-20 "GPU Operator installed but no dcgm-exporter service — GPU health is UNOBSERVED"
  fi
else
  warn DGX-20 "GPU Operator not installed yet (Day-0 step 7); GPU health unobserved"
fi

# --- evidence ---------------------------------------------------------------
mkdir -p "$(dirname "$OUT")"
{
  echo "{"
  echo "  \"context\": \"$CTX\","
  echo "  \"at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"gpu_nodes_expected\": $GPU_NODES, \"gpu_per_node_expected\": $GPU_PER_NODE,"
  echo "  \"passed\": $PASS, \"failed\": $FAIL, \"warned\": $WARN,"
  echo "  \"gate\": \"$([[ $FAIL -eq 0 ]] && echo PASS || echo FAIL)\","
  echo "  \"checks\": [$(IFS=,; echo "${RESULTS[*]}")]"
  echo "}"
} > "$OUT"

echo
echo "passed=$PASS failed=$FAIL warned=$WARN  -> $OUT"
[[ $FAIL -eq 0 ]] || exit 1
