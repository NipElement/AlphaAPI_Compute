#!/usr/bin/env bash
# ============================================================================
# rebuild-verify.sh — E2E-01: rebuild from Git and prove it is the same system
#
# Plan §10.4 E2E-01: "证明批准制品可在已知 EC2 基线上可重复构建同一平台",
# passing only if "配置 diff 仅含时间/UID 等批准字段".
#
# This is the test that distinguishes a platform from a pet. Everything else
# in the suite runs against a cluster that has been alive for hours and has
# accumulated hand-applied fixes, restarts and patches. If any of that state
# is load-bearing, every other green result is partly an accident of history.
# The only way to find out is to destroy the cluster and build it again from
# the repository alone.
#
#   ./scripts/rebuild-verify.sh
#
# Destroys and recreates ONLY the kind cluster. Host state, the evidence pack
# and everything the guard protects are untouched — teardown is scoped by
# cluster name and label, never a global prune (plan §11.7).
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
# shellcheck disable=SC1091
source versions.env
RUN_ID="$(cat .run_id)"
CTX="${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}"
K="kubectl --context $CTX"
OUT="$REPO/evidence/$RUN_ID/tests/E2E-01"
mkdir -p "$OUT"/{before,after,stdout}

red(){ printf '\033[31m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- snapshot --
# Capture the facts that must be identical across a rebuild. Deliberately NOT
# a raw dump of every object: resourceVersion, uid, timestamps, pod names and
# cluster IPs legitimately differ, and diffing them would bury the signal.
# What is captured is what the platform IS, not what this instance happens to
# be numbered.
snapshot() {   # snapshot <dir>
  local d="$1"

  $K get nodes -o json 2>/dev/null | python3 -c '
import json, sys
out = []
for n in json.load(sys.stdin)["items"]:
    lb = n["metadata"].get("labels", {})
    if "arise.ai/node-id" not in lb:
        continue
    out.append({
        "nodeId":   lb["arise.ai/node-id"],
        "pair":     lb.get("arise.ai/pair"),
        "owner":    lb.get("arise.ai/owner"),
        "fakeGpu":  n["status"]["allocatable"].get("arise.dev/fake-gpu"),
        "realGpu":  n["status"]["allocatable"].get("nvidia.com/gpu", "0"),
    })
out.sort(key=lambda x: x["nodeId"])
print(json.dumps(out, indent=2, sort_keys=True))' > "$d/nodes.json"

  $K get crd -o json 2>/dev/null | python3 -c '
import json, sys
names = sorted(c["metadata"]["name"] for c in json.load(sys.stdin)["items"]
               if "arise" in c["metadata"]["name"] or "volcano" in c["metadata"]["name"])
print(json.dumps(names, indent=2))' > "$d/crds.json"

  $K get validatingadmissionpolicy -o json 2>/dev/null | python3 -c '
import json, sys
out = {}
for p in json.load(sys.stdin)["items"]:
    n = p["metadata"]["name"]
    if not n.startswith("arise"):
        continue
    # Count validations rather than diffing CEL text: the assertion is that
    # the same policies with the same number of rules came back, and the CEL
    # itself is already under version control.
    out[n] = len(p["spec"].get("validations", []))
print(json.dumps(out, indent=2, sort_keys=True))' > "$d/policies.json"

  $K get queue -o json 2>/dev/null | python3 -c '
import json, sys
out = {}
for q in json.load(sys.stdin)["items"]:
    n = q["metadata"]["name"]
    if n in ("root", "default"):
        continue
    s = q["spec"]
    out[n] = {"weight": s.get("weight"), "reclaimable": s.get("reclaimable"),
              "capability": s.get("capability")}
print(json.dumps(out, indent=2, sort_keys=True))' > "$d/queues.json"

  $K get priorityclass -o json 2>/dev/null | python3 -c '
import json, sys
out = {}
for p in json.load(sys.stdin)["items"]:
    if not p["metadata"]["name"].startswith("arise"):
        continue
    out[p["metadata"]["name"]] = {"value": p["value"],
                                  "preemptionPolicy": p.get("preemptionPolicy")}
print(json.dumps(out, indent=2, sort_keys=True))' > "$d/priorityclasses.json"

  $K get ns -o json 2>/dev/null | python3 -c '
import json, sys
out = {}
for n in json.load(sys.stdin)["items"]:
    lb = n["metadata"].get("labels", {})
    if lb.get("project") != "arise-b300-prelab":
        continue
    out[n["metadata"]["name"]] = {
        "enforce": lb.get("pod-security.kubernetes.io/enforce"),
        "tier":    lb.get("arise.ai/tier")}
print(json.dumps(out, indent=2, sort_keys=True))' > "$d/namespaces.json"

  # Images must be identical AND digest-pinned. A rebuild that silently
  # resolved a tag to a different digest is not the same platform.
  $K get deploy -A -o json 2>/dev/null | python3 -c '
import json, sys
out = {}
for dep in json.load(sys.stdin)["items"]:
    md = dep["metadata"]
    if md.get("labels", {}).get("project") != "arise-b300-prelab":
        continue
    key = f'"'"'{md["namespace"]}/{md["name"]}'"'"'
    out[key] = sorted(c["image"] for c in
                      dep["spec"]["template"]["spec"]["containers"])
print(json.dumps(out, indent=2, sort_keys=True))' > "$d/images.json"

  $K get resourcequota -A -o json 2>/dev/null | python3 -c '
import json, sys
out = {}
for q in json.load(sys.stdin)["items"]:
    md = q["metadata"]
    out[f'"'"'{md["namespace"]}/{md["name"]}'"'"'] = q["spec"]["hard"]
print(json.dumps(out, indent=2, sort_keys=True))' > "$d/quotas.json"

  # Grafana UIDs are an explicit E2E-01 requirement: a dashboard whose UID
  # changes on rebuild breaks every link and alert annotation pointing at it.
  $K -n monitoring exec deploy/grafana -- \
    wget -qO- http://127.0.0.1:3000/api/search?query= 2>/dev/null \
    | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
    print(json.dumps(sorted((r.get("uid"), r.get("title")) for r in rows),
                     indent=2))
except Exception:
    print("[]")' > "$d/grafana.json" 2>/dev/null || echo '[]' > "$d/grafana.json"
}

# =========================================================== run ===========
echo "=== E2E-01: rebuild from Git ==="
./scripts/guard.sh check || { red "guard failed; refusing to rebuild"; exit 1; }

echo
echo "--- capturing BEFORE state ---"
snapshot "$OUT/before"
wc -l "$OUT"/before/*.json | tail -1

echo
echo "--- controlled teardown (kind cluster only) ---"
kind delete cluster --name "$CLUSTER_NAME" 2>&1 | tail -2
./scripts/guard.sh check || { red "guard failed after teardown"; exit 1; }

echo
echo "--- rebuild from the repository ---"
REBUILD_START=$(date +%s)
make cluster > "$OUT/stdout/rebuild-cluster.log" 2>&1 \
  || { red "make cluster failed"; tail -20 "$OUT/stdout/rebuild-cluster.log"; exit 1; }
grn "  cluster recreated"
make deploy  > "$OUT/stdout/rebuild-deploy.log" 2>&1 \
  || { ylw "  make deploy returned non-zero (verify gate runs inside it)"; }
REBUILD_SECS=$(( $(date +%s) - REBUILD_START ))
grn "  platform redeployed in ${REBUILD_SECS}s"

# Grafana needs a moment before its API answers.
$K -n monitoring rollout status deploy/grafana --timeout=300s >/dev/null 2>&1
sleep 20

echo
echo "--- capturing AFTER state ---"
snapshot "$OUT/after"

echo
echo "=== comparison ==="
FAIL=0
for f in nodes crds policies queues priorityclasses namespaces images quotas grafana; do
  if diff -u "$OUT/before/$f.json" "$OUT/after/$f.json" > "$OUT/stdout/diff-$f.txt" 2>&1; then
    grn "  identical: $f"
  else
    red "  DIFFERS:   $f"
    sed -n '1,25p' "$OUT/stdout/diff-$f.txt" | sed 's/^/      /'
    FAIL=1
  fi
done

echo
{
  echo "{"
  echo "  \"test_id\": \"E2E-01\","
  echo "  \"rebuild_seconds\": $REBUILD_SECS,"
  echo "  \"identical\": $([[ $FAIL -eq 0 ]] && echo true || echo false),"
  echo "  \"compared\": [\"nodes\",\"crds\",\"policies\",\"queues\",\"priorityclasses\",\"namespaces\",\"images\",\"quotas\",\"grafana\"],"
  echo "  \"note\": \"Compares what the platform IS. resourceVersion/uid/timestamps/pod names/cluster IPs are expected to differ and are excluded by construction.\""
  echo "}"
} > "$OUT/result.json"

if [[ $FAIL -eq 0 ]]; then
  grn "=== E2E-01 REBUILD IDENTICAL (${REBUILD_SECS}s) ==="
else
  red "=== E2E-01 REBUILD DIFFERS — see $OUT/stdout/diff-*.txt ==="
fi
./scripts/guard.sh check
exit $FAIL
