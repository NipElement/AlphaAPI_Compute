#!/usr/bin/env bash
# ============================================================================
# lib.sh — assertions + evidence capture for the Phase A matrix.
#
# Design rules that come straight from plan §9.4 / §10.2:
#   - A test's verdict comes from a machine-checkable assertion, never from a
#     human reading output. Screenshots are auxiliary at best.
#   - If required evidence cannot be written, the case is INVALID, not PASS.
#   - A case that cannot run because a precondition is unavailable is BLOCKED,
#     and BLOCKED is never silently upgraded.
#   - "The object exists" is not evidence that "the object is enforced".
#     Where enforcement is the claim, probe it empirically.
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"
RUN_ID="$(cat "$REPO/.run_id")"
# KUBE_CONTEXT lets the SAME matrix run against the DGX cluster on day 0.
# Without it every assertion here is welded to the kind cluster, and the
# suite that proves the platform works could never be pointed at the
# hardware it was written for. Same pattern as scripts/onboard-node.sh.
CTX="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
K="kubectl --context $CTX"

# ------------------------------------------------------------- overlay -----
# OVERLAY=lab (default) runs against the simulation; OVERLAY=dgx against
# hardware. Review 2026-08-27 (Day-0 toolkit): the matrix used to be welded to
# the simulated resource, so `make dgx-test` could never pass — SEC-03 even
# asserted the real GPUs must NOT exist. Every case now requests $GPU_RES; the
# handful that exist only to exercise lab simulation (fault injection on the
# fake advertiser, the vast-mock, grafana, the aux cpu nodes) declare
# `lab_only` and are SKIPPED on dgx — printed, counted and listed in the
# summary, never silently.
OVERLAY="${OVERLAY:-lab}"
case "$OVERLAY" in
  lab) GPU_RES="$FAKE_GPU_RESOURCE"; GPU_PER_NODE="$FAKE_GPU_PER_NODE"
       GPU_TOTAL="$FAKE_GPU_TOTAL"; SCRATCH_NODE=cpu02; SCRATCH_ROLE=cpu ;;
  dgx) GPU_RES="nvidia.com/gpu"; GPU_PER_NODE="${HW_GPU_PER_NODE:-8}"
       GPU_TOTAL=$(( GPU_PER_NODE * ${GPU_NODES:-4} )); SCRATCH_NODE=dgx04; SCRATCH_ROLE=gpu ;;
  *)   echo "OVERLAY must be lab or dgx (got '$OVERLAY')" >&2; exit 2 ;;
esac
SIM_GPU_RES="$FAKE_GPU_RESOURCE"        # the resource that must NOT exist on dgx
GPU_RES_JP="${GPU_RES//./\\.}"           # jsonpath-escaped form (dots)

lab_only() {  # lab_only <why> — call right after begin; returns 0 when skipping
  [[ "$OVERLAY" == lab ]] && return 1
  CUR_STATUS="SKIPPED"; CUR_MSGS+=("SKIPPED on $OVERLAY: $*")
  echo "      $(c_ylw "⊘ SKIPPED (lab-only): $*")"
  end
  return 0
}
EVROOT="$REPO/evidence/$RUN_ID/tests"

# --------------------------------------------------------------- node names --
# Tests must never spell a PHYSICAL node name. kind calls its nodes
# "<cluster>-workerN"; four DGX B300 machines are called whatever the datacenter
# calls them. Every assertion here is about a LOGICAL node — dgx03, cpu02, the
# head node — and the cluster already carries that mapping as labels
# (arise.ai/node-id on GPU nodes, arise.ai/aux-name on the aux pool), applied by
# scripts/label-nodes.sh from kind/node-map.yaml.
#
# Resolving through the labels is what lets the SAME suite run on hardware. It
# also removes a quiet failure mode: a hardcoded name that no longer exists
# makes kubectl return empty, and an assertion comparing empty to empty passes.
node_for() {  # node_for dgx03 | node_for cpu02 | node_for control-plane
  local want="$1" name=""
  case "$want" in
    control-plane)
      name=$($K get nodes -l node-role.kubernetes.io/control-plane \
             -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) ;;
    dgx*)
      name=$($K get nodes -l "arise.ai/node-id=$want" \
             -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) ;;
    *)
      name=$($K get nodes -l "arise.ai/aux-name=$want" \
             -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) ;;
  esac
  if [[ -z "$name" ]]; then
    # Loud, not empty: an unresolvable logical node means the cluster was never
    # labelled (run scripts/label-nodes.sh) and every assertion downstream would
    # otherwise compare one empty string to another and call it a PASS.
    echo "NODE-NOT-FOUND-$want"
    return 1
  fi
  printf '%s' "$name"
}

