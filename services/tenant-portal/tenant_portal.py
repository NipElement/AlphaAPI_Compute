#!/usr/bin/env python3
"""
ARISE Tenant Portal — the USER-facing service (product-architecture.md §1/§6).

This is the Volcengine-shaped surface: a tenant submits training jobs, dev
machines, volumes and inference services. It is deliberately a THIN client of
the platform's admission gates:

  - The portal builds well-formed manifests (PSA-restricted security context,
    the namespace's queue, the right owner selector and tolerations) so the
    happy path is one click.
  - It does NOT re-implement policy. Quantization, queue binding, owner gates
    and quotas are enforced by the API server; the portal's job on a violation
    is to surface the server's message verbatim, not to pre-judge it. Two
    policy engines drift apart; one cannot.
  - Its RBAC holds workload verbs in tenant namespaces ONLY. No nodes, no
    NodeOwnership, no queues. A compromised portal can waste a tenant's quota,
    never the fleet's integrity. Test UI-02 asserts every one of these denials.

Auth note (honest scope): "who is the user" is out of scope for the prelab —
the ns parameter selects the tenant. Before real customers this needs OIDC in
front; the RBAC boundary below is what keeps that gap non-fatal.

Dependencies: Python standard library only.
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# RFC1123 label/subdomain: the only shape a k8s object name can take. Names are
# interpolated into API URL PATHS, so anything with / ? # .. would let a crafted
# name reach a different resource or inject query params. Validate at the door.
_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def name_ok(s):
    return bool(_DNS1123.match(s or ""))

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
PORT = int(os.environ.get("PORT", "8080"))
FAKE_GPU = os.environ.get("FAKE_GPU_RESOURCE", "arise.dev/fake-gpu")

# Tenant -> queue mirrors the SCH-12 admission binding. The portal offering a
# queue the gate would refuse is a bug factory, so the mapping has exactly one
# source: platform/tenants.yaml, rendered into the platform-tenants ConfigMap
# and mounted here. Onboarding a customer is an edit THERE plus a regenerate —
# not a code change in three services, each of which could be forgotten.
#
# The literals below are the fallback for a pod without the mount (and the
# thing scripts/tenant-check.py compares against the register, so the two can
# never drift apart silently).
TENANTS_PATH = os.environ.get("TENANTS_PATH", "/etc/arise/tenants.json")
TENANTS = {
    "tenant-arise":  {"queue": "arise-internal",  "owner": "ARISE"},
    "tenant-direct": {"queue": "direct-customer", "owner": "DIRECT"},
}
PRIORITIES = {"tenant-arise": ["arise-best-effort", "arise-reserved"],
              "tenant-direct": ["arise-contract-bound"]}


def _load_tenants():
    """Replace the built-ins with the mounted register, if one is present.

    Fails SOFT on a malformed file: the built-ins are a known-good pair, and a
    portal that refuses to start would take the console down for every tenant
    over one bad edit. The mismatch is caught before deploy by the L0 gate.
    """
    try:
        raw = json.loads(Path(TENANTS_PATH).read_text())
        assert isinstance(raw, dict) and raw
        loaded, prios = {}, {}
        for ns, spec in raw.items():
            loaded[ns] = {"queue": spec["queue"], "owner": spec["owner"]}
            prios[ns] = list(spec["priorities"])
        TENANTS.clear()
        TENANTS.update(loaded)
        PRIORITIES.clear()
        PRIORITIES.update(prios)
        log("INFO", "tenant register loaded", path=TENANTS_PATH,
            tenants=sorted(TENANTS))
    except FileNotFoundError:
        log("INFO", "no tenant register mounted; using built-in defaults",
            path=TENANTS_PATH, tenants=sorted(TENANTS))
    except Exception as exc:                                 # noqa: BLE001
        log("ERROR", "tenant register unreadable; using built-in defaults",
            path=TENANTS_PATH, error_class=type(exc).__name__)

# Catalog images: users pick a key, never a raw reference. In the lab there is
# exactly one runnable image (everything is simulated); on real hardware this
# becomes the vetted image registry list.
IMAGES = {
    "python-3.12": "python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36",
    # A dev machine a customer can SSH into (services/devbox): sshd as the
    # unprivileged dev user on 2222. Env-driven because the lab loads it into
    # kind by tag while dgx pulls the registry digest pushed at Day-0.
    "devbox": os.environ.get("DEVBOX_IMAGE", "arise/devbox:lab"),
}
DEFAULT_IMAGE = "python-3.12"
SSH_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                 "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                 "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")


def validate_ssh_public_key(key: str) -> str:
    """One authorized_keys line, or a 400. This string ends up in a file sshd
    parses, so it is validated as DATA, never pasted: one line, a known key
    type, base64 body, no options prefix (options like `command=` change what
    the key can do and are not the customer's to set here)."""
    key = (key or "").strip()
    if not key or "\n" in key or "\r" in key:
        raise ApiError(400, "sshPublicKey must be a single line")
    parts = key.split()
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        raise ApiError(400, f"sshPublicKey must start with one of {list(SSH_KEY_TYPES)}")
    import base64, binascii
    try:
        base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "sshPublicKey body is not valid base64")
    if len(key) > 4096:
        raise ApiError(400, "sshPublicKey too long")
    return " ".join(parts[:3])          # type, body, optional comment

