#!/usr/bin/env bash
# ============================================================================
# validate.sh — L0 static checks (plan §10.1 "格式、schema、策略、依赖锁定")
#
# Runs with no cluster and no Docker, so it is the gate that can be enforced
# from the first commit onward. Sections:
#   1. every YAML parses; k8s-looking docs have apiVersion/kind/metadata.name
#   2. no floating image tags (:latest; digests preferred)     [DEP-01]
#   3. no nvidia.com/gpu anywhere in the lab overlay           [SEC-03]
#   4. no plaintext credentials                                [SEC-07]
#   5. every shell script passes `bash -n`
#   6. version lock completeness (versions.env)
#   7. node-map single source of truth
#   8. i18n locale parity (vue-i18n zh/en)
#   9. capacity-controller adapter-mode unit tests (no cluster)
#  10. gateway public-mode security unit tests (no cluster)
#  11. tenant register (platform/tenants.yaml) vs every consumer
#  12. tests carry no physical node names and honour KUBE_CONTEXT
# The dgx overlay has its own static gate: `make dgx-render`
# (scripts/dgx-render-check.sh).
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
FAIL=0
red(){ printf '\033[31m%s\033[0m\n' "$*"; FAIL=1; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }

echo "=== 1/12 YAML parse + k8s shape ==="
python3 - <<'PY' || FAIL=1
import sys, pathlib, yaml
bad = 0
for p in sorted(pathlib.Path('.').rglob('*.y*ml')):
    if 'node_modules' in str(p) or '/dist/' in str(p):
        continue
    if 'evidence/' in str(p):
        continue
    try:
        docs = list(yaml.safe_load_all(p.read_text()))
    except Exception as e:
        print(f"  PARSE FAIL {p}: {e}"); bad = 1; continue
    for i, d in enumerate(docs):
        if not isinstance(d, dict):
            continue
        # k8s-shaped docs must be complete
        if 'apiVersion' in d or 'kind' in d:
            for f in ('apiVersion', 'kind'):
                if f not in d:
                    print(f"  SHAPE FAIL {p} doc{i}: missing {f}"); bad = 1
            md = d.get('metadata') or {}
            # Config FILES, not API objects — they carry no metadata.name by
            # design: kind's Cluster, our NodeMap, kustomize's Kustomization,
            # and kubeadm's three (InitConfiguration / ClusterConfiguration /
            # KubeletConfiguration, consumed by `kubeadm init --config`, never
            # by an API server).
            exempt = ('Cluster', 'NodeMap', 'Kustomization',
                      'InitConfiguration', 'ClusterConfiguration',
                      'KubeletConfiguration', 'JoinConfiguration')
            if d.get('kind') not in exempt and not md.get('name'):
                print(f"  SHAPE FAIL {p} doc{i}: missing metadata.name"); bad = 1
    print(f"  ok {p} ({len(docs)} doc(s))")
sys.exit(bad)
PY
[[ $FAIL -eq 0 ]] && grn "  YAML ok" || red "  YAML failures above"

echo
echo "=== 2/12 image pinning (no :latest, digests preferred) ==="
if grep -rnE 'image:\s*\S+:latest' --include='*.yaml' --include='*.yml' --exclude-dir=node_modules . 2>/dev/null | grep -v evidence/; then
  red "  floating :latest tag found (plan §5.3 forbids)"
else
  grn "  no :latest tags"
fi
echo "  images referenced:"
grep -rhoE 'image:\s*\S+' --include='*.yaml' --include='*.yml' --exclude-dir=node_modules . 2>/dev/null \
  | grep -v evidence/ | sed 's/image:\s*/    /' | sort -u || echo "    (none yet)"

echo
echo "=== 3/12 no real GPU resource in lab overlay (SEC-03) ==="
# admission-policies.yaml is the ONE file allowed to name nvidia.com/gpu,
# because denying it is that file's whole job. Anywhere else is a defect.
STRAY=$(grep -rln 'nvidia\.com/gpu' platform/base platform/overlays/lab \
          controller mock 2>/dev/null \
        | grep -v 'platform/overlays/lab/admission-policies.yaml' || true)
if [[ -n "$STRAY" ]]; then
  red "  nvidia.com/gpu referenced outside the deny policy:"
  echo "$STRAY" | sed 's/^/    /'
else
  grn "  clean — only arise.dev/fake-gpu is used"
fi
# ...and the deny policy must actually be present, or SEC-03 has no enforcer.
if grep -q "name: arise-deny-real-gpu" platform/overlays/lab/admission-policies.yaml 2>/dev/null; then
  grn "  deny policy 'arise-deny-real-gpu' present"
else
  red "  deny policy for nvidia.com/gpu is MISSING (SEC-03 unenforced)"
fi

echo
echo "=== 4/12 secret scan (SEC-07) ==="
# The scanner must not match its own pattern definition, hence --exclude of
# this file. Assembling the pattern from fragments also keeps it from tripping
# other scanners that read this repo.
PAT="(AKIA[0-9A-Z]{16}|aws_secret""_access_key\s*=|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer [A-Za-z0-9_.-]{24,}|x-api""-key:\s*\S+)"
if grep -rnEi "$PAT" --exclude-dir=evidence --exclude-dir=.git \
     --exclude-dir=node_modules --exclude-dir=dist \
     --exclude=validate.sh . 2>/dev/null; then
  red "  possible plaintext credential"
