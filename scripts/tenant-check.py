#!/usr/bin/env python3
"""Tenant register consistency gate (validate.sh §11) — no cluster needed.

platform/tenants.yaml is authoritative. Onboarding a customer touches roughly
a dozen files, and the dangerous failure is a MISSED edit rather than a wrong
one: before 2026-08-27 a tenant namespace absent from the platform-ingress
fence's enumerated list could reach the internal APIs by pod IP and bypass
gateway authentication entirely. This asserts every consumer agrees with the
register, so a missed edit fails static validation instead of shipping.

Checks, per registered tenant:
  1. Namespace exists with arise.ai/tier=tenant, arise.ai/tenant=<short>, and
     PSA restricted (the tier label is what every admission binding and the
     platform-ingress fence select on).
  2. ResourceQuota and LimitRange exist for the namespace.
  3. All three NetworkPolicies exist (default-deny-all, allow-dns,
     deny-imds-and-host-links).
  4. tenant-runner ServiceAccount + Role + RoleBinding exist.
  5. Portal RBAC (Role + RoleBinding) exists in BOTH overlays.
  6. The Volcano queue exists, and the queue-binding CEL map binds the
     namespace to exactly that queue — in BOTH overlays.
  7. The portal's TENANTS and PRIORITIES agree with the register.
  8. The gateway seeds an account for the tenant.
And globally:
  9. The platform-ingress fence excludes tenants BY LABEL (not only by an
     enumerated name list, which cannot cover a tenant added later).
 10. All FOUR services that reason about tenants (portal, gateway,
     capacity-controller, ops-console) read the mounted register and carry a
     matching fallback. The controller's list is the one with teeth: a tenant
     missing from it is invisible to the drain.
"""
import ast
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
FAILS = []


def fail(msg):
    FAILS.append(msg)


def docs(rel):
    p = REPO / rel
    if not p.exists():
        fail(f"missing file: {rel}")
        return []
    return [d for d in yaml.safe_load_all(p.read_text()) if d]


def find(ds, kind, name, ns=None):
    for d in ds:
        if d.get("kind") != kind:
            continue
        md = d.get("metadata", {})
        if md.get("name") != name:
            continue
        if ns is not None and md.get("namespace") != ns:
            continue
        return d
    return None


reg = yaml.safe_load((REPO / "platform/tenants.yaml").read_text())
tenants = reg["spec"]["tenants"]

ns_docs = docs("platform/base/namespaces.yaml")
quota_docs = docs("platform/base/quotas.yaml")
netpol_docs = docs("platform/base/networkpolicies.yaml")
rbac_docs = docs("platform/base/rbac.yaml")
queue_docs = docs("platform/overlays/lab/volcano-queues.yaml")
portal_src = (REPO / "services/tenant-portal/tenant_portal.py").read_text()
gateway_src = (REPO / "services/gateway/gateway.py").read_text()
controller_src = (REPO
                  / "services/capacity-controller/capacity_controller.py").read_text()
console_src = (REPO / "services/ops-console/console.py").read_text()

OVERLAYS = ("lab", "dgx")