SIM_VCPU = "arise.dev/sim-vcpu"
SIM_MEM = "arise.dev/sim-mem-gi"

# Flavors are REAL DGX B300 slices (NVIDIA user guide, cited in versions.env):
# a node is 8x B300 (288GB HBM each) + 256 vCPU (2x Xeon 6776P) + 2TB RAM, so
# one GPU slice carries 32 vCPU + 256 GiB. CPU-pool flavors slice the (interim)
# generic CPU node profile 64 vCPU / 512 GiB. `vcpu`/`memGi` are the REAL
# magnitudes, requested as simulated extended resources; every pod also
# carries a fixed tiny native request that merely runs the sleep process.
FLAVORS = [
    {"key": "cpu.small",   "vcpu": 4,    "memGi": 16,   "gpu": 0,
     "desc": "轻量 CPU 容器 · CPU 池"},
    {"key": "cpu.large",   "vcpu": 16,   "memGi": 64,   "gpu": 0,
     "desc": "大型 CPU 容器 · CPU 池"},
    {"key": "cpu.custom",  "vcpu": None, "memGi": None, "gpu": 0,
     "desc": "自定义 vCPU/内存 · CPU 池"},
    {"key": "gpu.1",  "vcpu": 32,  "memGi": 256,  "gpu": 1,
     "desc": "1× B300 (288GB HBM) · 1/8 节点"},
    {"key": "gpu.2",  "vcpu": 64,  "memGi": 512,  "gpu": 2,
     "desc": "2× B300 (576GB HBM) · 1/4 节点"},
    {"key": "gpu.4",  "vcpu": 128, "memGi": 1024, "gpu": 4,
     "desc": "4× B300 (1.15TB HBM) · 半节点"},
    {"key": "gpu.8",  "vcpu": 256, "memGi": 2048, "gpu": 8,
     "desc": "8× B300 (2.3TB HBM) · 整节点 DGX B300"},
]
HW = {
    "node": "NVIDIA DGX B300", "gpus": "8× B300 Blackwell Ultra",
    "hbmPerGpu": 288, "vcpu": 256, "cpus": "2× Intel Xeon Platinum 6776P",
    "memGi": 2048, "nvme": "8× 3.84TB E1.S 缓存 + 2× 1.92TB M.2 启动",
    "net": "8× ConnectX-8 800Gb/s + 2× BlueField-3 400Gb/s",
}
# Job priority is a product field (Volcengine 优先级调度): internal tenants
# choose best-effort/reserved; contract tenants are always contract-bound.
# (PRIORITIES is defined with TENANTS above — both come from the register.)


def log(level, msg, **kw):
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "component": "tenant-portal", "msg": msg}
    rec.update(kw)
    print(json.dumps(rec), flush=True)


def _token():
    with open(f"{SA}/token", encoding="utf-8") as fh:
        return fh.read().strip()


def api(method, path, body=None, content_type="application/json"):
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", content_type)
        req.data = json.dumps(body).encode()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def k8s_error(exc: urllib.error.HTTPError) -> ApiError:
    """Surface the server's own denial verbatim — the admission message IS the
    product's error message (it names the grid step, the queue, the gate)."""
    try:
        detail = json.loads(exc.read().decode()).get("message", "")
    except Exception:                                        # noqa: BLE001
        detail = exc.reason
    return ApiError(exc.code, detail[:800])


