#!/usr/bin/env bash
# ============================================================================
# verify.sh — Stage 6 completion gate (plan §7.8)
#
# 完成门: four workers Ready with allocatable=8 each; all P0 platform pods
# Ready; real nvidia.com/gpu capacity is 0 everywhere; VAST production adapter
# reports disabled.
#
# Prints a machine-parsable summary to evidence/<run_id>/deploy/verify.json so
# the result is not a screenshot (plan §9.4).
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"
CTX="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
K="kubectl --context $CTX"
EV="$REPO/evidence/$(cat "$REPO/.run_id")/deploy"
mkdir -p "$EV"

PASS=0; FAIL=0
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

echo "=== Stage 6 completion gate ==="

# --- nodes ------------------------------------------------------------------
# 8 = 1 control-plane + 4 GPU (dgx01..04) + 2 CPU (cpu01/02) + 1 storage
READY=$($K get nodes --no-headers 2>/dev/null | grep -c ' Ready ')
[[ "$READY" == "8" ]]; chk DEP-04 "8 nodes Ready (got $READY)" $?

# Aux nodes must exist with their roles, and must expose ZERO fake GPUs —
# they are not in the advertiser's map, and that absence is load-bearing.
CPU_N=$($K get nodes -l arise.ai/role=cpu --no-headers 2>/dev/null | grep -c ' Ready ')
[[ "$CPU_N" == "2" ]]; chk AUX-01 "2 CPU-pool nodes Ready (got $CPU_N)" $?
STOR_N=$($K get nodes -l arise.ai/role=storage --no-headers 2>/dev/null | wc -l)
[[ "$STOR_N" == "1" ]]; chk AUX-02 "1 storage node present (got $STOR_N)" $?
AUX_GPU=$($K get nodes -l 'arise.ai/role in (cpu,storage)' -o json 2>/dev/null \
  | python3 -c "import json,sys;print(sum(int(n['status'].get('allocatable',{}).get('arise.dev/fake-gpu',0)) for n in json.load(sys.stdin)['items']))")
[[ "${AUX_GPU:-0}" == "0" ]]; chk AUX-03 "aux nodes expose 0 fake-gpu (got $AUX_GPU)" $?
STOR_TAINT=$($K get nodes -l arise.ai/role=storage -o jsonpath='{.items[0].spec.taints[?(@.key=="arise.ai/storage-only")].effect}' 2>/dev/null)
[[ "$STOR_TAINT" == "NoSchedule" ]]; chk AUX-04 "storage node tainted NoSchedule" $?

# --- fake GPU capacity ------------------------------------------------------
TOTAL=0; PERNODE_OK=1
for n in $($K get nodes -l arise.ai/node-id --no-headers -o custom-columns=N:.metadata.name 2>/dev/null); do
  c=$($K get node "$n" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}' 2>/dev/null)
  c=${c:-0}
  [[ "$c" == "8" ]] || PERNODE_OK=0
  TOTAL=$((TOTAL + c))
done
[[ $PERNODE_OK -eq 1 ]]; chk SCH-01a "every worker allocatable=8" $?
[[ "$TOTAL" == "32" ]]; chk SCH-01b "cluster total fake-gpu=32 (got $TOTAL)" $?

CP_GPU=$($K get node "${CLUSTER_NAME}-control-plane" \
        -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}' 2>/dev/null)
[[ -z "$CP_GPU" || "$CP_GPU" == "0" ]]; chk SCH-01c "control-plane exposes 0 fake-gpu" $?

# Simulated capacity must equal REAL DGX B300 magnitudes (versions.env cites
# the NVIDIA user guide): 4 nodes x 256 vCPU / 2048 GiB + 2 CPU nodes x 64/512.
SIMV=$($K get nodes -o json 2>/dev/null | python3 -c "import json,sys;print(sum(int(n['status'].get('allocatable',{}).get('arise.dev/sim-vcpu',0)) for n in json.load(sys.stdin)['items']))")
[[ "$SIMV" == "1152" ]]; chk HW-SIM1 "fleet sim-vCPU = 1152 (4x256 + 2x64) (got $SIMV)" $?
SIMM=$($K get nodes -o json 2>/dev/null | python3 -c "import json,sys;print(sum(int(n['status'].get('allocatable',{}).get('arise.dev/sim-mem-gi',0)) for n in json.load(sys.stdin)['items']))")
[[ "$SIMM" == "9216" ]]; chk HW-SIM2 "fleet sim-mem = 9216 Gi (4x2048 + 2x512) (got $SIMM)" $?

# --- no REAL gpu anywhere ---------------------------------------------------
REAL=$($K get nodes -o json 2>/dev/null \
      | python3 -c "import json,sys;print(sum(int(n['status'].get('allocatable',{}).get('nvidia.com/gpu',0)) for n in json.load(sys.stdin)['items']))" 2>/dev/null || echo 0)
[[ "$REAL" == "0" ]]; chk SEC-03a "nvidia.com/gpu capacity is 0 (got $REAL)" $?

# --- platform workloads -----------------------------------------------------
for d in platform-system/capacity-controller platform-system/fake-gpu-advertiser vast-mock/vast-mock; do
  ns="${d%%/*}"; name="${d##*/}"
  $K -n "$ns" rollout status "deploy/$name" --timeout=120s >/dev/null 2>&1
  chk "DEP-05" "$ns/$name Available" $?
done

# --- admission policies bound ----------------------------------------------
VAP=$($K get validatingadmissionpolicy --no-headers 2>/dev/null | grep -c arise)
[[ "$VAP" -ge 3 ]]; chk SEC-02a "ValidatingAdmissionPolicies present (got $VAP)" $?

# --- CRD --------------------------------------------------------------------
$K get crd nodeownerships.infrastructure.arise.ai >/dev/null 2>&1
chk OWN-00 "NodeOwnership CRD established" $?

# --- VAST production adapter disabled (VST-06) ------------------------------
ENV_OFF=$($K -n platform-system get deploy capacity-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VAST_PRODUCTION_ADAPTER_ENABLED")].value}' 2>/dev/null)
[[ "$ENV_OFF" == "false" ]]; chk VST-06a "production adapter flag=false (got '$ENV_OFF')" $?

# --- API server bound to loopback only (plan §5.3) --------------------------
APISRV=$($K config view -o jsonpath="{.clusters[?(@.name=='$CTX')].cluster.server}" 2>/dev/null)
[[ "$APISRV" == https://127.0.0.1:* ]]; chk DEP-04b "API server is loopback-only ($APISRV)" $?

# --- evidence ---------------------------------------------------------------
{
  echo "{"
  echo "  \"run_id\": \"$(cat "$REPO/.run_id")\","
  echo "  \"at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"passed\": $PASS, \"failed\": $FAIL,"
  echo "  \"gate\": \"$([[ $FAIL -eq 0 ]] && echo PASS || echo FAIL)\","
  echo "  \"checks\": [$(IFS=,; echo "${RESULTS[*]}")]"
  echo "}"
} > "$EV/verify.json"

echo
echo "passed=$PASS failed=$FAIL  -> $EV/verify.json"
[[ $FAIL -eq 0 ]] || exit 1
