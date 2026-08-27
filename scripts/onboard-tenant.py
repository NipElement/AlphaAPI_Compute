#!/usr/bin/env python3
"""Emit every Kubernetes object a registered tenant needs.

USAGE:  scripts/onboard-tenant.py <namespace> [--profile customer|internal]

Onboarding used to mean coordinated edits across ~12 places, and the dangerous
failure was a MISSED edit rather than a wrong one (a namespace absent from the
platform-ingress fence could reach the internal APIs by pod IP and bypass
gateway auth; one absent from the queue-binding CEL silently lost its reclaim
protection). Two changes removed most of that surface:

  - the ingress fence and the queue binding now key on NAMESPACE LABELS
    (arise.ai/tier, arise.ai/queue), so a correctly-labelled tenant is fenced
    and entitled by construction;
  - the portal and gateway read platform/tenants.yaml from a mounted
    ConfigMap, so their tenant lists are regenerated, not edited.

What remains is the per-tenant Kubernetes objects, which this script writes.
The workflow is:

  1. add the tenant to platform/tenants.yaml
  2. scripts/onboard-tenant.py tenant-<name> > platform/base/tenant-<name>.yaml
  3. add that file to platform/base/kustomization.yaml
  4. review the quota numbers (they are DEFAULTS, not a contract)
  5. make validate     # §11 tells you if anything is still out of step
  6. make deploy / make dgx-deploy
  7. create the customer's account in the console (admin -> 用户管理)

Nothing here prints or invents a credential.
"""
import argparse
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

# Quota DEFAULTS by profile. Deliberately conservative: a quota is a ceiling,
# and raising one after a contract is signed is a one-line change, while
# handing a new tenant the whole fleet on day one is not recoverable.
PROFILES = {
    # A dedicated-node customer: one B300 node's worth of GPU, with CPU/memory
    # matching the real per-node magnitudes (8 GPU, 256 threads, 2 TiB).
    "customer": {
        "requests.nvidia.com/gpu": "8",
        "requests.cpu": "256", "requests.memory": "2048Gi",
        "limits.cpu": "512", "limits.memory": "4096Gi",
        "pods": "50", "count/services": "10",
        "count/persistentvolumeclaims": "10",
        "requests.storage": "30Ti",
        "arise-shared.storageclass.storage.k8s.io/requests.storage": "24Ti",
        "arise-longterm.storageclass.storage.k8s.io/requests.storage": "12Ti",
    },
    "internal": {
        "requests.nvidia.com/gpu": "8",
        "requests.cpu": "256", "requests.memory": "2048Gi",
        "limits.cpu": "512", "limits.memory": "4096Gi",
        "pods": "50", "count/services": "10",
        "count/persistentvolumeclaims": "10",
        "requests.storage": "10Ti",
        "arise-shared.storageclass.storage.k8s.io/requests.storage": "8Ti",
        "arise-longterm.storageclass.storage.k8s.io/requests.storage": "4Ti",
    },
}

TENANT_ROLE_RULES = [
    {"apiGroups": [""], "resources": ["pods", "pods/log", "pods/status"],
     "verbs": ["get", "list", "watch", "create", "delete"]},
    {"apiGroups": ["batch"], "resources": ["jobs"],
     "verbs": ["get", "list", "watch", "create", "delete"]},
    {"apiGroups": ["scheduling.volcano.sh"], "resources": ["podgroups"],
     "verbs": ["get", "list", "watch", "create", "delete"]},
    {"apiGroups": [""], "resources": ["events"],
     "verbs": ["get", "list", "watch"]},
]