# current test context
CUR_ID=""; CUR_PRIO=""; CUR_DESC=""; CUR_DIR=""
CUR_STATUS=""; CUR_MSGS=(); CUR_START=0
PASS_N=0; FAIL_N=0; BLOCK_N=0; INVALID_N=0; SKIP_N=0
# Results accumulate as JSON Lines in a file. Building JSON by string-pasting
# in bash breaks the moment a message contains a quote or newline — which is
# exactly what admission-denial messages are full of.
RESULTS_JSONL="$(mktemp)"
trap 'rm -f "$RESULTS_JSONL"' EXIT

c_red(){ printf '\033[31m%s\033[0m' "$*"; }
c_grn(){ printf '\033[32m%s\033[0m' "$*"; }
c_ylw(){ printf '\033[33m%s\033[0m' "$*"; }
c_gry(){ printf '\033[90m%s\033[0m' "$*"; }

# ---------------------------------------------------------------- lifecycle -
begin() {   # begin <id> <priority> <description>
  CUR_ID="$1"; CUR_PRIO="$2"; CUR_DESC="$3"
  CUR_STATUS="PASS"; CUR_MSGS=(); CUR_START=$(date +%s)
  CUR_DIR="$EVROOT/$CUR_ID"
  if ! mkdir -p "$CUR_DIR"/{request,response,stdout,events,metrics} 2>/dev/null; then
    # plan §9.4: evidence that cannot be persisted invalidates the case
    CUR_STATUS="INVALID"
    CUR_MSGS+=("cannot create evidence dir $CUR_DIR")
  fi
  printf '  %-9s %-3s %s\n' "$CUR_ID" "$CUR_PRIO" "$CUR_DESC"
}

note() { CUR_MSGS+=("$*"); echo "      $(c_gry "· $*")"; }

fail() {
  [[ "$CUR_STATUS" == "INVALID" ]] || CUR_STATUS="FAIL"
  CUR_MSGS+=("FAIL: $*")
  echo "      $(c_red "✗ $*")"
}

ok() { echo "      $(c_grn "✓ $*")"; }

blocked() {
  # A recorded FAIL/INVALID must never be downgraded to BLOCKED — that would
  # let a failed P0 assertion end the test in a bucket the exit gate ignores,
  # and the run would exit 0. Mirror fail()'s stickiness.
  if [[ "$CUR_STATUS" == "FAIL" || "$CUR_STATUS" == "INVALID" ]]; then
    CUR_MSGS+=("BLOCKED (precondition lost, but $CUR_STATUS already stands): $*")
    echo "      $(c_ylw "⊘ $*  — keeping existing $CUR_STATUS, not downgrading")"
  else
    CUR_STATUS="BLOCKED"; CUR_MSGS+=("BLOCKED: $*")
    echo "      $(c_ylw "⊘ $*")"
  fi
}

invalid() {
  # Likewise, a real FAIL outranks INVALID: a failed assertion is more
  # actionable than "inconclusive", and evidence must not lose it.
  if [[ "$CUR_STATUS" == "FAIL" ]]; then
    CUR_MSGS+=("INVALID note (FAIL already stands): $*")
    echo "      $(c_ylw "! $*  — keeping existing FAIL, not downgrading")"
  else
    CUR_STATUS="INVALID"; CUR_MSGS+=("INVALID: $*")
    echo "      $(c_ylw "! $*")"
  fi
}

end() {
  local dur=$(( $(date +%s) - CUR_START ))
  case "$CUR_STATUS" in
    PASS)    PASS_N=$((PASS_N+1));    printf '      %s (%ss)\n' "$(c_grn PASS)" "$dur" ;;
    FAIL)    FAIL_N=$((FAIL_N+1));    printf '      %s (%ss)\n' "$(c_red FAIL)" "$dur" ;;
    BLOCKED) BLOCK_N=$((BLOCK_N+1));  printf '      %s (%ss)\n' "$(c_ylw BLOCKED)" "$dur" ;;
    INVALID) INVALID_N=$((INVALID_N+1)); printf '      %s (%ss)\n' "$(c_ylw INVALID)" "$dur" ;;
    SKIPPED) SKIP_N=$((SKIP_N+1)) ;;
  esac
  printf '%s\n' "${CUR_MSGS[@]:-}" | python3 -c '
import json, sys
msgs = sys.stdin.read()
rec = {"id": sys.argv[1], "priority": sys.argv[2], "status": sys.argv[3],
       "duration_s": int(sys.argv[4]), "description": sys.argv[5],
       "messages": msgs.strip(), "evidence": "tests/" + sys.argv[1]}
