#!/usr/bin/env bash
# ============================================================================
# DGX overlay static render gate — no cluster, no docker (kubectl binary only,
# used purely as a kustomize renderer).
#
# This is the promotion-side twin of validate.sh's lab checks: it renders
# platform/overlays/dgx and asserts the properties that make the overlay safe
# to apply on hardware. Run via `make dgx-render`. Every FAIL here is a real
# "do not apply this on the machine" condition.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

command -v kubectl >/dev/null 2>&1 || {
  echo "kubectl missing — run 'make tools' first"; exit 1; }

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
kubectl kustomize platform/overlays/dgx > "$TMP" || {
  echo "FAIL: dgx overlay does not render"; exit 1; }

python3 - "$TMP" <<'PY'
import sys, yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
fails = []


def every(pred, what):
    bad = [f'{d["kind"]} {d["metadata"].get("namespace","")}/{d["metadata"]["name"]}'
           for d in docs if not pred(d)]
    if bad:
        fails.append(f"{what}: " + "; ".join(bad))


# 1. No simulation residue: nothing named for, and no references to, the
#    lab's fake resources — except the policy that DENIES them.
for d in docs:
    nm = d["metadata"].get("name", "")
    ns = d["metadata"].get("namespace", "")
    if ns == "vast-mock" or nm.startswith("vast-mock") or "fake-gpu" in nm:
        fails.append(f"lab residue object: {d['kind']} {ns}/{nm}")
    if "arise.dev/" in yaml.dump(d) and nm != "arise-deny-simulated-gpu":
        fails.append(f"simulation-resource reference: {d['kind']} {ns}/{nm}")

# 2. Required admission policies AND their bindings (a policy without its
#    binding validates nothing — the object exists, the gate does not).
for want in ("arise-deny-simulated-gpu", "arise-tenant-owner-gate",
             "arise-tenant-host-isolation", "arise-queue-binding",
             "arise-priority-binding", "arise-flavor-quantization",
             "arise-storage-quantization", "arise-tenant-suspended"):
    if not any(d["metadata"].get("name") == want
               and d["kind"] == "ValidatingAdmissionPolicy" for d in docs):
        fails.append(f"ValidatingAdmissionPolicy {want} missing")
    if not any(d["metadata"].get("name") == want
               and d["kind"] == "ValidatingAdmissionPolicyBinding" for d in docs):
        fails.append(f"ValidatingAdmissionPolicyBinding {want} missing")

# 2b. The owner gate must carry the lab's DIRECT branch: tenant-direct pods
#     target the node their customer pays for. A dgx copy that hardcodes
#     ARISE denies the day-0 product (caught in review, 2026-08-26).
og = next((d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"
           and d["metadata"]["name"] == "arise-tenant-owner-gate"), None)
if og and "allowedOwner" not in yaml.dump(og):
    fails.append("owner gate lacks the allowedOwner DIRECT branch "
                 "(tenant-direct would be denied its own reserved nodes)")

# 3. No hostPath VOLUMES in any workload: the docker-cp/hostPath SPA delivery
#    is a kind-ism and no dgx workload may depend on one node's filesystem.
#    (Inspects real pod specs — the host-isolation POLICY legitimately
#    mentions the word "hostPath" in its denial message.)
#    EXPLICIT exemptions, each with a reason — a kind not in the list would be
#    a silent loophole, an unexplained name in this set would be scope creep:
#      etcd-backup: host plumbing BY DEFINITION — it reads the host's etcd
#      client certs and writes snapshots to the head node's disk. Confined to
#      the control-plane node; mounts nothing else.
#      node-exporter: an observability DaemonSet whose FUNCTION is reading the
#      host's /proc, /sys and root filesystem (read-only). The stated
#      exception to the platform-off-sellable-nodes doctrine.
#      audit-archive: same category — it moves the apiserver's own audit files
#      off the OS disk onto /raid; both paths are host facts by definition.
HOSTPATH_EXEMPT = {"etcd-backup", "node-exporter", "audit-archive"}
for d in docs:
    tmpl = None
    if d["kind"] in ("Deployment", "DaemonSet", "StatefulSet", "Job"):
        tmpl = d["spec"]["template"]["spec"]
    elif d["kind"] == "CronJob":
        tmpl = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    elif d["kind"] == "Pod":
        tmpl = d["spec"]
    if tmpl and any("hostPath" in v for v in (tmpl.get("volumes") or [])):
        if d["metadata"]["name"] in HOSTPATH_EXEMPT:
            continue
        fails.append(f"hostPath volume: {d['kind']} "
                     f"{d['metadata'].get('namespace','')}/{d['metadata']['name']}")