# ---------------------------------------------------------- manifest build --
def parse_size(body):
    """vcpu/memGi are whole units of the REAL hardware. Legacy cpu/memory
    params map through for API compatibility."""
    legacy_cpu = str(body.get("cpu", "")).strip()
    if legacy_cpu.endswith("m"):
        # "300m" silently becoming 300 whole vCPUs is exactly the kind of
        # unit confusion that books a third of a node by accident.
        raise ApiError(400, "millicore values are not accepted here; "
                            "resources are whole vCPUs (e.g. vcpu: 4)")
    try:
        vcpu = int(body.get("vcpu") or legacy_cpu or 1)
        mem_raw = body.get("memGi")
        if mem_raw is None:
            mem_raw = str(body.get("memory", "2Gi")).replace("Gi", "")
        mem_gi = int(mem_raw)
    except (TypeError, ValueError):
        raise ApiError(400, "vcpu and memGi must be whole numbers "
                            "(real hardware units: 1 vCPU / 1 GiB steps)")
    if vcpu < 1 or mem_gi < 1:
        raise ApiError(400, "vcpu and memGi must be >= 1")
    return vcpu, mem_gi


def restricted_container(name, image_key, vcpu, mem_gi, gpu, command):
    image = IMAGES.get(image_key)
    if not image:
        raise ApiError(400, f"unknown image '{image_key}'; catalog: {list(IMAGES)}")
    # Real magnitudes ride the simulated resources; the native request is a
    # fixed on-grid footprint that only has to run a sleep process.
    req = {"cpu": "500m", "memory": "512Mi",
           SIM_VCPU: str(vcpu), SIM_MEM: str(mem_gi)}
    lim = {"cpu": "1", "memory": "1Gi",
           SIM_VCPU: str(vcpu), SIM_MEM: str(mem_gi)}
    if gpu:
        req[FAKE_GPU] = str(gpu)
        lim[FAKE_GPU] = str(gpu)
    return {
        "name": name, "image": image, "command": command,
        "resources": {"requests": req, "limits": lim},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
    }