for t in tenants:
    ns, short = t["namespace"], t["short"]

    # 1. namespace + the labels every gate selects on
    n = find(ns_docs, "Namespace", ns)
    if not n:
        fail(f"{ns}: no Namespace in platform/base/namespaces.yaml")
    else:
        lb = n["metadata"].get("labels", {})
        if lb.get("arise.ai/tier") != "tenant":
            fail(f"{ns}: arise.ai/tier must be 'tenant' (every admission "
                 f"binding and the platform-ingress fence select on it); "
                 f"found {lb.get('arise.ai/tier')!r}")
        if lb.get("arise.ai/tenant") != short:
            fail(f"{ns}: arise.ai/tenant={lb.get('arise.ai/tenant')!r}, "
                 f"register says {short!r}")
        if lb.get("pod-security.kubernetes.io/enforce") != "restricted":
            fail(f"{ns}: PSA enforce must be 'restricted' for a tenant")

    # 2. quota + limit range
    if not find(quota_docs, "ResourceQuota", f"{ns}-quota", ns):
        fail(f"{ns}: no ResourceQuota named {ns}-quota")
    if not find(quota_docs, "LimitRange", f"{ns}-limits", ns):
        fail(f"{ns}: no LimitRange named {ns}-limits (without it a Pod with no "
             f"requests escapes quota accounting)")

    # 3. the three fences
    for pol in ("default-deny-all", "allow-dns", "deny-imds-and-host-links"):
        if not find(netpol_docs, "NetworkPolicy", pol, ns):
            fail(f"{ns}: missing NetworkPolicy {pol}")

    # 4. tenant identity
    if not find(rbac_docs, "ServiceAccount", "tenant-runner", ns):
        fail(f"{ns}: no tenant-runner ServiceAccount")
    if not find(rbac_docs, "Role", "arise:tenant-runner", ns):
        fail(f"{ns}: no arise:tenant-runner Role")
    if not find(rbac_docs, "RoleBinding", "arise:tenant-runner", ns):
        fail(f"{ns}: no arise:tenant-runner RoleBinding")

    # 5. portal reach, in BOTH overlays (a tenant the portal cannot act in is
    #    a tenant whose customer sees an empty console)
    for ov in OVERLAYS:
        pd = docs(f"platform/overlays/{ov}/tenant-portal.yaml")
        if not find(pd, "Role", "arise:tenant-portal", ns):
            fail(f"{ns}: no arise:tenant-portal Role in the {ov} overlay")
        if not find(pd, "RoleBinding", "arise:tenant-portal", ns):
            fail(f"{ns}: no arise:tenant-portal RoleBinding in the {ov} overlay")

    # 6. entitlement: the queue object, and the namespace label the binding
    #    policy reads. The label IS the binding — a tenant whose label is wrong
    #    or missing silently falls back to the 'default' queue: no weight, no
    #    reclaim protection, i.e. a paying customer's work becomes reclaimable.
    if not find(queue_docs, "Queue", t["queue"]):
        fail(f"{ns}: queue {t['queue']} is not defined in volcano-queues.yaml")
    if n:
        lq = n["metadata"].get("labels", {}).get("arise.ai/queue")
        if lq != t["queue"]:
            fail(f"{ns}: namespace label arise.ai/queue={lq!r}, register says "
                 f"{t['queue']!r} — the queue-binding policy reads this label, "
                 f"so a mismatch downgrades this tenant to the default queue")
    for ov in OVERLAYS:
        qb = find(docs(f"platform/overlays/{ov}/queue-binding-policy.yaml"),
                  "ValidatingAdmissionPolicy", "arise-queue-binding")
        if not qb:
            fail(f"{ov}: arise-queue-binding policy missing")
            continue
        vars_ = {v["name"]: v["expression"] for v in qb["spec"]["variables"]}
        if "arise.ai/queue" not in vars_.get("nsQueue", ""):
            fail(f"{ov}: queue binding is not label-driven (no nsQueue variable "
                 f"reading arise.ai/queue) — every new tenant would need a CEL "
                 f"edit in this file, and forgetting it is silent")
        # The tenant must NOT also be enumerated: a hardcoded entry would win
        # over the label, leaving the label decorative and never exercised.
        if re.search(r"'" + re.escape(ns) + r"'\s*:", vars_.get("allowed", "")):
            fail(f"{ns}: still enumerated in the {ov} queue-binding allow-list; "
                 f"that path shadows the label, so the label is untested")

    # 7. the portal's own view must match
    m = re.search(r"^TENANTS\s*=\s*(\{.*?^\})", portal_src, re.S | re.M)
    if not m:
        fail("tenant-portal: TENANTS dict not found")
    else:
        pt = ast.literal_eval(m.group(1))
        if ns not in pt:
            fail(f"{ns}: absent from the portal's TENANTS — the portal would "
                 f"refuse every request for it")
        else:
            if pt[ns].get("queue") != t["queue"]:
                fail(f"{ns}: portal queue {pt[ns].get('queue')!r} != register "
                     f"{t['queue']!r}")
            if pt[ns].get("owner") != t["owner"]:
                fail(f"{ns}: portal owner {pt[ns].get('owner')!r} != register "
                     f"{t['owner']!r}")
    m = re.search(r"^PRIORITIES\s*=\s*(\{.*?\})\s*$", portal_src, re.S | re.M)
    if not m:
        fail("tenant-portal: PRIORITIES dict not found")
    else:
        pr = ast.literal_eval(m.group(1))
        if pr.get(ns) != t["priorities"]:
            fail(f"{ns}: portal priorities {pr.get(ns)} != register "
                 f"{t['priorities']}")

    # 7b. the gateway's fallback tenant list (used when the register ConfigMap
    #     is not mounted) must agree too, or an unmounted pod would refuse to
    #     bind a new customer's account to their own namespace.
    m = re.search(r"^VALID_TENANTS\s*=\s*(\[[^\]]*\])", gateway_src, re.M)
    if not m:
        fail("gateway: VALID_TENANTS fallback not found")
    elif ns not in ast.literal_eval(m.group(1)):
        fail(f"{ns}: absent from the gateway's VALID_TENANTS fallback")

    # 7c. the DRAIN path's view of "a tenant". This one is not cosmetic: a
    #     tenant missing from TENANT_NAMESPACES is INVISIBLE to the drain, so
    #     that customer's pods keep running on a node being handed to the
    #     marketplace or returned to the ARISE pool — still executing on
    #     hardware sold to someone else.
    for src, name in ((controller_src, "capacity-controller"),
                      (console_src, "ops-console")):
        m = re.search(r"^TENANT_NAMESPACES\s*=\s*(\([^)]*\))", src, re.M)
        if not m:
            fail(f"{name}: TENANT_NAMESPACES fallback not found")
        elif ns not in ast.literal_eval(m.group(1)):
            fail(f"{ns}: absent from {name}'s TENANT_NAMESPACES fallback — its "
                 f"pods would not be drained on an ownership handover")

    # 8. an account that can actually log in
    if f'"{t["gatewayAccount"]}"' not in gateway_src:
        fail(f"{ns}: gateway seeds no account {t['gatewayAccount']!r}")
    else:
        acct = re.search(r'add_user\("' + re.escape(t["gatewayAccount"])
                         + r'".*?"(tenant-[a-z-]+)"', gateway_src, re.S)
        if not acct or acct.group(1) != ns:
            fail(f"{ns}: gateway account {t['gatewayAccount']!r} is not bound "
                 f"to this namespace")