else
  grn "  no plaintext credentials found"
fi

echo
echo "=== 5/12 shell syntax ==="
for s in scripts/*.sh tests/*.sh; do
  [[ -e "$s" ]] || continue
  if bash -n "$s" 2>/dev/null; then echo "  ok $s"; else red "  SYNTAX FAIL $s"; bash -n "$s"; fi
done

echo
echo "=== 6/12 version lock completeness ==="
# shellcheck disable=SC1091
source versions.env
for v in KIND_VERSION KUBECTL_VERSION HELM_VERSION KIND_NODE_IMAGE VOLCANO_VERSION DOCKER_ENGINE_VERSION; do
  if [[ -z "${!v:-}" ]]; then red "  $v unset"; else echo "  $v=${!v}"; fi
done
[[ "$KIND_NODE_IMAGE" == *"@sha256:"* ]] \
  && grn "  kind node image pinned by digest" \
  || red "  kind node image MUST be pinned by digest (plan §4.1)"

echo
echo "=== 7/12 node-map single source of truth ==="
# kind/node-map.yaml is authoritative. The advertiser gets the same mapping via
# NODE_MAP_JSON in the Deployment. If those two ever disagree, capacity lands
# on the wrong logical node and every ownership assertion downstream is wrong.
python3 - <<'PY' || FAIL=1
import json, re, sys, yaml

nm = yaml.safe_load(open('kind/node-map.yaml'))
authoritative = {n['kindNode']: n['nodeId'] for n in nm['spec']['nodes']}

# Two consumers mirror the map: the advertiser Deployment and the device
# plugin DaemonSet. BOTH copies must agree with the authoritative file.
mirrors = {}
for fname, kinds in (('platform/overlays/lab/deployments.yaml', ('Deployment',)),
                     ('platform/overlays/lab/fake-gpu-plugin.yaml', ('DaemonSet',))):
    for d in yaml.safe_load_all(open(fname)):
        if not d or d.get('kind') not in kinds:
            continue
        for c in d['spec']['template']['spec']['containers']:
            for e in c.get('env', []) or []:
                if e.get('name') == 'NODE_MAP_JSON':
                    mirrors[f"{d['kind']}/{d['metadata']['name']}"] = \
                        json.loads(e['value'])
if not mirrors:
    print("  FAIL: NODE_MAP_JSON not found in any workload"); sys.exit(1)
bad = {who: m for who, m in mirrors.items() if m != authoritative}
if bad:
    print("  FAIL: node-map.yaml and NODE_MAP_JSON disagree")
    print(f"    node-map.yaml : {authoritative}")
    for who, m in bad.items():
        print(f"    {who} : {m}")
    sys.exit(1)
print(f"  ok  {len(authoritative)} nodes agree across "
      + ", ".join(sorted(mirrors)) + ": "
      + ", ".join(f"{k}->{v}" for k, v in sorted(authoritative.items())))

# per-node fake GPU count must match versions.env
env = dict(re.findall(r'^(\w+)=(.*)$', open('versions.env').read(), re.M))
per = int(env['FAKE_GPU_PER_NODE'])
total = int(env['FAKE_GPU_TOTAL'])
if per * len(authoritative) != total:
    print(f"  FAIL: {per} x {len(authoritative)} != FAKE_GPU_TOTAL={total}")
    sys.exit(1)
print(f"  ok  {per} per node x {len(authoritative)} nodes = {total}")
PY

echo "=== 8/12 i18n locale parity (vue-i18n zh/en) ==="
python3 scripts/i18n-check.py || FAIL=1

echo "=== 9/12 capacity-controller adapter modes (unit, no cluster) ==="
python3 tests/unit_adapter_modes.py || FAIL=1

echo "=== 10/12 gateway public-mode security (unit, no cluster) ==="
python3 tests/unit_gateway_security.py || FAIL=1

echo "=== 11/12 tenant register vs its consumers ==="
python3 scripts/tenant-check.py || FAIL=1

echo "=== 12/12 tests are cluster-portable (no physical node names) ==="
# The matrix must be runnable against the DGX cluster on day 0, which means no
# assertion may spell a kind node name. Logical ids resolve through node_for()
# (tests/lib.sh), which reads the labels label-nodes.sh applies. A hardcoded
# name is also a silent-pass risk: on a cluster where it does not exist,
# kubectl returns empty and an assertion comparing empty to empty passes.
if grep -nE '\$\{CLUSTER_NAME\}-(worker|control-plane)' tests/*.sh 2>/dev/null; then
  red "  a test spells a physical node name; use node_for <logical-id> instead"
else
  grn "  no physical node names in tests/"
fi
# The context must be overridable, or the suite is welded to the kind cluster.
for f in tests/lib.sh scripts/verify.sh scripts/label-nodes.sh; do
  if grep -q 'KUBE_CONTEXT' "$f"; then echo "  ok $f honours KUBE_CONTEXT";
  else red "  $f hardcodes the kube context"; fi
done

echo
if [[ $FAIL -eq 0 ]]; then grn "=== L0 VALIDATE: PASS ==="; else red "=== L0 VALIDATE: FAIL ==="; fi
exit $FAIL
