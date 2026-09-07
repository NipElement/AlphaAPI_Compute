#!/usr/bin/env bash
# Explicit optional gateway deployment. This is never part of ordinary platform reapply.
set -euo pipefail
cd "$(dirname "$0")/.."
overlay=${1:?overlay required}
context=${2:?kube context required}
keys=${3:-platform/access/keys.yaml}
if [[ "$overlay" == lab ]]; then
  [[ "$context" == kind-* ]] || { echo 'lab access requires a kind context' >&2; exit 1; }
  ./scripts/guard.sh check
fi
manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
python3 scripts/render-access.py --overlay "$overlay" --keys "$keys" --ready > "$manifest"
kubectl --context "$context" apply --server-side --field-manager=arise-tenant-access --force-conflicts -f "$manifest"
replicas=$(python3 - "$manifest" <<'PYTHON'
import sys, yaml
print(next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d['kind'] == 'Deployment')['spec']['replicas'])
PYTHON
)
if [[ "$replicas" == 0 ]]; then
  kubectl --context "$context" -n access-system wait --for=delete pod -l app.kubernetes.io/name=tenant-bastion --timeout=60s
else
  kubectl --context "$context" -n access-system rollout status deployment/tenant-bastion --timeout=180s
fi
# Config changes trigger Recreate via a hash of the code and authorized keys.
# Removed tenants' old ingress rules are harmless without bastion egress or an
# authorized key; prune only these two component-owned policies, never PVCs.
python3 - "$manifest" "$context" <<'PY'
import json, subprocess, sys, yaml
wanted = {(d['metadata'].get('namespace'), d['metadata']['name'])
          for d in yaml.safe_load_all(open(sys.argv[1])) if d and d['kind'] == 'NetworkPolicy'}
k = ['kubectl', '--context', sys.argv[2]]
existing = json.loads(subprocess.check_output(k + ['get', 'networkpolicy', '-A', '-l',
    'arise.ai/component=tenant-access', '-o', 'json'], text=True))
for policy in existing['items']:
    md = policy['metadata']
    if (md['namespace'], md['name']) not in wanted:
        subprocess.run(k + ['-n', md['namespace'], 'delete', 'networkpolicy', md['name']], check=True)
PY
if [[ "$overlay" == lab ]]; then ./scripts/guard.sh check; fi
