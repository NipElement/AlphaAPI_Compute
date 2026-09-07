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
  7. Existing lab fallback entries agree with the register. New customers
     require no code entry or seed account; runtime loaders are tested separately.
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
TIER = "arise.ai/tier"
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

def rendered(overlay="lab"):
    """Everything the overlay ACTUALLY applies, not four hardcoded files.

    Reading platform/base/{namespaces,quotas,networkpolicies,rbac}.yaml by
    name contradicted the procedure it was gating: customer-onboarding.md
    tells the operator to write the generated objects to
    platform/base/tenant-<name>.yaml and add THAT to the kustomization, which
    left every check below failing forever with messages naming files the
    operator was never told to touch (rehearsed end-to-end 2026-08-31 — the
    tenant applied cleanly and every fence held, while `make validate` §11
    stayed red).

    Rendering is also strictly stronger: a per-tenant file that exists but was
    never added to kustomization.yaml passes a file-based check and is never
    applied to anything. Rendering cannot be fooled by it.
    """
    import subprocess
    r = subprocess.run(["kubectl", "kustomize", f"platform/overlays/{overlay}"],
                       capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        fail(f"kubectl kustomize platform/overlays/{overlay} failed: "
             f"{r.stderr.strip()[:200]}")
        return []
    return [d for d in yaml.safe_load_all(r.stdout) if d]


RENDER = {ov: rendered(ov) for ov in ("lab", "dgx")}
ns_docs = quota_docs = netpol_docs = rbac_docs = RENDER["lab"]
queue_docs = docs("platform/overlays/lab/volcano-queues.yaml")
portal_src = (REPO / "services/tenant-portal/tenant_portal.py").read_text()
gateway_src = (REPO / "services/gateway/gateway.py").read_text()
controller_src = (REPO
                  / "services/capacity-controller/capacity_controller.py").read_text()
console_src = (REPO / "services/ops-console/console.py").read_text()
metering_src = (REPO / "services/metering/metering.py").read_text()

OVERLAYS = ("lab", "dgx")

for t in tenants:
    ns, short = t["namespace"], t["short"]

    # 1. namespace + the labels every gate selects on
    n = find(ns_docs, "Namespace", ns)
    if not n:
        fail(f"{ns}: no Namespace in the rendered lab overlay (add the objects scripts/onboard-tenant.py emits, and the file itself to platform/base/kustomization.yaml)")
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
    # Read the RENDER, not overlays/*/tenant-portal.yaml: onboard-tenant.py
    # emits these two objects into the per-tenant file under platform/base/,
    # exactly as customer-onboarding.md instructs, and they are byte-identical
    # in both overlays (checked 2026-08-31). Looking them up by FILE meant the
    # documented procedure produced a working, fully fenced tenant that this
    # gate still called incomplete — rehearsed end to end and measured.
    for ov in OVERLAYS:
        pd = RENDER[ov]
        if not find(pd, "Role", "arise:tenant-portal", ns):
            fail(f"{ns}: no arise:tenant-portal Role in the {ov} render — the "
                 f"portal could not act in this namespace and the customer "
                 f"would see an empty console")
        if not find(pd, "RoleBinding", "arise:tenant-portal", ns):
            fail(f"{ns}: no arise:tenant-portal RoleBinding in the {ov} render")

    # 6. entitlement: the queue object, and the namespace label the binding
    #    policy reads. The label IS the binding — a tenant whose label is wrong
    #    or missing silently falls back to the 'default' queue: no weight, no
    #    reclaim protection, i.e. a paying customer's work becomes reclaimable.
    if not find(queue_docs, "Queue", t["queue"]):
        fail(f"{ns}: queue {t['queue']} is not defined in volcano-queues.yaml")
    if n:
        # The owner gate reads this label to decide which owner class the
        # tenant may target and which taint it may tolerate. A namespace whose
        # label disagrees with the register is a whole-node customer who
        # cannot reach the node they are paying for — or, worse, one who can
        # reach a class they did not buy (2026-09-01).
        lo = n["metadata"].get("labels", {}).get("arise.ai/owner")
        if lo != t["owner"]:
            fail(f"{ns}: namespace label arise.ai/owner={lo!r}, register says "
                 f"{t['owner']!r} — arise-tenant-owner-gate reads this label, "
                 f"so a mismatch either locks this tenant out of its reserved "
                 f"nodes or lets it target a class it did not buy")
        # The priority binding keys on the owner CLASS, so the register must
        # never say a DIRECT tenant has some other entitlement (or a
        # non-DIRECT tenant the contract one) — otherwise the policy and the
        # register mean different things by the same word (2026-09-01).
        _contract = "arise-contract-bound"
        if t["owner"] == "DIRECT" and list(t["priorities"]) != [_contract]:
            fail(f"{ns}: owner=DIRECT but priorities={t['priorities']} — the "
                 f"priority binding grants {_contract} by owner class, so a "
                 f"DIRECT tenant's priorities must be exactly [{_contract}]")
        if t["owner"] != "DIRECT" and _contract in t["priorities"]:
            fail(f"{ns}: owner={t['owner']} may not hold {_contract}; that "
                 f"class is reserved for work fulfilling a paid contract")
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
        og = find(RENDER[ov], "ValidatingAdmissionPolicy", "arise-tenant-owner-gate")
        if not og:
            fail(f"{ov}: arise-tenant-owner-gate is missing")
        else:
            ovars = {v["name"]: v["expression"] for v in og["spec"]["variables"]}
            ao = ovars.get("allowedOwner", "")
            # The label must be INDEXED, not merely mentioned: the guard
            # clause `'arise.ai/owner' in namespaceObject.metadata.labels`
            # contains the string, so a substring test passed while the
            # expression ignored the value entirely (caught by mutating it,
            # 2026-09-01 — the same vacuity as every other "is it mentioned"
            # check in this file's history).
            if "namespaceObject.metadata.labels['arise.ai/owner']" not in ao:
                fail(f"{ov}: the owner gate does not read the arise.ai/owner "
                     f"namespace label — every whole-node customer after the "
                     f"first would need a CEL edit, and forgetting it is silent")
            if not any("variables.allowedOwner == 'DIRECT'" in v.get("expression", "")
                       for v in og["spec"]["validations"]):
                fail(f"{ov}: the toleration rule still keys on a namespace "
                     f"NAME, so only one tenant can tolerate its own "
                     f"reserved-node taint")
        pb = find(RENDER[ov], "ValidatingAdmissionPolicy", "arise-priority-binding")
        if not pb:
            fail(f"{ov}: arise-priority-binding policy missing")
        elif not any("namespaceObject.metadata.labels['arise.ai/owner']" in
                     v.get("expression", "") for v in pb["spec"]["validations"]):
            fail(f"{ov}: the priority binding does not read the arise.ai/owner "
                 f"label — a second contract customer's jobs would be refused "
                 f"at pod admission")
        if "arise.ai/queue" not in vars_.get("nsQueue", ""):
            fail(f"{ov}: queue binding is not label-driven (no nsQueue variable "
                 f"reading arise.ai/queue) — every new tenant would need a CEL "
                 f"edit in this file, and forgetting it is silent")
        # The tenant must NOT also be enumerated: a hardcoded entry would win
        # over the label, leaving the label decorative and never exercised.
        if re.search(r"'" + re.escape(ns) + r"'\s*:", vars_.get("allowed", "")):
            fail(f"{ns}: still enumerated in the {ov} queue-binding allow-list; "
                 f"that path shadows the label, so the label is untested")

    # Lab fallbacks are optional seeds. Registered customers are loaded from
    # the mounted register and get persistent accounts through the admin API.
    # Check a fallback if it exists, never require code edits per customer.
    tree = ast.parse(portal_src)
    literals = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in ("TENANTS", "PRIORITIES")}
    if ns in literals["TENANTS"]:
        for field in ("queue", "owner"):
            if literals["TENANTS"][ns][field] != t[field]:
                fail(f"{ns}: lab fallback {field} differs from register")
        if literals["PRIORITIES"].get(ns) != t["priorities"]:
            fail(f"{ns}: lab fallback priorities differ from register")

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
                      (console_src, "ops-console", "load_tenant_namespaces"),
                      (metering_src, "metering", "load_tenant_namespaces")):
    if "/etc/arise/tenants.json" not in src:
        fail(f"{name}: does not read the mounted tenant register")
    if re.search(rf"^\s*{fn}\(\)", src, re.M) is None:
        fail(f"{name}: {fn}() is defined but never called at startup")