# 4. Image hygiene: no unresolved placeholders, no :latest, and EVERY image
#    digest-pinned — with exactly one sanctioned exception: arise/web resolves
#    to the day0-registry.invalid sentinel (an IETF-reserved TLD that cannot
#    pull) until Day-0 pushes it and pins the registry digest. Anything else
#    unpinned is a mutable-tag supply-chain hole.
images = set()
for d in docs:
    for line in yaml.dump(d).splitlines():
        if "image:" in line:
            img = line.split("image:", 1)[1].strip().strip("'\"")
            images.add((d["metadata"].get("name", "?"), img))
for owner, img in sorted(images):
    if "PLACEHOLDER" in img:
        fails.append(f"unresolved image placeholder in {owner}")
    elif img.endswith(":latest"):
        fails.append(f":latest tag in {owner}")
    elif "@sha256:" not in img and not img.startswith("day0-registry.invalid/"):
        fails.append(f"image not digest-pinned in {owner}: {img}")

# 4b. Cross-check against versions.env so the two declarations cannot
#     silently diverge: the rendered python digest must equal
#     PYTHON_BASE_IMAGE's, and the sentinel web image must carry
#     ARISE_WEB_IMAGE's tag.
env = {}
for line in open("versions.env"):
    line = line.split("#", 1)[0].strip()
    if "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
py_digest = env.get("PYTHON_BASE_IMAGE", "").split("@")[-1]
web_tag = env.get("ARISE_WEB_IMAGE", "").rsplit(":", 1)[-1]
rendered_py = {img for _, img in images if img.startswith("python@")}
if rendered_py and not all(i.endswith(py_digest) for i in rendered_py):
    fails.append(f"rendered python digest disagrees with versions.env: {rendered_py}")
# WS6 ops images: also single-sourced against versions.env (same rule).
for var, prefix in (("ETCD_IMAGE", "registry.k8s.io/etcd@"),
                    ("NODE_EXPORTER_IMAGE", "quay.io/prometheus/node-exporter@"),
                    ("KUBE_STATE_METRICS_IMAGE",
                     "registry.k8s.io/kube-state-metrics/kube-state-metrics@")):
    want = env.get(var, "").split("@")[-1]
    got = {img for _, img in images if img.startswith(prefix)}
    if got and not all(i.endswith(want) for i in got):
        fails.append(f"rendered {prefix.split('/')[-1].rstrip('@')} digest "
                     f"disagrees with versions.env {var}: {got}")

rendered_web = {img for _, img in images
                if img.startswith("day0-registry.invalid/arise/web")}
if rendered_web and not all(i.endswith(":" + web_tag) for i in rendered_web):
    fails.append(f"rendered web tag disagrees with versions.env "
                 f"ARISE_WEB_IMAGE ({web_tag}): {rendered_web}")

# 5. Tenant quotas must bound the REAL GPU resource.
for ns in ("tenant-arise", "tenant-direct"):
    q = next((d for d in docs if d["kind"] == "ResourceQuota"
              and d["metadata"].get("namespace") == ns), None)
    if not q:
        fails.append(f"no ResourceQuota in {ns}")
    elif "requests.nvidia.com/gpu" not in q["spec"]["hard"]:
        fails.append(f"{ns} quota does not bound requests.nvidia.com/gpu")

# 6. Platform infra placement: every Deployment pins to the control-plane so
#    nothing platform-owned can squat on (or survive a drain of) a sellable
#    GPU node.
every(lambda d: d["kind"] != "Deployment"
      or d["spec"]["template"]["spec"].get("nodeSelector", {})
             .get("node-role.kubernetes.io/control-plane") is not None,
      "Deployment not pinned to control-plane")

# 7. Both product StorageClasses exist (the storage admission policy names
#    them; missing classes would strand every tenant PVC).
have_sc = {d["metadata"]["name"] for d in docs if d["kind"] == "StorageClass"}
for want in ("arise-shared", "arise-longterm"):
    if want not in have_sc:
        fails.append(f"StorageClass {want} missing")