# 9. the fence must be keyed on the LABEL, not only an enumerated name list —
#    otherwise it is fail-open for every tenant added after it was written.
fence = find(netpol_docs, "NetworkPolicy", "platform-internal-ingress",
             "platform-system")
if not fence:
    fail("platform-internal-ingress NetworkPolicy missing")
else:
    exprs = []
    for rule in fence["spec"].get("ingress", []):
        for src in rule.get("from", []):
            sel = src.get("namespaceSelector") or {}
            exprs += sel.get("matchExpressions", [])
    if not any(e.get("key") == "arise.ai/tier"
               and e.get("operator") == "NotIn"
               and "tenant" in (e.get("values") or []) for e in exprs):
        fail("platform-internal-ingress does not exclude tenants BY LABEL "
             "(arise.ai/tier NotIn [tenant]). An enumerated name list alone is "
             "fail-open: a tenant onboarded later is not in it and would reach "
             "the internal APIs by pod IP, bypassing gateway auth.")

# 10. both services must actually READ the mounted register, not just carry a
#     fallback — otherwise the ConfigMap is decorative and onboarding still
#     needs a code change.
for src, name, fn in ((portal_src, "tenant-portal", "_load_tenants"),
                      (gateway_src, "gateway", "load_tenants"),
                      (controller_src, "capacity-controller",
                       "load_tenant_namespaces"),
                      (console_src, "ops-console", "load_tenant_namespaces")):
    if "/etc/arise/tenants.json" not in src:
        fail(f"{name}: does not read the mounted tenant register")
    if re.search(rf"^\s*{fn}\(\)", src, re.M) is None:
        fail(f"{name}: {fn}() is defined but never called at startup")

print(f"tenant register: {len(tenants)} tenant(s) "
      f"({', '.join(t['namespace'] for t in tenants)})")
if FAILS:
    for f_ in FAILS:
        print(f"  FAIL {f_}")
    print(f"FAIL: {len(FAILS)} inconsistenc(ies) between the register and its consumers")
    sys.exit(1)
print("  ok  every consumer agrees with platform/tenants.yaml")