print(json.dumps(rec, ensure_ascii=False))
' "$CUR_ID" "$CUR_PRIO" "$CUR_STATUS" "$dur" "$CUR_DESC" >> "$RESULTS_JSONL"
  {
    echo "test_id=$CUR_ID"; echo "priority=$CUR_PRIO"; echo "status=$CUR_STATUS"
    echo "duration_s=$dur"; echo "description=$CUR_DESC"; echo "---"
    printf '%s\n' "${CUR_MSGS[@]:-}"
  } > "$CUR_DIR/stdout/result.txt" 2>/dev/null
  CUR_ID=""
}

# --------------------------------------------------------------- assertions -
assert_eq() {  # assert_eq <actual> <expected> <label>
  if [[ "$1" == "$2" ]]; then ok "$3 ($1)"; else fail "$3: expected '$2', got '$1'"; fi
}

assert_ne() {
  if [[ "$1" != "$2" ]]; then ok "$3"; else fail "$3: got forbidden value '$1'"; fi
}

assert_contains() {  # assert_contains <haystack> <needle> <label>
  if [[ "$1" == *"$2"* ]]; then ok "$3"; else fail "$3: '$2' not found in output"; fi
}

# assert_ne on a whole string passes spuriously when the haystack is a LIST
# (e.g. a node's taint keys): "a b c" != "b" is trivially true even though b is
# present. Absence of an element needs a substring test.
assert_not_contains() {  # assert_not_contains <haystack> <needle> <label>
  if [[ "$1" != *"$2"* ]]; then ok "$3"; else fail "$3: '$2' is still present in '$1'"; fi
}

# The workhorse for policy tests: the command MUST fail, and its error MUST
# mention the expected reason. A command that fails for an unrelated reason is
# not evidence that the policy works.
assert_rejected() {  # assert_rejected <expected-substring> <label> -- <cmd...>
  local want="$1" label="$2"; shift 3
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  printf '%s\n' "$out" > "$CUR_DIR/response/${label//[^a-zA-Z0-9]/_}.txt" 2>/dev/null
  if (( rc == 0 )); then
    fail "$label: command SUCCEEDED but should have been rejected"
    return
  fi
  # `want` may list alternatives separated by '|'. Several admission plugins
  # can legitimately reject the same object (PSA and a ValidatingAdmissionPolicy
  # both forbid hostNetwork, and only the first to fire is reported), so the
  # assertion accepts any of the acceptable reasons — but still refuses to
  # count an unrelated failure as a pass.
  local matched=0 alt
  while IFS= read -r alt; do
    [[ -n "$alt" && "$out" == *"$alt"* ]] && { matched=1; break; }
  done < <(printf '%s\n' "${want//|/$'\n'}")
  if (( matched )); then
    ok "$label (rejected: ${alt:0:52})"
  else
    fail "$label: rejected, but not for an expected reason. want one of [$want], got: ${out:0:200}"
  fi
}

assert_accepted() {  # assert_accepted <label> -- <cmd...>
  local label="$1"; shift 2
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  if (( rc == 0 )); then ok "$label"; else fail "$label: ${out:0:220}"; fi
}

# ------------------------------------------------------------------ helpers -
kap() { $K apply -f - ; }          # kubectl apply from stdin
kdel(){ $K delete -f - --ignore-not-found --wait=false >/dev/null 2>&1; }

# Poll until a jsonpath equals want, or time out. Returns 0/1.
wait_for() {  # wait_for <timeout_s> <want> <kubectl args...>
  local timeout="$1" want="$2"; shift 2
  local deadline=$(( $(date +%s) + timeout )) got
  while (( $(date +%s) < deadline )); do
    got="$($K "$@" 2>/dev/null)"
    [[ "$got" == "$want" ]] && return 0
    sleep 3
  done
  return 1
}

nown_phase() { $K get nodeownership "$1" -o jsonpath='{.status.phase}' 2>/dev/null; }

# Talk to the VAST mock from inside the cluster (it is ClusterIP-only and has
# no egress — by design, see networkpolicies.yaml).
mock_get() {  # mock_get <path>
  $K -n vast-mock exec deploy/vast-mock -- python3 -c "
import urllib.request,sys
print(urllib.request.urlopen('http://127.0.0.1:8080$1',timeout=8).read().decode())" 2>/dev/null
}

mock_post() {  # mock_post <path> <json>
  $K -n vast-mock exec deploy/vast-mock -- python3 -c "
import urllib.request,sys
req=urllib.request.Request('http://127.0.0.1:8080$1',method='POST')
req.add_header('Content-Type','application/json')
req.data=b'''$2'''
try: print(urllib.request.urlopen(req,timeout=8).read().decode())
except Exception as e: print('ERR',e)" 2>/dev/null
}

mock_field() {  # mock_field <machine> <field>
  mock_get "/v1/machines/$1" | python3 -c "import json,sys; print(json.load(sys.stdin)['$2'])" 2>/dev/null
}