def pod_spec_base(ns, gpu):
    """Placement policy encoded once: GPU work on the tenant's owner pool,
    CPU-only work on the CPU pool so GPU-node cores stay with their cards."""
    t = TENANTS[ns]
    spec = {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 5,
        "securityContext": {
            "runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
            "fsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
    if gpu:
        spec["nodeSelector"] = {"arise.ai/role": "gpu",
                                "arise.ai/owner": t["owner"]}
        if t["owner"] == "DIRECT":
            spec["tolerations"] = [{"key": "arise.ai/direct-owned",
                                    "operator": "Equal", "value": "true",
                                    "effect": "NoSchedule"}]
    else:
        spec["nodeSelector"] = {"arise.ai/role": "cpu"}
    return spec


def require_tenant(ns):
    if ns not in TENANTS:
        raise ApiError(403, f"namespace '{ns}' is not a tenant; choose one of {list(TENANTS)}")
    return ns


PORTAL_LABEL = {"arise.ai/managed-by": "tenant-portal"}


# ------------------------------------------------------------- workloads ----
def create_job(ns, body):
    name = body.get("name", "").strip()
    if not name:
        raise ApiError(400, "name required")
    replicas = int(body.get("replicas", 1))
    gpu = int(body.get("gpu", 0))
    vcpu, mem_gi = parse_size(body)
    prio = body.get("priority") or PRIORITIES[ns][0]
    if prio not in PRIORITIES[ns]:
        raise ApiError(400, f"priority must be one of {PRIORITIES[ns]} for {ns}")
    framework = str(body.get("framework", "custom"))[:32]
    vcjob = {
        "apiVersion": "batch.volcano.sh/v1alpha1", "kind": "Job",
        "metadata": {"name": name, "namespace": ns,
                     "labels": {**PORTAL_LABEL, "arise.ai/framework": framework}},
        "spec": {
            "minAvailable": replicas,          # gang: all replicas or none
            "schedulerName": "volcano",
            "priorityClassName": prio,
            "queue": TENANTS[ns]["queue"],
            # A preempted gang must requeue whole, never limp (SCH-05 hazard).
            "policies": [{"event": "PodEvicted", "action": "RestartJob"}],
            "tasks": [{
                "replicas": replicas, "name": "worker",
                "template": {
                    "metadata": {"labels": {**PORTAL_LABEL,
                                            "arise.ai/workload": name}},
                    "spec": {**pod_spec_base(ns, gpu),
                             "priorityClassName": prio, "containers": [
                        restricted_container("worker", body.get("image", DEFAULT_IMAGE),
                                             vcpu, mem_gi, gpu,
                                             ["python3", "-c",
                                              body.get("script", "import time;time.sleep(3600)")])]},
                },
            }],
        },
    }
    try:
        api("POST", f"/apis/batch.volcano.sh/v1alpha1/namespaces/{ns}/jobs", vcjob)
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    log("INFO", "job created", ns=ns, name=name, replicas=replicas, gpu=gpu,
        vcpu=vcpu, mem_gi=mem_gi, priority=prio)
    return {"created": name, "queue": TENANTS[ns]["queue"], "gang": replicas,
            "priority": prio}


def list_jobs(ns):
    try:
        items = api("GET", f"/apis/batch.volcano.sh/v1alpha1/namespaces/{ns}/jobs").get("items", [])
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    out = []
    for j in items:
        st = j.get("status", {})
        out.append({"name": j["metadata"]["name"],
                    "phase": st.get("state", {}).get("phase", "-"),
                    "running": st.get("running", 0),
                    "replicas": j["spec"]["tasks"][0]["replicas"],
                    "queue": j["spec"].get("queue")})
    return out


def create_devmachine(ns, body):
    name = body.get("name", "").strip()
    if not name:
        raise ApiError(400, "name required")
    gpu = int(body.get("gpu", 0))
    vcpu, mem_gi = parse_size(body)
    vol = body.get("volume") or {}
    ssh_key = body.get("sshPublicKey")
    if ssh_key:
        ssh_key = validate_ssh_public_key(ssh_key)
    spec = pod_spec_base(ns, gpu)
    if ssh_key:
        # The customer reaches this machine over SSH: the devbox image runs
        # sshd as the dev user on 2222; their public key rides in a ConfigMap.
        ctr = restricted_container("dev", "devbox", vcpu, mem_gi, gpu, None)
        ctr.pop("command", None)             # the image's entrypoint is sshd
        ctr["ports"] = [{"name": "ssh", "containerPort": 2222}]
        ctr["readinessProbe"] = {"tcpSocket": {"port": 2222},
                                 "initialDelaySeconds": 2, "periodSeconds": 5}
    else:
        ctr = restricted_container("dev", body.get("image", DEFAULT_IMAGE),
                                   vcpu, mem_gi, gpu,
                                   ["python3", "-c", "import time\nwhile True: time.sleep(60)"])
    # Every dev machine gets a WRITABLE home and /tmp. The rootfs stays
    # read-only (that is the security posture); these emptyDirs are what let
    # `pip install`, `git clone` and a shell history actually work.
    ctr["volumeMounts"] = [{"name": "home", "mountPath": "/home/dev"},
                           {"name": "tmp", "mountPath": "/tmp"}]
    spec["volumes"] = [{"name": "home", "emptyDir": {}},
                       {"name": "tmp", "emptyDir": {}}]
    if ssh_key:
        ctr["volumeMounts"] += [{"name": "keys", "mountPath": "/keys"},
                                {"name": "authorized", "mountPath": "/etc/arise/ssh",
                                 "readOnly": True}]
        spec["volumes"] += [{"name": "keys", "emptyDir": {}},
                            {"name": "authorized",
                             "configMap": {"name": f"{name}-ssh"}}]
    if vol:
        pvc_name = f"{name}-data"
        create_volume(ns, {"name": pvc_name,
                           "sizeGi": int(vol.get("sizeGi", 10)),
                           "class": vol.get("class", "arise-longterm")})
        ctr["volumeMounts"].append({"name": "data", "mountPath": "/data"})
        spec["volumes"].append({"name": "data",
                                "persistentVolumeClaim": {"claimName": pvc_name}})
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": name, "namespace": ns,
                        "labels": {**PORTAL_LABEL, "arise.ai/kind": "devmachine",
                                   **({"arise.ai/ssh": "true"} if ssh_key else {})}},
           "spec": {**spec, "containers": [ctr]}}
    try:
        if ssh_key:
            api("POST", f"/api/v1/namespaces/{ns}/configmaps", {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"{name}-ssh", "namespace": ns,
                             "labels": {**PORTAL_LABEL, "arise.ai/devmachine": name}},
                "data": {"authorized_keys": ssh_key + "\n"}})
        api("POST", f"/api/v1/namespaces/{ns}/pods", pod)
        if ssh_key:
            # A stable in-cluster name for the machine's SSH endpoint. How a
            # customer reaches it from OUTSIDE (bastion / LB / port-forward)
            # is decision D4; this is the half that does not depend on it.
            api("POST", f"/api/v1/namespaces/{ns}/services", {
                "apiVersion": "v1", "kind": "Service",
                "metadata": {"name": f"{name}-ssh", "namespace": ns,
                             "labels": {**PORTAL_LABEL, "arise.ai/devmachine": name}},
                "spec": {"type": "ClusterIP",
                         "selector": {"arise.ai/kind": "devmachine"},
                         "ports": [{"name": "ssh", "port": 22, "targetPort": 2222}]}})
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    log("INFO", "devmachine created", ns=ns, name=name, gpu=gpu,
        volume=bool(vol), ssh=bool(ssh_key))
    return {"created": name, "volume": f"{name}-data" if vol else None,
            "ssh": {"service": f"{name}-ssh.{ns}.svc", "port": 22, "user": "dev"}
                   if ssh_key else None}