# 7c-bis. The derivation itself (once, not per tenant). A service that goes
#         back to reading the constant directly re-opens the hole above.
for src, name, fn in ((controller_src, "capacity-controller", "isolation_namespaces"),
                      (console_src, "ops-console", "tenant_namespaces_now"),
                      (metering_src, "metering", "metered_namespaces")):
    if f"def {fn}(" not in src:
        fail(f"{name}: no {fn}() — the tenant set would be a hardcoded list "
             f"again, and a tenant onboarded after startup would be invisible")
        continue
    # Look at the CODE, not at the file and not at the prose. Two earlier
    # cuts of this check were vacuous (both caught by mutation, 2026-08-31):
    # searching the whole source let an unrelated mention satisfy it, and
    # searching the function's source text let its own DOCSTRING satisfy it —
    # every one of these functions explains itself with the words
    # "arise.ai/tier=tenant". ast.unparse of the body minus the docstring has
    # no comments and no prose in it at all.
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == fn)
    stmts = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                              and isinstance(node.body[0].value, ast.Constant)
                              and isinstance(node.body[0].value.value, str)) \
        else node.body
    code = "\n".join(ast.unparse(s) for s in stmts)
    if "set(TENANT_NAMESPACES) | set(live)" not in code:
        fail(f"{name}: {fn}() does not UNION the register with the labelled "
             f"namespaces; a replacement lets one source subtract a tenant "
             f"the other names")
    if "TIER_LABEL" not in code or "%3Dtenant" not in code:
        fail(f"{name}: {fn}() does not query labelSelector "
             f"TIER_LABEL%3Dtenant")
    # ...and TIER_LABEL must be the label every other gate selects on. The
    # constant is what the mutation test pointed at a different string.
    lit = next((a.value.value for a in tree.body
                if isinstance(a, ast.Assign)
                and getattr(a.targets[0], "id", "") == "TIER_LABEL"
                and isinstance(a.value, ast.Constant)), None)
    if lit != TIER:
        fail(f"{name}: TIER_LABEL is {lit!r}, not {TIER!r} — the tenant set "
             f"would be selected on a label nothing carries")
    # ...and it must actually be CALLED. A helper nothing uses is decoration.
    uses = len(re.findall(rf"\b{fn}\(\)", src)) - 1   # minus the def
    if uses < 1:
        fail(f"{name}: {fn}() is defined but never called")


print(f"tenant register: {len(tenants)} tenant(s) "
      f"({', '.join(t['namespace'] for t in tenants)})")
if FAILS:
    for f_ in FAILS:
        print(f"  FAIL {f_}")
    print(f"FAIL: {len(FAILS)} inconsistenc(ies) between the register and its consumers")
    sys.exit(1)
print("  ok  every consumer agrees with platform/tenants.yaml")