# Prometheus / Alertmanager are ClusterIP-only, so query them from inside.
prom_q() {  # prom_q <promql>
  local enc; enc=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$1")
  $K -n monitoring exec deploy/prometheus -- \
    wget -qO- "http://127.0.0.1:9090/api/v1/query?query=$enc" 2>/dev/null
}

prom_alerts() {
  $K -n monitoring exec deploy/prometheus -- \
    wget -qO- "http://127.0.0.1:9090/api/v1/alerts" 2>/dev/null
}

am_alerts() {
  $K -n monitoring exec deploy/alertmanager -- \
    wget -qO- "http://127.0.0.1:9093/api/v2/alerts" 2>/dev/null
}

capture_events() {  # capture_events <object-name>
  $K get events -A --field-selector "involvedObject.name=$1" \
    -o custom-columns='TIME:.lastTimestamp,TYPE:.type,REASON:.reason,MSG:.message' \
    --no-headers > "$CUR_DIR/events/$1.txt" 2>/dev/null
}

# ------------------------------------------------------------------ reports -
write_reports() {
  local sumdir="$REPO/evidence/$RUN_ID/summary"
  mkdir -p "$sumdir"
  # A single-case debug run must NOT clobber the authoritative full-matrix
  # result. Only `all` owns results.json / junit.xml; everything else writes
  # a mode-suffixed file. Losing a completed matrix run to a one-off rerun is
  # an evidence-integrity failure, not a cosmetic one (plan §9.4).
  local suffix=""
  [[ "${MODE:-all}" == "all" ]] || suffix="-${MODE}"
  local total=$((PASS_N+FAIL_N+BLOCK_N+INVALID_N+SKIP_N))

  python3 - "$sumdir" "$PASS_N" "$FAIL_N" "$BLOCK_N" "$INVALID_N" "$RUN_ID" "$RESULTS_JSONL" "$suffix" "$SKIP_N" "$OVERLAY" <<'PYEOF' 
import json, sys, time, xml.etree.ElementTree as ET
sumdir, p, f, b, i, run_id, jsonl, suffix, s, overlay = sys.argv[1:11]
results = [json.loads(l) for l in open(jsonl) if l.strip()]

summary = {
  "run_id": run_id,
  "document_id": "ARISE-B300-PRELAB-DEPLOY-TEST-001",
  "waiver_id": "WAIVER-2026-08-11-001",
  "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "overlay": overlay,
  "totals": {"passed": int(p), "failed": int(f),
             "blocked": int(b), "invalid": int(i), "skipped_lab_only": int(s),
             "total": len(results)},
  # Named, not just counted: a skipped case is a claim NOT made on this
  # cluster, and the reader must be able to see which claims those are.
  "skipped_lab_only": [r["id"] for r in results if r["status"] == "SKIPPED"],
  "results": results,
}
# Plan §10.6: P0/P1 must be PASS and BLOCKED/INVALID/NOT-RUN must be zero.
p0p1_bad = [r["id"] for r in results
            if r["priority"] in ("P0","P1") and r["status"] not in ("PASS", "SKIPPED")]
summary["phase_a_exit_gate"] = {
  "satisfied": not p0p1_bad,
  "blocking_cases": p0p1_bad,
  "rule": "plan 10.6 — all P0/P1 PASS, zero BLOCKED/INVALID/NOT-RUN; "
          "lab-only simulation cases are SKIPPED on dgx and listed above",
}
json.dump(summary, open(f"{sumdir}/results{suffix}.json","w"), indent=2, ensure_ascii=False)

suite = ET.Element("testsuite", name="arise-b300-phase-a",
                   tests=str(len(results)), failures=str(f),
                   skipped=str(int(b)+int(i)), time="0")
for r in results:
    tc = ET.SubElement(suite, "testcase", classname=r["priority"],
                       name=f'{r["id"]} {r["description"]}',
                       time=str(r["duration_s"]))
    if r["status"] == "FAIL":
        ET.SubElement(tc, "failure", message=r["messages"][:400]).text = r["messages"]
    elif r["status"] in ("BLOCKED","INVALID"):
        ET.SubElement(tc, "skipped", message=r["status"]).text = r["messages"]
ET.ElementTree(suite).write(f"{sumdir}/junit{suffix}.xml", encoding="utf-8",
                            xml_declaration=True)
print(f"  results{suffix}.json + junit{suffix}.xml -> {sumdir}")
PYEOF

  echo
  printf '  PASS=%s  FAIL=%s  BLOCKED=%s  INVALID=%s  SKIPPED(lab-only)=%s  (total %s, overlay %s)\n' \
    "$(c_grn $PASS_N)" "$(c_red $FAIL_N)" "$(c_ylw $BLOCK_N)" "$(c_ylw $INVALID_N)" "$(c_ylw $SKIP_N)" "$total" "$OVERLAY"
}