def create_volume(ns, body):
    name = body.get("name", "").strip()
    if not name:
        raise ApiError(400, "name required")
    size = int(body.get("sizeGi", 10))
    cls = body.get("class", "arise-shared")
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
           "metadata": {"name": name, "namespace": ns, "labels": dict(PORTAL_LABEL)},
           "spec": {"storageClassName": cls, "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": f"{size}Gi"}}}}
    try:
        api("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", pvc)
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    log("INFO", "volume created", ns=ns, name=name, size=size, cls=cls)
    return {"created": name, "class": cls, "sizeGi": size}


def create_service(ns, body):
    """Online inference shape: Deployment + ClusterIP Service (§6)."""
    name = body.get("name", "").strip()
    if not name:
        raise ApiError(400, "name required")
    replicas = int(body.get("replicas", 1))
    gpu = int(body.get("gpu", 0))
    vcpu, mem_gi = parse_size(body)
    ctr = restricted_container("srv", body.get("image", DEFAULT_IMAGE),
                               vcpu, mem_gi, gpu,
                               ["python3", "-m", "http.server", "8080"])
    ctr["ports"] = [{"name": "http", "containerPort": 8080}]
    spec = pod_spec_base(ns, gpu)
    spec["restartPolicy"] = "Always"       # a service restarts; a job does not
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": name, "namespace": ns, "labels": dict(PORTAL_LABEL)},
           "spec": {"replicas": replicas,
                    "selector": {"matchLabels": {"arise.ai/service": name}},
                    "template": {"metadata": {"labels": {**PORTAL_LABEL,
                                                         "arise.ai/service": name}},
                                 "spec": {**spec, "containers": [ctr]}}}}
    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": name, "namespace": ns, "labels": dict(PORTAL_LABEL)},
           "spec": {"type": "ClusterIP",
                    "selector": {"arise.ai/service": name},
                    "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}}
    try:
        api("POST", f"/apis/apps/v1/namespaces/{ns}/deployments", dep)
        api("POST", f"/api/v1/namespaces/{ns}/services", svc)
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    log("INFO", "service created", ns=ns, name=name, replicas=replicas)
    return {"created": name, "endpoint": f"{name}.{ns}.svc:80"}


def overview(ns):
    out = {"namespace": ns, "queue": TENANTS[ns]["queue"], "quota": {}}
    try:
        rqs = api("GET", f"/api/v1/namespaces/{ns}/resourcequotas").get("items", [])
        if rqs:
            st = rqs[0].get("status", {})
            for k in sorted(st.get("hard", {})):
                out["quota"][k] = {"used": st.get("used", {}).get(k, "0"),
                                   "hard": st["hard"][k]}
        pods = api("GET", f"/api/v1/namespaces/{ns}/pods"
                   f"?labelSelector=arise.ai/managed-by%3Dtenant-portal").get("items", [])
        out["devmachines"] = [
            {"name": p["metadata"]["name"],
             "phase": p.get("status", {}).get("phase"),
             "node": p.get("spec", {}).get("nodeName")}
            for p in pods
            if p["metadata"].get("labels", {}).get("arise.ai/kind") == "devmachine"]
        pvcs = api("GET", f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", [])
        out["volumes"] = [
            {"name": c["metadata"]["name"],
             "class": c["spec"].get("storageClassName"),
             "size": c["spec"]["resources"]["requests"]["storage"],
             "phase": c.get("status", {}).get("phase")}
            for c in pvcs]
        out["jobs"] = list_jobs(ns)
        deps = api("GET", f"/apis/apps/v1/namespaces/{ns}/deployments"
                   f"?labelSelector=arise.ai/managed-by%3Dtenant-portal").get("items", [])
        out["services"] = [
            {"name": d["metadata"]["name"],
             "ready": d.get("status", {}).get("readyReplicas", 0),
             "replicas": d["spec"].get("replicas", 0),
             "endpoint": f'{d["metadata"]["name"]}.{ns}.svc:80'}
            for d in deps]
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    return out


def instances(ns, workload):
    """Pods of a workload: vcjob pods carry arise.ai/workload, a devmachine or
    service pod its own name/selector."""
    sel = urllib.parse.quote(f"arise.ai/workload={workload}")
    pods = api("GET", f"/api/v1/namespaces/{ns}/pods?labelSelector={sel}").get("items", [])
    if not pods:
        sel = urllib.parse.quote(f"arise.ai/service={workload}")
        pods = api("GET", f"/api/v1/namespaces/{ns}/pods?labelSelector={sel}").get("items", [])
    if not pods and name_ok(workload):
        # workload is now validated to the RFC1123 shape (no / ? # ..), so direct
        # path interpolation cannot escape the pods/<name> segment.
        try:
            pods = [api("GET", f"/api/v1/namespaces/{ns}/pods/{workload}")]
        except urllib.error.HTTPError:
            pods = []
    out = []
    for p in pods:
        st = p.get("status", {})
        req = (p["spec"]["containers"][0].get("resources", {}).get("requests") or {})
        out.append({"name": p["metadata"]["name"],
                    "phase": st.get("phase"),
                    "node": p["spec"].get("nodeName"),
                    "started": st.get("startTime"),
                    "gpu": req.get("arise.dev/fake-gpu", "0"),
                    "vcpu": req.get("arise.dev/sim-vcpu", "-"),
                    "memGi": req.get("arise.dev/sim-mem-gi", "-")})
    return {"instances": out}


def pod_logs(ns, pod, tail=200):
    if not name_ok(pod):
        raise ApiError(400, "invalid pod name")
    try:
        n = max(1, min(int(tail), 2000))          # clamp; never trust the client
    except (TypeError, ValueError):
        n = 200
    req = urllib.request.Request(
        f"{API}/api/v1/namespaces/{ns}/pods/{pod}/log?tailLines={n}")
    req.add_header("Authorization", f"Bearer {_token()}")
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return {"pod": pod, "log": resp.read().decode(errors="replace")[-20000:]}
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc


def workload_events(ns, name):
    evs = api("GET", f"/api/v1/namespaces/{ns}/events"
              f"?fieldSelector=involvedObject.name%3D{urllib.parse.quote(name)}"
              ).get("items", [])
    rows = [{"at": e.get("lastTimestamp") or e.get("eventTime") or "",
             "type": e.get("type"), "reason": e.get("reason"),
             "message": (e.get("message") or "")[:400]} for e in evs]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"events": rows[:50]}


def delete_workload(ns, kind, name):
    if not name_ok(name):
        raise ApiError(400, "invalid resource name")
    paths = {
        "job":        f"/apis/batch.volcano.sh/v1alpha1/namespaces/{ns}/jobs/{name}",
        "devmachine": f"/api/v1/namespaces/{ns}/pods/{name}",
        "volume":     f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{name}",
        "service":    f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
    }
    if kind not in paths:
        raise ApiError(400, f"unknown kind {kind}")
    if kind == "devmachine":
        # Cascade the SSH side objects (ConfigMap + Service) when present.
        # Best-effort and 404-tolerant: a machine created without a key has
        # neither, and a half-deleted one must still finish deleting.
        for path in (f"/api/v1/namespaces/{ns}/configmaps/{name}-ssh",
                     f"/api/v1/namespaces/{ns}/services/{name}-ssh"):
            try:
                api("DELETE", path)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise k8s_error(exc) from exc
    try:
        api("DELETE", paths[kind])
        if kind == "service":
            api("DELETE", f"/api/v1/namespaces/{ns}/services/{name}")
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    log("INFO", "deleted", ns=ns, kind=kind, name=name)
    return {"deleted": name}


# ---------------------------------------------------------------- HTTP -----
class Handler(BaseHTTPRequestHandler):
    server_version = "arise-tenant-portal/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _ns(self):
        # Require an explicit ns. Silently defaulting to a live tenant is exactly
        # how an omitted selector became a cross-tenant bypass at the gateway — a
        # missing tenant must fail closed, never resolve to tenant-arise.
        vals = self._qs().get("ns")
        if not vals or not vals[0]:
            raise ApiError(400, "ns (tenant namespace) is required")
        return require_tenant(vals[0])

    def do_GET(self):                                        # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            # No HTML is served here. The SPA (web/, Vue 3 + Arco) is served
            # same-origin by the gateway, which proxies ONLY /api/* to this
            # process — so "/" is unreachable through the supported path.
            # The pre-gateway hand-rolled page was removed 2026-08-17 per the
            # decision recorded in runbooks/security-audit-2026-08.md.
            if path in ("/healthz", "/readyz"):
                self._json(200, {"status": "ok"})
            elif path == "/api/flavors":
                self._json(200, {"flavors": FLAVORS, "images": list(IMAGES),
                                 "hw": HW, "priorities": PRIORITIES,
                                 "grid": {"vcpuStep": 1, "memStepGi": 1,
                                          "storageStepGi": 10},
                                 "tenants": list(TENANTS)})
            elif path == "/api/overview":
                self._json(200, overview(self._ns()))
            elif path == "/api/instances":
                self._json(200, instances(self._ns(),
                                          (self._qs().get("workload") or [""])[0]))
            elif path == "/api/logs":
                q = self._qs()
                self._json(200, pod_logs(self._ns(), (q.get("pod") or [""])[0],
                                         (q.get("tail") or ["200"])[0]))
            elif path == "/api/events":
                self._json(200, workload_events(self._ns(),
                                                (self._qs().get("name") or [""])[0]))
            else:
                self._json(404, {"error": "not found"})
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception as exc:                             # noqa: BLE001
            log("ERROR", "GET failed", path=path, error_class=type(exc).__name__)
            self._json(500, {"error": "internal error"})

    def do_POST(self):                                       # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        handlers = {"/api/jobs": create_job, "/api/devmachines": create_devmachine,
                    "/api/volumes": create_volume, "/api/services": create_service}
        try:
            ns = self._ns()
            if path in handlers:
                self._json(201, handlers[path](ns, body))
            else:
                self._json(404, {"error": "not found"})
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception as exc:                             # noqa: BLE001
            log("ERROR", "POST failed", path=path, error_class=type(exc).__name__)
            self._json(500, {"error": "internal error"})

    def do_DELETE(self):                                     # noqa: N802
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        # /api/<kind-plural>/<name>
        kinds = {"jobs": "job", "devmachines": "devmachine",
                 "volumes": "volume", "services": "service"}
        try:
            if len(parts) == 3 and parts[0] == "api" and parts[1] in kinds:
                self._json(200, delete_workload(self._ns(), kinds[parts[1]], parts[2]))
            else:
                self._json(404, {"error": "not found"})
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception as exc:                             # noqa: BLE001
            self._json(500, {"error": "internal error"})


# -------------------------------------------------------------- web page ----

def main():
    _load_tenants()          # register first: every route below reads TENANTS
    log("INFO", "tenant portal listening", port=PORT,
        tenants=list(TENANTS),
        note="workload client only; policy lives in the API server gates")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
