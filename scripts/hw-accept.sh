#!/usr/bin/env bash
# ============================================================================
# hw-accept.sh — run the hardware acceptance jobs and grade them (Day-0 step 13)
#
#   KUBE_CONTEXT=<dgx> scripts/hw-accept.sh 1node|2node|all [--cleanup]
#
# Applies infra/dgx/acceptance/{common,nccl-<n>node}.yaml, waits for the
# Volcano job to finish, parses the HW_RESULT line the script prints, grades
# it against infra/dgx/acceptance/hw-thresholds.env and writes
# evidence/RUN-dgx/hw-accept-<test>-<ts>.json (plus the full pod logs and
# nvidia-smi -q from the node). A job that never prints HW_RESULT is a FAIL,
# not an "inconclusive": the hardware either meets the written promise or it
# does not, and the offer page quotes this file.
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO"
# shellcheck disable=SC1091
source infra/dgx/acceptance/hw-thresholds.env
[[ -n "${KUBE_CONTEXT:-}" ]] || { echo "KUBE_CONTEXT is unset; refusing to guess a cluster" >&2; exit 2; }
K="kubectl --context $KUBE_CONTEXT"
MODE="${1:?1node|2node|all}"; CLEANUP="${2:-}"
OUTDIR="$REPO/evidence/RUN-dgx"; mkdir -p "$OUTDIR"
FAILS=0

run_one() {  # run_one <1node|2node>
  local n="$1" job="hw-nccl-$n" test min
  case "$n" in
    1node) test=nvlink-1node; min="$NVLINK_BUSBW_MIN_GBPS" ;;
    2node) test=ib-2node;     min="$IB_2NODE_BUSBW_MIN_GBPS" ;;
    *) echo "unknown mode $n" >&2; return 2 ;;
  esac
  local ts; ts=$(date -u +%Y%m%dT%H%M%SZ)
  local out="$OUTDIR/hw-accept-$test-$ts.json"
  echo "=== $test (job $job, threshold busbw >= $min GB/s) ==="
  $K apply -f infra/dgx/acceptance/common.yaml >/dev/null || return 1
  $K -n hw-acceptance delete vcjob "$job" --ignore-not-found --wait=true >/dev/null 2>&1
  $K apply -f "infra/dgx/acceptance/nccl-$n.yaml" >/dev/null || return 1
  local deadline=$(( $(date +%s) + 1200 )) phase=""
  while (( $(date +%s) < deadline )); do
    phase=$($K -n hw-acceptance get vcjob "$job" -o jsonpath='{.status.state.phase}' 2>/dev/null)
    [[ "$phase" == Completed || "$phase" == Failed || "$phase" == Terminated || "$phase" == Aborted ]] && break
    sleep 10
  done
  local logs; logs="$OUTDIR/hw-accept-$test-$ts.log"
  $K -n hw-acceptance logs -l "volcano.sh/job-name=$job" --all-containers --tail=-1 --prefix > "$logs" 2>&1 || true
  local node; node=$($K -n hw-acceptance get pod -l "volcano.sh/job-name=$job" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)
  local result; result=$(grep -h "HW_RESULT" "$logs" | head -1 | sed 's/.*HW_RESULT //')
  python3 - "$out" "$test" "$phase" "$min" "$node" "$logs" "$result" <<'PY'
import json, sys, time
out, test, phase, mn, node, logs, raw = sys.argv[1:8]
r = json.loads(raw) if raw.strip().startswith("{") else {}
checks = []
def chk(id_, desc, ok): checks.append({"id": id_, "desc": desc, "status": "PASS" if ok else "FAIL"})
chk("HW-03" if test == "nvlink-1node" else "HW-06a", "job completed and reported", phase == "Completed" and bool(r))
if test == "nvlink-1node":
    chk("HW-03", f"8 GPUs visible per node (got {r.get('gpus_per_node')}), HBM {r.get('hbm_gib')}", r.get("gpus_per_node") == 8)
    chk("HW-04", f"NVLink all-reduce busbw {r.get('busbw_gbps')} GB/s >= {mn}", (r.get("busbw_gbps") or 0) >= float(mn))
    chk("HW-05", "all-reduce result element-exact", r.get("correct") is True)
else:
    ib = "NCCL INFO NET/IB" in open(logs, errors="replace").read() or "via NET/IB" in open(logs, errors="replace").read()
    chk("HW-06", f"two-node all-reduce busbw {r.get('busbw_gbps')} GB/s >= {mn} over IB (IB transport seen: {ib})",
        (r.get("busbw_gbps") or 0) >= float(mn) and ib and r.get("world") == 16)
    chk("HW-07", "16-GPU all-reduce result element-exact", r.get("correct") is True)
rec = {"test": test, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "job_phase": phase,
       "node": node, "threshold_busbw_gbps": float(mn), "result": r, "checks": checks, "logs": logs}
json.dump(rec, open(out, "w"), indent=2)
for c in checks: print(f"  {c['status']:4} {c['id']:6} {c['desc']}")
sys.exit(0 if all(c["status"] == "PASS" for c in checks) else 1)
PY
  local rc=$?
  [[ -n "$node" ]] && $K get node "$node" -o yaml > "$OUTDIR/hw-accept-$test-$ts-node.yaml" 2>/dev/null
  echo "  evidence: $out"
  return $rc
}

case "$MODE" in
  1node|2node) run_one "$MODE" || FAILS=$((FAILS+1)) ;;
  all) run_one 1node || FAILS=$((FAILS+1)); run_one 2node || FAILS=$((FAILS+1)) ;;
  *) echo "usage: $0 1node|2node|all [--cleanup]" >&2; exit 2 ;;
esac
[[ "$CLEANUP" == "--cleanup" ]] && $K delete ns hw-acceptance --ignore-not-found --wait=false >/dev/null
if (( FAILS )); then echo "HW ACCEPTANCE: FAIL ($FAILS)"; exit 1; fi
echo "HW ACCEPTANCE: PASS"
