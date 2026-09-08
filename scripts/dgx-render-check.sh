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

# versions.env is the pin for everything numeric below (fleet size, image
# digests, CIDRs). Parsed once, up front, so any rule can use it.
import os, re
vers = {}
for _line in open("versions.env"):
    _line = _line.split("#", 1)[0].strip()
    if "=" in _line:
        _k, _v = _line.split("=", 1)
        vers[_k.strip()] = _v.strip()


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
#      ledger-backup: the point of the job is to put the money record on a
#      SECOND surface, so it cannot live on the same PVC it is copying. Same
#      head-node directory family as etcd-backup, and it mounts the ledger
#      itself READ-ONLY so it can never become a second writer (DGX-36).
HOSTPATH_EXEMPT = {"etcd-backup", "node-exporter", "audit-archive",
                   "ledger-backup"}
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

# 5. Tenant quotas must bound the REAL GPU resource, AND the number must
#    actually bound something: a key check passes with the value raised to the
#    whole fleet, which is precisely the state it exists to prevent (one
#    tenant's manifest occupying every B300). Compared against the fleet the
#    completion gate is pinned to, so the two cannot drift apart.
_fleet_gpus = int(vers.get("HW_FLEET_GPU_NODES", "0")) * int(vers.get("HW_GPU_PER_NODE", "0"))
if _fleet_gpus <= 0:
    fails.append("versions.env must pin HW_FLEET_GPU_NODES and HW_GPU_PER_NODE "
                 "(the quota ceiling is compared against them)")
for ns in ("tenant-arise", "tenant-direct"):
    q = next((d for d in docs if d["kind"] == "ResourceQuota"
              and d["metadata"].get("namespace") == ns), None)
    if not q:
        fails.append(f"no ResourceQuota in {ns}")
        continue
    hard = q["spec"]["hard"]
    if "requests.nvidia.com/gpu" not in hard:
        fails.append(f"{ns} quota does not bound requests.nvidia.com/gpu")
    elif _fleet_gpus > 0:
        _n = int(str(hard["requests.nvidia.com/gpu"]))
        if _n >= _fleet_gpus:
            fails.append(f"{ns} quota allows {_n} GPUs of a {_fleet_gpus}-GPU fleet: a single "
                         f"tenant could take everything; the ceiling must be strictly below the fleet")
        if _n <= 0:
            fails.append(f"{ns} quota allows {_n} GPUs — the tenant could never run")

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