PORTAL_ROLE_RULES = [
    {"apiGroups": [""], "resources": ["pods"],
     "verbs": ["get", "list", "watch", "create", "delete"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["persistentvolumeclaims", "services"],
     "verbs": ["get", "list", "watch", "create", "delete"]},
    {"apiGroups": ["apps"], "resources": ["deployments"],
     "verbs": ["get", "list", "watch", "create", "delete"]},
    {"apiGroups": ["batch.volcano.sh"], "resources": ["jobs"],
     "verbs": ["get", "list", "watch", "create", "delete"]},
    {"apiGroups": [""], "resources": ["resourcequotas"], "verbs": ["get", "list"]},
    {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list"]},
]


def objects(t, profile, project):
    ns, short = t["namespace"], t["short"]
    quota = dict(PROFILES[profile])
    out = [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": ns, "labels": {
             "arise.ai/tier": "tenant",          # fences + admission bindings
             "arise.ai/tenant": short,
             "arise.ai/queue": t["queue"],       # entitlement, read by the VAP
             "project": project,
             "pod-security.kubernetes.io/enforce": "restricted",
             "pod-security.kubernetes.io/audit": "restricted",
             "pod-security.kubernetes.io/warn": "restricted"}}},
        {"apiVersion": "v1", "kind": "ResourceQuota",
         "metadata": {"name": f"{ns}-quota", "namespace": ns},
         "spec": {"hard": quota}},
        {"apiVersion": "v1", "kind": "LimitRange",
         "metadata": {"name": f"{ns}-limits", "namespace": ns},
         "spec": {"limits": [{
             "type": "Container",
             # On the quantization grid (flavor-policy): LimitRanger injects
             # these BEFORE the flavor gate validates, so off-grid defaults
             # would make the platform reject its own defaults.
             "default": {"cpu": "1", "memory": "1Gi"},
             "defaultRequest": {"cpu": "500m", "memory": "512Mi"},
             "max": {"cpu": "256", "memory": "2048Gi"},
             "min": {"cpu": "10m", "memory": "16Mi"}}]}},
        # --- the three fences ------------------------------------------------
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "default-deny-all", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "allow-dns", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{
             "to": [{"namespaceSelector": {"matchLabels": {
                         "kubernetes.io/metadata.name": "kube-system"}},
                     "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
             "ports": [{"protocol": "UDP", "port": 53},
                       {"protocol": "TCP", "port": 53}]}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "deny-imds-and-host-links", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{
             "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [
                 "169.254.0.0/16",   # IMDS + link-local
                 "172.31.0.0/16",    # host network
                 "10.96.0.0/12",     # service CIDR
                 "10.244.0.0/16",    # pod CIDR: tenants reach the platform
                                     # ONLY through the gateway, never pod-to-pod
             ]}}]}]}},
        # A tenant's own pods may talk to each other (its dev machine to its
        # online service). Intra-namespace only; SVC-01 proves the other
        # tenant stays fenced.
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "allow-intra-namespace", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                  "ingress": [{"from": [{"podSelector": {}}]}],
                  "egress": [{"to": [{"podSelector": {}}]}]}},
        # --- identities ------------------------------------------------------
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "tenant-runner", "namespace": ns,
                      "labels": {"project": project}}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": "arise:tenant-runner", "namespace": ns},
         "rules": TENANT_ROLE_RULES},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": "arise:tenant-runner", "namespace": ns},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                     "name": "arise:tenant-runner"},
         "subjects": [{"kind": "ServiceAccount", "name": "tenant-runner",
                       "namespace": ns}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": "arise:tenant-portal", "namespace": ns},
         "rules": PORTAL_ROLE_RULES},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": "arise:tenant-portal", "namespace": ns},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                     "name": "arise:tenant-portal"},
         "subjects": [{"kind": "ServiceAccount", "name": "tenant-portal",
                       "namespace": "platform-system"}]},
    ]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("namespace")
    ap.add_argument("--profile", choices=sorted(PROFILES), default=None,
                    help="quota profile (default: from the register's 'kind')")
    ap.add_argument("--project", default="arise-b300",
                    help="project label (arise-b300 for hardware, "
                         "arise-b300-prelab for the lab)")
    args = ap.parse_args()

    reg = yaml.safe_load((REPO / "platform/tenants.yaml").read_text())
    t = next((x for x in reg["spec"]["tenants"]
              if x["namespace"] == args.namespace), None)
    if not t:
        print(f"error: {args.namespace} is not in platform/tenants.yaml — add it "
              f"there first; that file is the source of truth.", file=sys.stderr)
        return 2
    profile = args.profile or ("internal" if t.get("kind") == "internal"
                               else "customer")

    # Foot-gun guard: the two rehearsed tenants already have hand-written
    # manifests in platform/base. Emitting a second definition for them and
    # applying it would give kustomize two objects with the same identity —
    # a build error at best, silent divergence at worst.
    base_ns = (REPO / "platform/base/namespaces.yaml").read_text()
    if f"name: {args.namespace}\n" in base_ns:
        print(f"warning: {args.namespace} already has manifests in "
              f"platform/base/namespaces.yaml. This output is for a NEW "
              f"tenant; applying it as well would define the same objects "
              f"twice. Use it as a reference only.", file=sys.stderr)

    print(f"# GENERATED by scripts/onboard-tenant.py for {t['namespace']}")
    print(f"# Register entry: kind={t.get('kind')} queue={t['queue']} "
          f"owner={t['owner']}")
    print(f"# Quota profile: {profile} — these numbers are DEFAULTS. Review "
          f"them against the signed contract before applying.")
    print("#")
    print("# The namespace labels below are load-bearing, not decoration:")
    print("#   arise.ai/tier=tenant   -> every admission binding + the")
    print("#                             platform-ingress fence select on it")
    print("#   arise.ai/queue=<queue> -> the queue-binding policy reads it;")
    print("#                             wrong or missing means this tenant")
    print("#                             silently falls back to the 'default'")
    print("#                             queue: no weight, no reclaim protection")
    print(yaml.dump_all(objects(t, profile, args.project), sort_keys=False,
                        default_flow_style=False, allow_unicode=True,
                        explicit_start=True), end="")
    print(f"""
# ---------------------------------------------------------------------------
# REMAINING STEPS (nothing else needs a code change):
#   1. save this into platform/base/tenant-{t['short']}.yaml
#   2. add it to platform/base/kustomization.yaml
#   3. make validate        # §11 verifies every consumer agrees
#   4. make deploy          # or: make dgx-deploy   (regenerates the register
#                           #      ConfigMap the portal and gateway read)
#   5. create the customer's account in the console: admin -> 用户管理,
#      role=user, tenant={t['namespace']}. No credential belongs in Git.
# ---------------------------------------------------------------------------""",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