# 8. The capacity-controller must run the no-marketplace adapter here — the
#    mock does not exist on this overlay and the production adapter does not
#    exist at all (VST-06).
cc = next((d for d in docs if d["kind"] == "Deployment"
           and d["metadata"]["name"] == "capacity-controller"), None)
if cc is None:
    fails.append("capacity-controller Deployment missing")
else:
    env = {e["name"]: e.get("value") for c in cc["spec"]["template"]["spec"]["containers"]
           for e in c.get("env", [])}
    if env.get("VAST_ADAPTER") != "none":
        fails.append(f"capacity-controller VAST_ADAPTER={env.get('VAST_ADAPTER')!r}, expected 'none'")
    if env.get("VAST_PRODUCTION_ADAPTER_ENABLED") != "false":
        fails.append("VAST_PRODUCTION_ADAPTER_ENABLED must be 'false'")
    if env.get("FAKE_GPU_RESOURCE") != "nvidia.com/gpu":
        fails.append("controller drain gate must watch nvidia.com/gpu")

# 8b. Gateway public posture (WS2). The gateway is the only workload that
#     faces users, so its launch switches are gated here rather than trusted:
#     public mode ON (fail-closed credentials), and every credential arriving
#     from a Secret rather than a literal in Git.
gw = next((d for d in docs if d["kind"] == "Deployment"
           and d["metadata"]["name"] == "platform-gateway"), None)
if gw is None:
    fails.append("platform-gateway Deployment missing")
else:
    for c in gw["spec"]["template"]["spec"]["containers"]:
        env = {e["name"]: e for e in c.get("env", [])}
        if env.get("GW_PUBLIC_MODE", {}).get("value") != "true":
            fails.append("gateway GW_PUBLIC_MODE must be 'true' on dgx "
                         "(it is what makes README passwords refuse to start)")
        for want in ("GW_ADMIN_PASSWORD", "GW_ARISE_PASSWORD",
                     "GW_DIRECT_PASSWORD", "GW_SESSION_KEY"):
            e = env.get(want)
            if not e:
                fails.append(f"gateway {want} not set")
            elif "value" in e:
                fails.append(f"gateway {want} carries a LITERAL value — "
                             "credentials must come from a Secret, never Git")
            elif not (e.get("valueFrom") or {}).get("secretKeyRef"):
                fails.append(f"gateway {want} is not a secretKeyRef")

# 8c. No literal credential anywhere in the render. Catches a well-meaning
#     `value: changeme` on any workload, not just the gateway.
for d in docs:
    if d["kind"] not in ("Deployment", "DaemonSet", "StatefulSet", "Job", "Pod"):
        continue
    spec = d["spec"] if d["kind"] == "Pod" else d["spec"]["template"]["spec"]
    for c in (spec.get("containers", []) + spec.get("initContainers", [])):
        for e in c.get("env", []):
            n = e["name"].upper()
            if ("value" in e and e["value"]
                    and any(k in n for k in ("PASSWORD", "SECRET", "TOKEN",
                                             "SESSION_KEY", "APIKEY", "API_KEY"))):
                fails.append(f"literal credential env {e['name']} in "
                             f"{d['kind']}/{d['metadata']['name']}")

# 8d. The vendored Volcano installer (applied by make dgx-volcano, outside
#     kustomize) must exist and be digest-pinned like everything else.
#     NOTE: `env` above was re-bound in 8b to the gateway's container env, so
#     versions.env is re-read here under its own name.
import os, re
vers = {}
for _line in open("versions.env"):
    _line = _line.split("#", 1)[0].strip()
    if "=" in _line:
        _k, _v = _line.split("=", 1)
        vers[_k.strip()] = _v.strip()
vend = "platform/vendor/volcano-" + vers.get("VOLCANO_VERSION", "MISSING") + ".yaml"
if not os.path.exists(vend):
    fails.append(f"vendored Volcano manifest missing: {vend}")
else:
    # per-line, same-line only: the CRD schemas in the installer contain
    # `image:` keys whose VALUE is on the next line (a schema, not a ref).
    for _l in open(vend):
        m = re.match(r"\s*(?:-\s*)?image:[ \t]*(\S+)\s*$", _l)
        if m and ("/" in m.group(1) or ":" in m.group(1)) and "@sha256:" not in m.group(1):
            fails.append(f"vendored Volcano image not digest-pinned: {m.group(1)}")

