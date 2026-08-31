#!/usr/bin/env bash
# ============================================================================
# gate-selftest.sh — does the render gate actually FAIL on a bad manifest?
#
# A gate is its own detector, so nothing normally proves its assertions work:
# delete one and every run still says PASS. The falsification audit of
# 2026-08-30 named this ("the mechanism cannot detect its own removal"), and
# this script is the answer: copy the tracked tree into a temp dir, apply one
# mutation at a time, and require dgx-render-check.sh to REJECT each one.
#
# A mutation that the gate accepts is printed as a HOLE — that is the finding.
# Run it after touching platform/overlays/dgx or the gate itself:
#   scripts/gate-selftest.sh
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$REPO"
git ls-files -z | xargs -0 tar -cf - | (cd "$WORK" && tar -xf -)
cp "$REPO/.run_id" "$WORK/.run_id" 2>/dev/null || echo SELFTEST > "$WORK/.run_id"

pass=0; holes=0
mutate() {  # mutate <name> <file> <python-expression-on-text>
  local name="$1" file="$2" expr="$3"
  ( cd "$WORK" && git ls-files >/dev/null 2>&1 || true
    cp "$file" "$file.orig"
    python3 - "$file" "$expr" <<'PY'
import sys, pathlib
p, expr = pathlib.Path(sys.argv[1]), sys.argv[2]
t = p.read_text()
old, new = expr.split("=>", 1)
if old not in t:
    print(f"SELFTEST-SETUP-FAILED: {old[:60]!r} not in {p}", file=sys.stderr); sys.exit(2)
p.write_text(t.replace(old, new, 1))
PY
  ) || { printf '  \033[31mSTALE\033[0m %-46s could not stage the mutation — this self-test no longer matches the repo\n' "$name"
         holes=$((holes+1)); return; }
  local out rc
  out=$(cd "$WORK" && ./scripts/dgx-render-check.sh 2>&1); rc=$?
  mv "$WORK/$file.orig" "$WORK/$file"
  if (( rc != 0 )); then
    printf '  \033[32mOK\033[0m   %-46s gate rejected it\n' "$name"; pass=$((pass+1))
  else
    printf '  \033[31mHOLE\033[0m %-46s gate ACCEPTED a bad manifest\n' "$name"; holes=$((holes+1))
  fi
}

echo "=== render-gate self-test (mutations must be REJECTED) ==="
mutate "unpinned image (tag instead of digest)" platform/overlays/dgx/monitoring.yaml \
  'image: prom/prometheus@sha256:=>image: prom/prometheus:v3.0.0  # sha256:'
mutate "simulated GPU request leaks into a dgx workload" platform/overlays/dgx/deployments.yaml \
  'requests: { cpu: 100m, memory: 128Mi }=>requests: { cpu: 100m, memory: 128Mi, arise.dev/fake-gpu: "1" }'
mutate "direct-customer queue becomes reclaimable" platform/overlays/dgx/volcano-queues.yaml \
  'reclaimable: false=>reclaimable: true'
mutate "system queue GPU cap raised" platform/overlays/dgx/volcano-queues.yaml \
  'nvidia.com/gpu: "4"=>nvidia.com/gpu: "40"'
mutate "tenant GPU quota raised to the whole fleet" platform/overlays/dgx/kustomization.yaml \
  'requests.nvidia.com/gpu: "16"=>requests.nvidia.com/gpu: "32"'
mutate "tenant GPU quota key removed" platform/overlays/dgx/kustomization.yaml \
  'requests.nvidia.com/gpu: "24"=>requests.arise.dev/removed: "24"'
mutate "kubeadm k8s version drifts from versions.env" infra/dgx/kubeadm-cluster-config.yaml \
  'kubernetesVersion: "v1.36.2"=>kubernetesVersion: "v1.35.0"'
mutate "vendored CNI checksum drifts" versions.env \
  'CALICO_MANIFEST_SHA256=36163=>CALICO_MANIFEST_SHA256=00000'
mutate "pod CIDR disagrees with versions.env" infra/dgx/kubeadm-cluster-config.yaml \
  'podSubnet: "10.244.0.0/16"=>podSubnet: "10.245.0.0/16"'

echo
if (( holes )); then
  echo "SELF-TEST FAILED: $holes mutation(s) the gate did not catch OR could not be staged, $pass caught"
  exit 1
fi
echo "SELF-TEST PASSED: all $pass mutations rejected by the gate"