# Authentication data must survive the gateway process and be recoverable.
if gw:
    pod = gw["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod.get("volumes", [])}
    container = next(c for c in pod["containers"] if c["name"] == "gateway")
    env_auth = {e["name"]: e for e in container.get("env", [])}
    if env_auth.get("GW_AUTH_DB", {}).get("value") != "/var/lib/arise-auth/auth.sqlite3":
        fails.append("gateway durable GW_AUTH_DB path missing")
    if volumes.get("auth", {}).get("persistentVolumeClaim", {}).get("claimName") != "platform-gateway-auth":
        fails.append("gateway authentication data must use its retained PVC")
    if not any(m.get("name") == "auth" and m.get("mountPath") == "/var/lib/arise-auth"
               for m in container.get("volumeMounts", [])):
        fails.append("gateway authentication volume is not mounted")
    auth_claim = next((d for d in docs if d.get("kind") == "PersistentVolumeClaim" and
                       d["metadata"]["name"] == "platform-gateway-auth"), None)
    if not auth_claim or auth_claim["spec"].get("storageClassName") != "arise-longterm":
        fails.append("gateway authentication PVC must use the Retain storage class")
if not any(d.get("kind") == "CronJob" and d["metadata"]["name"] == "auth-backup" for d in docs):
    fails.append("consistent authentication backup CronJob missing")
owner_gate = next((d for d in docs if d.get("kind") == "ValidatingAdmissionPolicy" and
                   d["metadata"]["name"] == "arise-tenant-owner-gate"), {})
if not any("arise.ai/tenant" in v.get("expression", "") and "request.namespace" in v.get("expression", "")
           and "tolerations" in v.get("expression", "")
           for v in owner_gate.get("spec", {}).get("validations", [])):
    fails.append("dedicated workloads need a tenant-specific admission binding")

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
#      The env-var opt-in below is the operator promising "I filled it in".
#      Nobody would ever set it — it appeared in no runbook, Makefile target
#      or doc until 2026-08-31, so the gate was permanently a WARN. It is now
#      named in runbooks/day0-setup.md, and the HALF-FILLED state below is a
#      hard FAIL that needs no flag at all: the two placeholders are the SAME
#      head-node address in two places, and filling one of them is the
#      realistic mistake. advertiseAddress right + controlPlaneEndpoint still
#      literal means every kubeconfig kubeadm hands out points at nothing.
_kc0 = open("infra/dgx/kubeadm-cluster-config.yaml").read()
_adv = re.search(r'advertiseAddress:\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', _kc0, re.M)
_cpe = re.search(r'controlPlaneEndpoint:\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', _kc0, re.M)
if not _adv or not _cpe:
    fails.append("kubeadm-cluster-config.yaml is missing advertiseAddress or "
                 "controlPlaneEndpoint")
else:
    _adv_v, _cpe_v = _adv.group(1).strip(), _cpe.group(1).strip()
    _cpe_host = _cpe_v.rsplit(":", 1)[0]
    _ph = ["REPLACE_WITH_" in v for v in (_adv_v, _cpe_v)]
    if all(_ph):
        import sys as _sys
        print("  WARN kubeadm-cluster-config.yaml still has its D1 placeholders "
              "— fill the head-node address before kubeadm init; set "
              "DGX_KUBEADM_FILLED=1 (see runbooks/day0-setup.md) to make "
              "this a FAIL", file=_sys.stderr)
        if os.environ.get("DGX_KUBEADM_FILLED"):
            fails.append("DGX_KUBEADM_FILLED is set but "
                         "kubeadm-cluster-config.yaml still has placeholders")
    elif any(_ph):
        fails.append(f"kubeadm-cluster-config.yaml is HALF filled: "
                     f"advertiseAddress={_adv_v!r} controlPlaneEndpoint={_cpe_v!r} "
                     f"— both name the same head node; one of them was edited "
                     f"and the other was not")
    elif _adv_v != _cpe_host:
        fails.append(f"kubeadm advertiseAddress ({_adv_v}) != the host in "
                     f"controlPlaneEndpoint ({_cpe_host}): kubeconfigs would "
                     f"point somewhere the API server does not advertise")
    elif not _cpe_v.endswith(":6443"):
        fails.append(f"kubeadm controlPlaneEndpoint {_cpe_v!r} does not end in "
                     f":6443")
if "REPLACE_WITH_" in _kc0 and os.environ.get("DGX_KUBEADM_FILLED") and \
        not fails:
    fails.append("kubeadm-cluster-config.yaml still contains REPLACE_WITH_")

# 4b-bis. Singleton writers. Each of these owns state no lock protects:
#     metering owns one RWO ledger file, the controller has no leader
#     election, and the gateway holds the user store in process. replicas: 1
#     is only half of it — the default RollingUpdate surges to two pods on
#     every rollout, so the strategy has to be Recreate as well. Measured
#     2026-08-31: platform-gateway was replicas 1 + RollingUpdate maxSurge
#     25%, i.e. two gateways on every deploy.
for _name in ("metering", "capacity-controller", "platform-gateway"):
    _d = next((d for d in docs if d.get("kind") == "Deployment"
               and d["metadata"]["name"] == _name), None)
    if not _d:
        fails.append(f"Deployment {_name} is missing from the render")
        continue
    _r = _d["spec"].get("replicas")
    _s = (_d["spec"].get("strategy") or {}).get("type")
    if _r != 1:
        fails.append(f"{_name} has replicas={_r}: it owns state with no lock, "
                     f"two writers corrupt it")
    if _s != "Recreate":
        fails.append(f"{_name} strategy is {_s!r}, not Recreate: a rolling "
                     f"update surges to two pods and runs two writers at once")

# 4c. The drain deadline and the grace-period cap are ONE mechanism split
#     across two files. Eviction waits out a pod's grace period, so a pod may
#     hold a node in DRAINING for exactly that long; the deadline quarantines
#     the node when the drain outstays it. If the deadline ever drops below
#     the cap, every handover of a pod using its full LEGAL grace period ends
#     in a quarantine — the platform would take its own nodes out of service
#     as designed behaviour. Neither number may be edited alone (2026-08-31).
_grace_cap = None
for _d in docs:
    if _d.get("kind") != "ValidatingAdmissionPolicy":
        continue
    for _v in (_d.get("spec") or {}).get("validations") or []:
        _m = re.search(r"terminationGracePeriodSeconds\s*<=\s*(\d+)",
                       _v.get("expression", ""))
        if _m:
            _grace_cap = int(_m.group(1))
_deadline = None
for _d in docs:
    if _d.get("kind") != "Deployment" or \
            _d["metadata"]["name"] != "capacity-controller":
        continue
    for _c in (_d["spec"]["template"]["spec"].get("containers") or []):
        for _e in (_c.get("env") or []):
            if _e.get("name") == "DRAIN_TIMEOUT_SECONDS":
                _deadline = int(str(_e.get("value")))
if _grace_cap is None:
    fails.append("no terminationGracePeriodSeconds cap in any admission policy: "
                 "a tenant pod can hold a node in DRAINING for as long as it likes")
if _deadline is None:
    fails.append("capacity-controller has no DRAIN_TIMEOUT_SECONDS: the drain "
                 "deadline would fall back to the code default unnoticed")
if _grace_cap is not None and _deadline is not None and _deadline < 2 * _grace_cap:
    fails.append(f"DRAIN_TIMEOUT_SECONDS={_deadline} is below 2x the "
                 f"terminationGracePeriodSeconds cap ({_grace_cap}): a pod using "
                 f"its full legal grace period would quarantine the node it runs on")
# 4c'. The portal is the third copy of the same number. It validates a
#      customer's graceSeconds against GRACE_CAP_SECONDS and DISCLOSES the cap
#      on /api/flavors; if it disagrees with the admission policy, customers
#      are told one limit and refused at another (or, worse, the portal
#      accepts a value admission later rejects with an opaque 4xx).
_portal_cap = None
for _d in docs:
    if _d.get("kind") != "Deployment" or _d["metadata"]["name"] != "tenant-portal":
        continue
    for _c in (_d["spec"]["template"]["spec"].get("containers") or []):
        for _e in (_c.get("env") or []):
            if _e.get("name") == "GRACE_CAP_SECONDS":
                _portal_cap = int(str(_e.get("value")))
if _portal_cap is None:
    fails.append("tenant-portal has no GRACE_CAP_SECONDS env: the cap it tells customers "
                 "would be the code default, unrelated to the admission policy")
elif _grace_cap is not None and _portal_cap != _grace_cap:
    fails.append(f"tenant-portal GRACE_CAP_SECONDS={_portal_cap} but the admission policy "
                 f"caps terminationGracePeriodSeconds at {_grace_cap}: the disclosed limit "
                 f"and the enforced limit differ")

# 4c''. Privileged-PSA namespaces in the render are inside an admission
#      envelope IN THE RENDER. validate.sh 15 scans the source files; this
#      reads what kustomize actually emits, so a policy file that is present on
#      disk but dropped from base/kustomization.yaml — which never reaches the
#      cluster — fails here. Every Namespace rendered with enforce=privileged
#      must be named by a binding whose policy is also rendered.
_priv_ns = {d["metadata"]["name"] for d in docs if d.get("kind") == "Namespace"
            and ((d.get("metadata") or {}).get("labels") or {}).get("pod-security.kubernetes.io/enforce") == "privileged"}
_policies = {d["metadata"]["name"] for d in docs if d.get("kind") == "ValidatingAdmissionPolicy"}
_covered = set()
for _d in docs:
    if _d.get("kind") != "ValidatingAdmissionPolicyBinding" or not _d["metadata"]["name"].startswith("arise-"):
        continue
    if _d["spec"].get("policyName") not in _policies:
        fails.append(f"binding {_d['metadata']['name']} names policy {_d['spec'].get('policyName')!r} "
                     f"which is not in the render")
    for _e in ((_d["spec"].get("matchResources") or {}).get("namespaceSelector") or {}).get("matchExpressions") or []:
        if _e.get("key") == "kubernetes.io/metadata.name" and _e.get("operator") == "In":
            _covered.update(_e.get("values") or [])
if not _priv_ns:
    fails.append("no PSA-privileged namespace in the dgx render: platform-system/storage-system layout changed; update 4c''")
for _n in sorted(_priv_ns - _covered):
    fails.append(f"namespace {_n} is PSA-privileged but no rendered admission envelope binding names it "
                 f"(platform/base/privileged-namespaces-policy.yaml; is it still in base/kustomization.yaml?)")

# 4d. The money record must exist on more than one surface. metering-ledger is
#     an RWO PVC on node-local NVMe (D2): RAID survives a disk, not the node,
#     not a filesystem, not `kubectl delete pvc`. etcd has had a backup
#     CronJob since WS6; the ledger — the invoice itself — had none until
#     2026-08-31. Asserted structurally so it cannot be quietly dropped, and
#     asserted READ-ONLY so the backup can never become a second writer.
_lb = next((d for d in docs if d.get("kind") == "CronJob"
            and d["metadata"]["name"] == "ledger-backup"), None)
if not _lb:
    fails.append("no ledger-backup CronJob: the allocation ledger — the only "
                 "record of what customers owe — would have exactly one copy")
else:
    _lbs = _lb["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    _lv = {v["name"]: v for v in _lbs.get("volumes") or []}
    _pvc = (_lv.get("ledger") or {}).get("persistentVolumeClaim") or {}
    if _pvc.get("claimName") != "metering-ledger":
        fails.append("ledger-backup does not mount the metering-ledger PVC: "
                     f"got {_pvc.get('claimName')!r}")
    if _pvc.get("readOnly") is not True:
        fails.append("ledger-backup mounts the ledger PVC WRITABLE: metering "
                     "is a singleton writer (DGX-36) and a backup job must "
                     "never become a second one")
    if not any(m.get("name") == "ledger" and m.get("readOnly") is True
               for c in _lbs.get("containers") or []
               for m in c.get("volumeMounts") or []):
        fails.append("ledger-backup's container mount of the ledger is not "
                     "readOnly")
    if "hostPath" not in (_lv.get("backups") or {}):
        fails.append("ledger-backup writes its copies back onto a PVC or "
                     "emptyDir: a second copy on the same surface is not a "
                     "second copy")
    if (_lbs.get("nodeSelector") or {}).get(
            "node-role.kubernetes.io/control-plane") is None:
        fails.append("ledger-backup is not pinned to the head node; an RWO "
                     "PVC would make it unschedulable at random")

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

# NetworkPolicy permissions are additive: the old broad policy must exclude
# these APIs as well as the narrow gateway-only policy being present.
policies = [d for d in docs if d['kind'] == 'NetworkPolicy'
            and d['metadata'].get('namespace') == 'platform-system']
broad = next((d for d in policies if d['metadata']['name'] == 'platform-internal-ingress'), {})
excluded = set()
for expr in broad.get('spec', {}).get('podSelector', {}).get('matchExpressions', []):
    if expr.get('key') == 'app.kubernetes.io/name' and expr.get('operator') == 'NotIn':
        excluded.update(expr.get('values', []))
if not {'tenant-portal', 'ops-console', 'metering'} <= excluded:
    fails.append('broad platform ingress policy must exclude protected internal APIs')
narrow = next((d for d in policies if d['metadata']['name'] == 'platform-api-ingress'), {})
expected_source = {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'platform-system'}},
                   'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'platform-gateway'}}}
if narrow.get('spec', {}).get('ingress') != [{'from': [expected_source], 'ports': [{'protocol': 'TCP', 'port': 8080}]}]:
    fails.append('portal/console ingress must allow only the authenticated gateway on port 8080')

# The explicit public edge must also render and pin its maintained runtime.
import subprocess
edge_docs = [d for d in yaml.safe_load_all(subprocess.check_output(
    ['kubectl', 'kustomize', 'platform/overlays/dgx/edge'], text=True)) if d]
edge = next((d for d in edge_docs if d['kind'] == 'Deployment'
             and d['metadata']['name'] == 'platform-edge'), None)
if edge is None:
    fails.append('public Caddy edge deployment missing')
else:
    ep = edge['spec']['template']['spec']
    ec = ep['containers'][0]
    if ec['image'].split('@')[-1] != vers['CADDY_IMAGE'].split('@')[-1] or '@sha256:' not in ec['image']:
        fails.append('edge image must match the pinned CADDY_IMAGE digest')
    if ep.get('automountServiceAccountToken') is not False:
        fails.append('edge must not have Kubernetes API credentials')
    if edge['spec'].get('strategy', {}).get('type') != 'Recreate':
        fails.append('hostNetwork edge needs Recreate to avoid port conflicts')
    if not ec.get('securityContext', {}).get('readOnlyRootFilesystem'):
        fails.append('edge runtime must be read-only')
    epvc = next((d for d in edge_docs if d['kind'] == 'PersistentVolumeClaim'), {})
    if epvc.get('spec', {}).get('storageClassName') != 'arise-longterm':
        fails.append('edge ACME keys require retained storage')
caddyfile = open('platform/overlays/dgx/edge/Caddyfile').read()
for guard in ('admin off', 'max_size 1MB', 'read_header 10s',
              'header_up X-Forwarded-For {remote_host}', 'header_up X-Forwarded-Proto https'):
    if guard not in caddyfile:
        fails.append('edge is missing request guard: ' + guard)

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