# 8e. The dgx Volcano queues (applied by make dgx-volcano) bound the REAL
#     resource. The lab file bounds arise.dev/fake-gpu; applied on hardware it
#     bounded nothing (review 2026-08-27 P1-2).
import hashlib
_q = "platform/overlays/dgx/volcano-queues.yaml"
if not os.path.exists(_q):
    fails.append(f"dgx volcano queues missing: {_q}")
else:
    _qt = open(_q).read()
    if "arise.dev/" in _qt:
        fails.append("dgx volcano-queues.yaml carries arise.dev/ simulation residue")
    if "nvidia.com/gpu" not in _qt:
        fails.append("dgx volcano-queues.yaml bounds no nvidia.com/gpu capability")
    # Customer/internal queues must be able to hold at least one whole node
    # (256 threads); the `system` queue is deliberately tiny and exempt.
    for _qd in yaml.safe_load_all(_qt):
        if not _qd or _qd.get("kind") != "Queue":
            continue
        _qn = _qd["metadata"]["name"]; _qc = (_qd.get("spec") or {}).get("capability") or {}
        if _qn in ("arise-internal", "direct-customer") and int(str(_qc.get("cpu", "0")).rstrip("m") or 0) < 256:
            fails.append(f"dgx volcano-queues.yaml queue {_qn} cpu capability {_qc.get('cpu')} is below one node (256): lab token cap leaked")
    _q_by_name = {}
    for _qd in yaml.safe_load_all(_qt):
        if _qd and _qd.get("kind") == "Queue":
            _q_by_name[_qd["metadata"]["name"]] = _qd.get("spec") or {}
    # A paying customer's queue must never be reclaimable: reclaim is how
    # Volcano takes capacity BACK for another queue, which is exactly what a
    # dedicated reservation says cannot happen.
    if _q_by_name.get("direct-customer", {}).get("reclaimable") is not False:
        fails.append("direct-customer queue is reclaimable (or unset): a paid reservation "
                     "must not be reclaimable — volcano-queues.yaml")
    # The platform's own queue stays small and HARD, or platform work could
    # eat the fleet it is supposed to watch.
    _sys_gpu = str((_q_by_name.get("system", {}).get("capability") or {}).get("nvidia.com/gpu", ""))
    if not _sys_gpu or int(_sys_gpu) > 4:
        fails.append(f"system queue nvidia.com/gpu capability is {_sys_gpu or 'unset'} (must be <= 4)")
    if _q_by_name.get("system", {}).get("reclaimable") is not False:
        fails.append("system queue must be reclaimable: false (its cap is meant to be hard)")
    _lab = open("platform/overlays/lab/volcano-queues.yaml").read()
    _names = lambda s: sorted(re.findall(r"^  name: (\S+)", s, re.M))
    if _names(_qt) != _names(_lab):
        fails.append(f"dgx/lab volcano queue sets differ: {_names(_qt)} vs {_names(_lab)}")

# 8d2. The billing ledger's PVC must be RETAIN-class: `kubectl delete pvc`
#      on a Delete-class claim would destroy the only record of what
#      customers owe (no detector existed for this — audit 2026-08-30).
_ledger_pvcs = [d for d in docs if d["kind"] == "PersistentVolumeClaim"
                and d["metadata"]["name"] == "metering-ledger"]
if not _ledger_pvcs:
    fails.append("no metering-ledger PVC in the dgx render")
else:
    _cls = (_ledger_pvcs[0].get("spec") or {}).get("storageClassName")
    if _cls != "arise-longterm":
        fails.append(f"metering-ledger PVC storageClassName={_cls!r}; the billing ledger "
                     f"must sit on the Retain class (arise-longterm)")
_sc_retain = {d["metadata"]["name"]: d.get("reclaimPolicy") for d in docs
              if d["kind"] == "StorageClass"}
if _sc_retain.get("arise-longterm") != "Retain":
    fails.append(f"StorageClass arise-longterm reclaimPolicy={_sc_retain.get('arise-longterm')!r} "
                 f"(must be Retain — the ledger and customers' long-term data depend on it)")

# 8e2. Same for the dgx scheduler config (binpack.resources must be the REAL GPU).
_sc = "platform/overlays/dgx/volcano-scheduler-config.yaml"
if not os.path.exists(_sc):
    fails.append(f"dgx volcano scheduler config missing: {_sc}")
else:
    _st = open(_sc).read()
    if "arise.dev/" in _st:
        fails.append("dgx volcano-scheduler-config.yaml carries arise.dev/ simulation residue")
    if "binpack.resources: nvidia.com/gpu" not in _st:
        fails.append("dgx volcano-scheduler-config.yaml does not binpack on nvidia.com/gpu")
    if "project: arise-b300-prelab" in _st:
        fails.append("dgx volcano-scheduler-config.yaml carries the prelab project label")

# 8g0. kubeadm config placeholders: the file is applied by hand (not kustomize),
#      so nothing else refuses a literal REPLACE_WITH_ before `kubeadm init`.
_kc0 = open("infra/dgx/kubeadm-cluster-config.yaml").read()
if "REPLACE_WITH_" in _kc0:
    import sys as _sys
    _n = _kc0.count("REPLACE_WITH_")
    print(f"  WARN kubeadm-cluster-config.yaml still has {_n} REPLACE_WITH_ placeholder(s) (D1) — fill before kubeadm init;"
          f" set DGX_KUBEADM_FILLED=1 to make this a FAIL", file=_sys.stderr)
    if os.environ.get("DGX_KUBEADM_FILLED"):
        fails.append(f"kubeadm-cluster-config.yaml has {_n} REPLACE_WITH_ placeholders")

# 8f. The vendored CNI: present, checksum equals versions.env (a re-download
#     that silently changed is exactly what vendoring exists to catch),
#     images digest-pinned, pod CIDR equal to versions.env POD_CIDR.
_cni = "platform/vendor/calico-" + vers.get("CALICO_VERSION", "MISSING") + ".yaml"
if not os.path.exists(_cni):
    fails.append(f"vendored Calico manifest missing: {_cni}")
else:
    _sha = hashlib.sha256(open(_cni, "rb").read()).hexdigest()
    if _sha != vers.get("CALICO_MANIFEST_SHA256"):
        fails.append(f"vendored Calico sha256 {_sha[:12]}… != versions.env CALICO_MANIFEST_SHA256")
    _ct = open(_cni).read()
    for m in re.finditer(r"^\s*(?:-\s*)?image:[ \t]*(\S+)\s*$", _ct, re.M):
        if "/" in m.group(1) and "@sha256:" not in m.group(1):
            fails.append(f"vendored Calico image not digest-pinned: {m.group(1)}")
    _cidr = re.search(r"name: CALICO_IPV4POOL_CIDR\n\s*value: \"([^\"]+)\"", _ct)
    if not _cidr or _cidr.group(1) != vers.get("POD_CIDR"):
        fails.append(f"Calico CALICO_IPV4POOL_CIDR {_cidr and _cidr.group(1)} != versions.env POD_CIDR {vers.get('POD_CIDR')}")

# 8g. kubeadm config pins the same Kubernetes version the bootstrap installs.
_kc = open("infra/dgx/kubeadm-cluster-config.yaml").read()
_kv = re.search(r'^kubernetesVersion:\s*"([^"]+)"', _kc, re.M)
if not _kv or _kv.group(1) != vers.get("KUBE_VERSION"):
    fails.append(f"kubeadm kubernetesVersion {_kv and _kv.group(1)} != versions.env KUBE_VERSION {vers.get('KUBE_VERSION')}")
for _c, _k in (("podSubnet", "POD_CIDR"), ("serviceSubnet", "SERVICE_CIDR")):
    _m = re.search(rf'^\s*{_c}:\s*"([^"]+)"', _kc, re.M)
    if not _m or _m.group(1) != vers.get(_k):
        fails.append(f"kubeadm {_c} {_m and _m.group(1)} != versions.env {_k}")

# 9. Identity: every rendered object carries project=arise-b300 (the overlay
#    label transformer overrides base's -prelab), so fleet-wide selectors on
#    hardware see the whole platform, not just overlay-authored objects.
for d in docs:
    lbl = (d["metadata"].get("labels") or {}).get("project")
    if lbl != "arise-b300":
        fails.append(f"project label {lbl!r} on {d['kind']} "
                     f"{d['metadata'].get('namespace','')}/{d['metadata']['name']}")

print(f"  rendered objects: {len(docs)}")
if fails:
    for f in fails:
        print(f"  FAIL {f}")
    sys.exit(1)
print("  ok  all dgx render assertions hold")
PY

echo "dgx-render: PASS"
