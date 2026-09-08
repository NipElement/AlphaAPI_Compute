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

Identity is enforced by the gateway. NetworkPolicy admits only gateway Pods
to this internal API; the gateway locks tenant users to their namespace. This
process must never be exposed through a public Service or Ingress.

Dependencies: Python standard library only.
"""

import json
import hashlib
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
    return isinstance(s, str) and bool(_DNS1123.fullmatch(s))

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
    """Load the configured register; malformed input refuses startup."""
    try:
        raw = json.loads(Path(TENANTS_PATH).read_text())
        if not isinstance(raw, dict) or not raw:
            raise ValueError("empty or invalid tenant register")
        loaded, prios = {}, {}
        for ns, spec in raw.items():
            if not name_ok(ns) or not isinstance(spec, dict) or spec.get("owner") not in ("ARISE", "DIRECT"):
                raise ValueError("invalid tenant entry")
            if not name_ok(spec.get("queue")) or not isinstance(spec.get("priorities"), list) or not spec["priorities"]:
                raise ValueError("invalid tenant queue or priorities")
            loaded[ns] = {"queue": spec["queue"], "owner": spec["owner"]}
            prios[ns] = list(spec["priorities"])
        TENANTS.clear()
        TENANTS.update(loaded)
        PRIORITIES.clear()
        PRIORITIES.update(prios)
        log("INFO", "tenant register loaded", path=TENANTS_PATH,
            tenants=sorted(TENANTS))
    except FileNotFoundError:
        if FAKE_GPU == "nvidia.com/gpu":
            raise RuntimeError("tenant register is required on hardware")
        log("INFO", "no tenant register mounted; using lab defaults",
            path=TENANTS_PATH, tenants=sorted(TENANTS))
    except Exception as exc:                                 # noqa: BLE001
        log("ERROR", "tenant register unreadable; refusing to start",
            path=TENANTS_PATH, error_class=type(exc).__name__)
        raise RuntimeError("invalid tenant register") from exc

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
    """Validate one OpenSSH public-key line, including the binary wire fields.

    A decodable base64 string alone is not a public key (e.g. ``AAAA``).
    Reject malformed material before replacing a working authorized_keys file.
    """
    import base64
    import binascii
    import struct

    if not isinstance(key, str):
        raise ApiError(400, "sshPublicKey must be a string")
    key = key.strip()
    if not key or "\n" in key or "\r" in key or len(key) > 4096:
        raise ApiError(400, "sshPublicKey must be a single line of at most 4096 characters")
    parts = key.split()
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        raise ApiError(400, "invalid sshPublicKey type")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        offset = 0

        def field():
            nonlocal offset
            if offset + 4 > len(blob):
                raise ValueError("truncated field length")
            size = struct.unpack_from(">I", blob, offset)[0]
            offset += 4
            if not size or offset + size > len(blob):
                raise ValueError("empty or truncated field")
            value = blob[offset:offset + size]
            offset += size
            return value

        algorithm = parts[0]
        if field() != algorithm.encode("ascii"):
            raise ValueError("key type does not match payload")
        if algorithm == "ssh-rsa":
            exponent, modulus = field(), field()
            if exponent[0] & 128 or modulus[0] & 128:
                raise ValueError("negative RSA integer")
            e, n = int.from_bytes(exponent, "big"), int.from_bytes(modulus, "big")
            if e < 3 or e % 2 == 0 or e.bit_length() > 64 or not 2048 <= n.bit_length() <= 16384 or n % 2 == 0:
                raise ValueError("invalid RSA key size or exponent")
        elif "ed25519" in algorithm:
            if len(field()) != 32:
                raise ValueError("Ed25519 public key must be 32 bytes")
        else:
            curve = field().decode("ascii")
            expected = "nistp256" if algorithm.startswith("sk-") else algorithm.removeprefix("ecdsa-sha2-")
            point = field()
            if curve != expected or len(point) != {"nistp256": 65, "nistp384": 97, "nistp521": 133}.get(curve) or point[0] != 4:
                raise ValueError("invalid ECDSA curve or point encoding")
        if algorithm.startswith("sk-"):
            field()  # security-key application string
        if offset != len(blob):
            raise ValueError("trailing key payload")
    except (ValueError, binascii.Error, UnicodeError, struct.error) as exc:
        raise ApiError(400, "invalid sshPublicKey payload") from exc
    return " ".join(parts[:3])


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
    # Slices tile the whole-node flavor (248 / 1984, see gpu.8): 8 x gpu.1 =
    # 2 x gpu.4 = gpu.8. With nameplate slices (32 / 256) the 8th GPU of every
    # node was unsellable — the last slice never fit (audit 2026-08-27).
    {"key": "gpu.1",  "vcpu": 31,  "memGi": 248,  "gpu": 1,
     "desc": "1× B300 (288GB HBM) · 1/8 节点"},
    {"key": "gpu.2",  "vcpu": 62,  "memGi": 496,  "gpu": 2,
     "desc": "2× B300 (576GB HBM) · 1/4 节点"},
    {"key": "gpu.4",  "vcpu": 124, "memGi": 992,  "gpu": 4,
     "desc": "4× B300 (1.15TB HBM) · 半节点"},
    # Whole node = everything the kubelet can hand out, not the nameplate:
    # kubeadm reserves 3 CPU / 12 GiB for system+kube (kubeadm-cluster-config)
    # and the per-node DaemonSets (calico, node-exporter, dcgm, device plugin)
    # take a little more, so 256/2048 sat Pending forever (review 2026-08-27).
    {"key": "gpu.8",  "vcpu": 248, "memGi": 1984, "gpu": 8,
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
def integer(value, field, minimum=0, maximum=1_000_000):
    # int(1.9), int(True), and vcpu=0 falling back to one are all wrong
    # for purchased resources. Accept decimal integer strings for old clients.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ApiError(400, f"{field} must be an integer")
    raw = str(value)
    if not re.fullmatch(r"[0-9]+(?:\.0+)?", raw) or len(raw) > 16:
        raise ApiError(400, f"{field} must be an integer")
    out = int(raw.split(".")[0])
    if not minimum <= out <= maximum:
        raise ApiError(400, f"{field} must be {minimum}..{maximum}")
    return out


def resource_name(body, limit=58):
    name = body.get("name")
    # Names also become label values and Service names with a -ssh suffix.
    if (not isinstance(name, str) or len(name) > limit or
            not re.fullmatch(r"[a-z](?:[a-z0-9-]*[a-z0-9])?", name)):
        raise ApiError(400, f"name must be 1..{limit} lowercase letters, digits or hyphens, starting with a letter")
    return name


def parse_size(body):
    vcpu = body.get("vcpu", body.get("cpu", 1))
    mem = body.get("memGi", body.get("memory", "2Gi"))
    if isinstance(mem, str) and mem.endswith("Gi"):
        mem = mem[:-2]
    return integer(vcpu, "vcpu", 1, 4096), integer(mem, "memGi", 1, 32768)


def validate_volume(body):
    if not isinstance(body, dict):
        raise ApiError(400, "volume must be an object")
    size = integer(body.get("sizeGi", 10), "sizeGi", 10, 1_000_000)
    cls = body.get("class", "arise-shared")
    if size % 10 or cls not in ("arise-shared", "arise-longterm"):
        raise ApiError(400, "storage must be in 10 GiB steps, class arise-shared or arise-longterm")
    return size, cls


def request_fingerprint(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provision_object(path, obj, fingerprint=None, owner=None):
    """Create or resume exactly the same operation after a timeout/retry.

    Parents reserve the name in Kubernetes before any side object is created.
    Children are garbage-collected with that parent UID. Conflicts belonging
    to another operation are never adopted or deleted. Interrupted parents
    remain visibly suspended and can be resumed with the same request.
    """
    md = obj["metadata"]
    if fingerprint:
        md.setdefault("annotations", {})["arise.ai/request-sha256"] = fingerprint
    if owner:
        md["ownerReferences"] = [{"apiVersion": owner["apiVersion"], "kind": owner["kind"],
                                  "name": owner["metadata"]["name"], "uid": owner["metadata"]["uid"]}]
    try:
        current = api("GET", path + "/" + md["name"])
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        try:
            return api("POST", path, obj)
        except urllib.error.HTTPError as conflict:
            if conflict.code != 409:
                raise
        current = api("GET", path + "/" + md["name"])
    meta = current.get("metadata", {})
    if meta.get("deletionTimestamp"):
        raise ApiError(409, f"{md['name']} is being deleted; wait before retrying")
    if fingerprint and (meta.get("annotations") or {}).get("arise.ai/request-sha256") != fingerprint:
        raise ApiError(409, f"{md['name']} already exists with different settings")
    if owner and not any(r.get("uid") == owner["metadata"]["uid"] for r in meta.get("ownerReferences", [])):
        raise ApiError(409, f"{md['name']} belongs to another resource")
    return current


def admit_new_template(ns, collection, name, spec):
    try:
        api("GET", collection + "/" + name)
        return
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    api("POST", f"/api/v1/namespaces/{ns}/pods?dryRun=All", {
        "apiVersion": "v1", "kind": "Pod",
        # A Deployment/Job may share its name with an existing development Pod.
        # Validate a generated child name, as the real workload controller does.
        "metadata": {"generateName": name + "-admission-", "namespace": ns},
        "spec": spec})


# Node-local scratch a tenant may hold (writable layers + logs + emptyDirs).
# The magnitudes are per-pod, enforced by the kubelet (eviction), and sized
# so one tenant cannot exhaust a node's kubelet disk. Lab values are small;
# dgx sets them via env from the NVMe layout (D2).
CPU_POOL_ROLE = os.environ.get("CPU_POOL_ROLE", "cpu")     # lab: aux cpu nodes; dgx: gpu
# lab=true: vCPU/GiB are carried by arise.dev/sim-* extended resources (the
# kind nodes cannot back real magnitudes); dgx=false: native cpu/memory.
SIM_RESOURCES = os.environ.get("SIM_RESOURCES", "true").lower() != "false"
EPHEMERAL_LIMIT = os.environ.get("EPHEMERAL_LIMIT", "20Gi")
HOME_SIZE_LIMIT = os.environ.get("HOME_SIZE_LIMIT", "16Gi")
# Graceful termination. The admission policy (flavor-policy.yaml) caps
# terminationGracePeriodSeconds at GRACE_CAP_SECONDS; a node handover waits out
# a pod's whole grace period, so the cap is the OTHER half of the drain
# deadline (dgx-render-check 4c asserts the two agree). The portal hardcoded 5
# until 2026-09-08: customers were told "up to 300 s to checkpoint" while every
# portal-created workload got 5 s. The default stays short — a PID 1 that does
# not handle SIGTERM is simply held for the whole period and then killed, so a
# long default would make every delete slow for everyone — and a customer who
# checkpoints on SIGTERM asks for the time they need with graceSeconds (<= cap).
GRACE_CAP_SECONDS = int(os.environ.get("GRACE_CAP_SECONDS", "300"))
DEFAULT_GRACE_SECONDS = int(os.environ.get("DEFAULT_GRACE_SECONDS", "5"))
if not 1 <= DEFAULT_GRACE_SECONDS <= GRACE_CAP_SECONDS:
    raise SystemExit(f"DEFAULT_GRACE_SECONDS={DEFAULT_GRACE_SECONDS} must lie in "
                     f"1..GRACE_CAP_SECONDS ({GRACE_CAP_SECONDS})")
TMP_SIZE_LIMIT = os.environ.get("TMP_SIZE_LIMIT", "4Gi")


GPU_PER_NODE = int(os.environ.get("GPU_PER_NODE", "8"))


def gpu_ok(gpu: int, replicas: int, ns: str) -> int:
    """A pod can hold 0..GPU_PER_NODE GPUs (one node), and the whole workload
    must fit the namespace quota — refused HERE with numbers instead of a
    job that sits Pending forever with a truncated scheduler string."""
    if gpu < 0 or gpu > GPU_PER_NODE:
        raise ApiError(400, f"gpu must be 0..{GPU_PER_NODE} per pod (one node has {GPU_PER_NODE})")
    total = gpu * max(replicas, 1)
    if total:
        try:
            rqs = api("GET", f"/api/v1/namespaces/{ns}/resourcequotas").get("items", [])
        except urllib.error.HTTPError:
            rqs = []
        for rq in rqs:
            hard = (rq.get("status") or {}).get("hard") or {}
            used = (rq.get("status") or {}).get("used") or {}
            key = f"requests.{FAKE_GPU}"
            if key in hard:
                h, u = int(str(hard[key])), int(str(used.get(key, "0")))
                if total > h:
                    raise ApiError(409, f"{total} GPU(s) requested but the tenant quota has "
                                        f"{h - u} free of {h} ({key}); shrink the request or "
                                        f"delete something")
    return gpu


def restricted_container(name, image_key, vcpu, mem_gi, gpu, command):
    image = IMAGES.get(image_key) if isinstance(image_key, str) else None
    if not image:
        raise ApiError(400, f"unknown image '{image_key}'; catalog: {list(IMAGES)}")
    # Real magnitudes ride the simulated resources; the native request is a
    # fixed on-grid footprint that only has to run a sleep process.
    if SIM_RESOURCES:
        # Lab: real magnitudes ride the simulated extended resources; the
        # native request is a fixed on-grid footprint for a sleep process.
        req = {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "1Gi",
               SIM_VCPU: str(vcpu), SIM_MEM: str(mem_gi)}
        lim = {"cpu": "1", "memory": "1Gi", "ephemeral-storage": EPHEMERAL_LIMIT,
               SIM_VCPU: str(vcpu), SIM_MEM: str(mem_gi)}
    else:
        # Hardware: the flavor's vCPU / GiB ARE the native request (on the
        # 500m / 512Mi grid by construction: integers). Requesting the
        # simulated resources here left every pod Pending forever — nothing
        # on a DGX advertises arise.dev/sim-* (review 2026-08-27).
        req = {"cpu": str(vcpu), "memory": f"{mem_gi}Gi", "ephemeral-storage": "1Gi"}
        lim = {"cpu": str(vcpu), "memory": f"{mem_gi}Gi", "ephemeral-storage": EPHEMERAL_LIMIT}
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


def grace_seconds(body):
    """Optional graceSeconds in a create request: the time a workload gets
    between SIGTERM and SIGKILL on delete, eviction or node handover."""
    return integer(body.get("graceSeconds", DEFAULT_GRACE_SECONDS),
                   "graceSeconds", 1, GRACE_CAP_SECONDS)


def pod_spec_base(ns, gpu, grace=None):
    """Placement policy encoded once: GPU work on the tenant's owner pool,
    CPU-only work on the CPU pool so GPU-node cores stay with their cards."""
    t = TENANTS[ns]
    spec = {
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": DEFAULT_GRACE_SECONDS if grace is None else grace,
        # A tenant workload never talks to the API server; the `default` SA
        # token is a live bearer credential it must not hold (review
        # 2026-08-27 P2-5). The namespace's default SA opts out too.
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
            "fsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"},
        },
    }
    if gpu:
        spec["nodeSelector"] = {"arise.ai/role": "gpu",
                                "arise.ai/owner": t["owner"]}
    else:
        # Where CPU-only work goes is an OVERLAY fact: the lab has an aux cpu
        # pool; the Day-0 fleet is 4 GPU nodes + a tainted head and no CPU
        # node at all (D1). Pinning to role=cpu made every hardware dev
        # machine unschedulable (review 2026-08-27) — dgx sets CPU_POOL_ROLE=gpu
        # so CPU-only boxes ride the tenant's own pool (a DIRECT customer's
        # reserved node, the ARISE pool for internal work).
        spec["nodeSelector"] = {"arise.ai/role": CPU_POOL_ROLE}
        if CPU_POOL_ROLE == "gpu":
            spec["nodeSelector"]["arise.ai/owner"] = t["owner"]
    # A DIRECT node carries the arise.ai/direct-owned NoSchedule taint: EVERY
    # pod that targets it needs the toleration, GPU or not (review 2026-08-27
    # P1-1: CPU-only boxes for the paying customer sat Pending on dgx).
    if spec["nodeSelector"].get("arise.ai/owner") == "DIRECT":
        spec["nodeSelector"]["arise.ai/tenant"] = ns
        spec["tolerations"] = [{"key": "arise.ai/direct-owned",
                                "operator": "Equal", "value": "true",
                                "effect": "NoSchedule"}]
    return spec


def require_tenant(ns):
    if ns not in TENANTS:
        raise ApiError(403, f"namespace '{ns}' is not a tenant; choose one of {list(TENANTS)}")
    return ns


PORTAL_LABEL = {"arise.ai/managed-by": "tenant-portal"}


# ------------------------------------------------------------- workloads ----
def create_job(ns, body):
    name = resource_name(body)
    replicas = integer(body.get("replicas", 1), "replicas", 1, 64)
    gpu = gpu_ok(integer(body.get("gpu", 0), "gpu", 0, GPU_PER_NODE), replicas, ns)
    vcpu, mem_gi = parse_size(body)
    prio = body.get("priority") or PRIORITIES[ns][0]
    if prio not in PRIORITIES[ns]:
        raise ApiError(400, f"priority must be one of {PRIORITIES[ns]} for {ns}")
    framework = body.get("framework", "custom")
    if not isinstance(framework, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", framework):
        raise ApiError(400, "invalid framework label")
    script = body.get("script", "import time;time.sleep(3600)")
    if not isinstance(script, str) or not script.strip():
        raise ApiError(400, "script must be a non-empty string")
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
                    "spec": {**pod_spec_base(ns, gpu, grace_seconds(body)),
                             "priorityClassName": prio, "containers": [
                        restricted_container("worker", body.get("image", DEFAULT_IMAGE),
                                             vcpu, mem_gi, gpu,
                                             ["python3", "-c",
                                              script])]},
                },
            }],
        },
    }
    try:
        admit_new_template(ns, f"/apis/batch.volcano.sh/v1alpha1/namespaces/{ns}/jobs", name,
                           vcjob["spec"]["tasks"][0]["template"]["spec"])
        provision_object(f"/apis/batch.volcano.sh/v1alpha1/namespaces/{ns}/jobs", vcjob,
                         fingerprint=request_fingerprint(body))
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


def rotate_ssh_key(ns, name, body):
    """Replace a dev machine's authorized_keys in place. ConfigMap volumes
    hot-reload (kubelet sync, ~1 min), so a compromised key is revoked
    without deleting the box and its /home (review 2026-08-27 P2-10). The
    machine must have been created WITH a key: a keyless box has no sshd."""
    # Every other route validates the name before interpolating it into an API
    # path; this one did not (falsification audit 2026-08-30). A path segment
    # cannot contain a slash, but ".." is still a segment the API server would
    # clean into a DIFFERENT object, so validate it like the rest.
    if not name_ok(name):
        raise ApiError(400, "invalid dev machine name")
    key = validate_ssh_public_key(body.get("sshPublicKey") or "")
    try:
        pod = api("GET", f"/api/v1/namespaces/{ns}/pods/{name}")
        if (pod.get("metadata", {}).get("labels") or {}).get("arise.ai/kind") != "devmachine":
            raise ApiError(404, "no such dev machine")
        cm = api("GET", f"/api/v1/namespaces/{ns}/configmaps/{name}-ssh")
        if (cm.get("metadata", {}).get("labels") or {}).get("arise.ai/devmachine") != name:
            raise ApiError(409, "SSH configuration belongs to another resource")
        cm["data"] = {"authorized_keys": key + "\n"}
        api("PUT", f"/api/v1/namespaces/{ns}/configmaps/{name}-ssh", cm)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            try:
                api("GET", f"/api/v1/namespaces/{ns}/pods/{name}")
            except urllib.error.HTTPError:
                raise ApiError(404, f"no dev machine named {name}") from exc
            raise ApiError(404, f"{name} was created without an SSH key; there is no sshd to give a key to — recreate it with one") from exc
        raise k8s_error(exc) from exc
    log("INFO", "ssh key rotated", ns=ns, name=name)
    return {"rotated": name, "note": "sshd picks the new key up within ~1 minute; "
                                     "sessions already open are not cut"}


def create_devmachine(ns, body):
    name = resource_name(body)
    if not name_ok(name):
        raise ApiError(400, "invalid name (DNS-1123 label)")
    gpu = gpu_ok(integer(body.get("gpu", 0), "gpu", 0, GPU_PER_NODE), 1, ns)
    vcpu, mem_gi = parse_size(body)
    vol = body.get("volume", {})
    if vol is None:
        vol = {}
    if not isinstance(vol, dict):
        raise ApiError(400, "volume must be an object")
    if vol:
        vol_size, vol_class = validate_volume({"class": "arise-longterm", **vol})
    ssh_key = body.get("sshPublicKey")
    if ssh_key:
        ssh_key = validate_ssh_public_key(ssh_key)
    spec = pod_spec_base(ns, gpu, grace_seconds(body))
    if ssh_key:
        # The customer reaches this machine over SSH: the devbox image runs
        # sshd as the dev user on 2222; their public key rides in a ConfigMap.
        ctr = restricted_container("dev", "devbox", vcpu, mem_gi, gpu, None)
        ctr.pop("command", None)             # the image's entrypoint is sshd
        ctr["ports"] = [{"name": "ssh", "containerPort": 2222}]
        # exec, not tcpSocket: a TCP knock every 5 s made sshd log a
        # kex_exchange_identification line each time and buried the Logs tab
        # (customer walk 2026-08-27). procps is in the image for this.
        ctr["readinessProbe"] = {"exec": {"command": ["pgrep", "-x", "sshd"]},
                                 "initialDelaySeconds": 2, "periodSeconds": 10}
    else:
        ctr = restricted_container("dev", body.get("image", DEFAULT_IMAGE),
                                   vcpu, mem_gi, gpu,
                                   ["python3", "-c", "import time\nwhile True: time.sleep(60)"])
    # Every dev machine gets a WRITABLE home and /tmp. The rootfs stays
    # read-only (that is the security posture); these emptyDirs are what let
    # `pip install`, `git clone` and a shell history actually work.
    ctr["volumeMounts"] = [{"name": "home", "mountPath": "/home/dev"},
                           {"name": "tmp", "mountPath": "/tmp"}]
    # Bounded (review 2026-08-27 P2-7): without sizeLimit a `dd` into /tmp
    # fills the node's kubelet disk and evicts every co-located pod on the
    # shared pool. Over the limit the kubelet evicts THIS pod only.
    spec["volumes"] = [{"name": "home", "emptyDir": {"sizeLimit": HOME_SIZE_LIMIT}},
                       {"name": "tmp", "emptyDir": {"sizeLimit": TMP_SIZE_LIMIT}}]
    if ssh_key:
        ctr["volumeMounts"] += [{"name": "keys", "mountPath": "/keys"},
                                {"name": "authorized", "mountPath": "/etc/arise/ssh",
                                 "readOnly": True}]
        # Bounded like the other two. The container mounts /keys writable, so
        # an unbounded emptyDir here is a third scratch path that skips both
        # limits above; the pod's ephemeral-storage limit still caps the total,
        # but the per-volume bound is what makes the intent enforceable
        # (audit 2026-08-31).
        spec["volumes"] += [{"name": "keys", "emptyDir": {"sizeLimit": "16Mi"}},
                            {"name": "authorized",
                             "configMap": {"name": f"{name}-ssh"}}]
    if vol:
        pvc_name = f"{name}-data"
        ctr["volumeMounts"].append({"name": "data", "mountPath": "/data"})
        spec["volumes"].append({"name": "data",
                                "persistentVolumeClaim": {"claimName": pvc_name}})
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": name, "namespace": ns,
                        "labels": {**PORTAL_LABEL, "arise.ai/kind": "devmachine",
                                   # per-machine identity: the SSH Service
                                   # selects on it (review 2026-08-27 P1-3 —
                                   # selecting on kind alone round-robined
                                   # `alice-ssh` across every machine)
                                   "arise.ai/devmachine": name,
                                   **({"arise.ai/ssh": "true"} if ssh_key else {})}},
           "spec": {**spec, "containers": [ctr]}}
    # Reserve the Pod name before side effects. It cannot run or incur GPU
    # charges until every dependency is present and the scheduling gate opens.
    pod["spec"]["schedulingGates"] = [{"name": "arise.ai/provisioning"}]
    root = f"/api/v1/namespaces/{ns}"
    try:
        parent = provision_object(root + "/pods", pod, fingerprint=request_fingerprint(body))
        if vol:
            try:
                pvc = api("GET", root + f"/persistentvolumeclaims/{pvc_name}")
                if (pvc.get("metadata", {}).get("labels") or {}).get("arise.ai/devmachine") != name:
                    raise ApiError(409, f"volume {pvc_name} belongs to another resource")
                if (pvc["spec"].get("storageClassName") != vol_class or
                        pvc["spec"]["resources"]["requests"].get("storage") != f"{vol_size}Gi"):
                    raise ApiError(409, f"kept volume {pvc_name} has different size or class")
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                try:
                    create_volume(ns, {"name": pvc_name, "sizeGi": vol_size,
                                       "class": vol_class, "devmachine": name})
                except ApiError as conflict:
                    if conflict.code != 409:
                        raise
                    # Concurrent identical retry may have created it. Re-read
                    # through this same validation path on the next retry.
                    raise ApiError(409, "volume creation in progress; retry the same request") from conflict
        if ssh_key:
            provision_object(root + "/configmaps", {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"{name}-ssh", "namespace": ns,
                             "labels": {**PORTAL_LABEL, "arise.ai/devmachine": name}},
                "data": {"authorized_keys": ssh_key + "\n"}}, owner=parent)
            provision_object(root + "/services", {
                "apiVersion": "v1", "kind": "Service",
                "metadata": {"name": f"{name}-ssh", "namespace": ns,
                             "labels": {**PORTAL_LABEL, "arise.ai/devmachine": name}},
                "spec": {"type": "ClusterIP",
                         "selector": {"arise.ai/kind": "devmachine", "arise.ai/devmachine": name},
                         "ports": [{"name": "ssh", "port": 22, "targetPort": 2222}]}}, owner=parent)
        api("PATCH", root + f"/pods/{name}", {
            "metadata": {"uid": parent["metadata"]["uid"]},
            "spec": {"schedulingGates": None}}, content_type="application/merge-patch+json")
    except urllib.error.HTTPError as exc:
        raise k8s_error(exc) from exc
    log("INFO", "devmachine created", ns=ns, name=name, gpu=gpu,
        volume=bool(vol), ssh=bool(ssh_key))
    return {"created": name, "volume": f"{name}-data" if vol else None,
            "ssh": {"service": f"{name}-ssh.{ns}.svc", "port": 22, "user": "dev"}
                   if ssh_key else None}


def create_volume(ns, body):
    name = resource_name(body, limit=63)
    size, cls = validate_volume(body)
    labels = dict(PORTAL_LABEL)
    if body.get("devmachine"):
        labels["arise.ai/devmachine"] = body["devmachine"]   # auto-delete eligibility
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
           "metadata": {"name": name, "namespace": ns, "labels": labels},
           "spec": {"storageClassName": cls, "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": f"{size}Gi"}}}}
    try:
        api("POST", f"/api/v1/namespaces/{ns}/persistentvolumeclaims", pvc)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise ApiError(409, f"volume {name} already exists") from exc
        raise k8s_error(exc) from exc
    log("INFO", "volume created", ns=ns, name=name, size=size, cls=cls)
    return {"created": name, "class": cls, "sizeGi": size}


def create_service(ns, body):
    """Online inference shape: Deployment + ClusterIP Service (§6)."""
    name = resource_name(body)
    replicas = integer(body.get("replicas", 1), "replicas", 1, 64)
    gpu = gpu_ok(integer(body.get("gpu", 0), "gpu", 0, GPU_PER_NODE), replicas, ns)
    vcpu, mem_gi = parse_size(body)
    script = body.get("script")
    if script is not None and (not isinstance(script, str) or not script.strip()):
        raise ApiError(400, "script must be a non-empty Python string listening on port 8080")
    ctr = restricted_container("srv", body.get("image", DEFAULT_IMAGE),
                               vcpu, mem_gi, gpu,
                               ["python3", "-c", script] if script else
                               ["python3", "-m", "http.server", "8080"])
    ctr["ports"] = [{"name": "http", "containerPort": 8080}]
    ctr["readinessProbe"] = {"tcpSocket": {"port": "http"}, "periodSeconds": 5}
    ctr["volumeMounts"] = [{"name": "tmp", "mountPath": "/tmp"}]
    spec = pod_spec_base(ns, gpu, grace_seconds(body))
    spec["restartPolicy"] = "Always"
    spec["volumes"] = [{"name": "tmp", "emptyDir": {"sizeLimit": TMP_SIZE_LIMIT}}]
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": name, "namespace": ns, "labels": dict(PORTAL_LABEL)},
           "spec": {"replicas": 0,  # activated only after Service creation
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
        admit_new_template(ns, f"/apis/apps/v1/namespaces/{ns}/deployments", name,
                           dep["spec"]["template"]["spec"])
        parent = provision_object(f"/apis/apps/v1/namespaces/{ns}/deployments", dep,
                                  fingerprint=request_fingerprint(body))
        provision_object(f"/api/v1/namespaces/{ns}/services", svc, owner=parent)
        api("PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", {
            "metadata": {"uid": parent["metadata"]["uid"]}, "spec": {"replicas": replicas}},
            content_type="application/merge-patch+json")
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
             "phase": "Terminating" if p["metadata"].get("deletionTimestamp")
                      else "Provisioning" if p.get("spec", {}).get("schedulingGates")
                      else p.get("status", {}).get("phase"),
             "node": p.get("spec", {}).get("nodeName"),
             # The endpoint the customer needs, on every listing — not only
             # once in the create response (customer walk 2026-08-27).
             "ssh": ({"service": f'{p["metadata"]["name"]}-ssh.{ns}.svc', "port": 22, "user": "dev"}
                     if p["metadata"].get("labels", {}).get("arise.ai/ssh") == "true" else None)}
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
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise k8s_error(exc) from exc
            pods = []
    out = []
    for p in pods:
        st = p.get("status", {})
        req = (p["spec"]["containers"][0].get("resources", {}).get("requests") or {})
        out.append({"name": p["metadata"]["name"],
                    "phase": st.get("phase"),
                    "node": p["spec"].get("nodeName"),
                    "started": st.get("startTime"),
                    "gpu": req.get(FAKE_GPU, "0"),
                    "graceSeconds": p["spec"].get("terminationGracePeriodSeconds"),
                    # lab: the simulated magnitudes; hardware: the native ones
                    "vcpu": req.get(SIM_VCPU, "-") if SIM_RESOURCES else req.get("cpu", "-"),
                    "memGi": req.get(SIM_MEM, "-") if SIM_RESOURCES else str(req.get("memory", "-")).replace("Gi", "")})
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


def _plain_event(msg: str, limit: int = 1500) -> str:
    """Quota refusals arrive wrapped in Go's errors.StatusError{...} with the
    used/limited numbers at the END — a 400-char cut removed exactly the
    part a tenant needs. Strip the wrapper, keep the numbers."""
    m = re.search(r'Message:"((?:[^"\\]|\\.)*)"', msg)
    if m:
        try:
            msg = m.group(1).encode().decode("unicode_escape")
        except UnicodeDecodeError:
            msg = m.group(1)
    return msg[:limit]


METERING_URL = os.environ.get("METERING_URL", "http://metering.platform-system.svc:8080")


def usage(ns):
    """The tenant's own allocation summary from the metering service — the
    seconds behind the bill, readable before the statement arrives
    (customer walk 2026-08-27: nothing showed a tenant what it was holding)."""
    try:
        with urllib.request.urlopen(f"{METERING_URL}/usage?tenant={urllib.parse.quote(ns)}", timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise ApiError(502, f"metering answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ApiError(503, f"metering unavailable: {type(exc).__name__}") from exc


def workload_events(ns, name):
    evs = api("GET", f"/api/v1/namespaces/{ns}/events"
              f"?fieldSelector=involvedObject.name%3D{urllib.parse.quote(name)}"
              ).get("items", [])
    rows = [{"at": e.get("lastTimestamp") or e.get("eventTime") or "",
             "type": e.get("type"), "reason": e.get("reason"),
             "message": _plain_event((e.get("message") or ""))} for e in evs]
    rows.sort(key=lambda r: r["at"], reverse=True)
    return {"events": rows[:50]}


def delete_workload(ns, kind, name, delete_volume=False):
    if not name_ok(name):
        raise ApiError(400, "invalid resource name")
    root = f"/api/v1/namespaces/{ns}"
    paths = {
        "job": f"/apis/batch.volcano.sh/v1alpha1/namespaces/{ns}/jobs/{name}",
        "devmachine": root + f"/pods/{name}",
        "volume": root + f"/persistentvolumeclaims/{name}",
        "service": f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
    }
    if kind not in paths:
        raise ApiError(400, f"unknown kind {kind}")

    def get(path):
        try:
            return api("GET", path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def remove(path, obj):
        # Never delete a replacement object with the same name after a race.
        if obj:
            api("DELETE", path, {"apiVersion": "v1", "kind": "DeleteOptions",
                "preconditions": {"uid": obj["metadata"]["uid"],
                                  "resourceVersion": obj["metadata"]["resourceVersion"]},
                "propagationPolicy": "Background"})

    kept = None
    try:
        parent = get(paths[kind])
        if parent and kind == "devmachine" and (parent["metadata"].get("labels") or {}).get("arise.ai/kind") != "devmachine":
            raise ApiError(409, "this pod is not a development machine")
        # Existing pre-upgrade resources have no ownerReferences. Remove only
        # their labelled dependencies while their parent still exists. New
        # resources additionally have UID-based Kubernetes garbage collection.
        sides = (("configmaps", name + "-ssh"), ("services", name + "-ssh")) if kind == "devmachine" else (("services", name),) if kind == "service" else ()
        if parent:
            for plural, child_name in sides:
                path = root + f"/{plural}/{child_name}"
                obj = get(path)
                if not obj:
                    continue
                md = obj["metadata"]
                labels = md.get("labels") or {}
                refs = md.get("ownerReferences") or []
                owned = any(r.get("uid") == parent["metadata"]["uid"] for r in refs)
                legacy = (not refs and labels.get("arise.ai/managed-by") == "tenant-portal" and
                          (kind == "service" or labels.get("arise.ai/devmachine") == name))
                if owned or legacy:
                    remove(path, obj)
            remove(paths[kind], parent)
        if kind == "devmachine":
            path = root + f"/persistentvolumeclaims/{name}-data"
            pvc = get(path)
            if pvc:
                if delete_volume and (pvc["metadata"].get("labels") or {}).get("arise.ai/devmachine") == name:
                    remove(path, pvc)
                else:
                    kept = name + "-data"
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise k8s_error(exc) from exc
    log("INFO", "deleted", ns=ns, kind=kind, name=name, delete_volume=delete_volume)
    out = {"deleted": name}
    if kept:
        out["keptVolume"] = kept
        out["note"] = f"data volume {kept} kept (still counts against quota)"
    return out


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

    def _body(self):
        self.close_connection = True
        values = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(values) != 1 or not re.fullmatch(r"[0-9]{1,10}", values[0]):
            raise ApiError(400, "one valid Content-Length required")
        length = int(values[0])
        if length > 1024 * 1024:
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "incomplete request body")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("object required")
            return body
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "request body must be a JSON object")

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
                                 "grace": {"defaultSeconds": DEFAULT_GRACE_SECONDS,
                                           "capSeconds": GRACE_CAP_SECONDS},
                                 "grid": {"vcpuStep": 1, "memStepGi": 1,
                                          "storageStepGi": 10},
                                 "tenants": list(TENANTS)})
            elif path == "/api/overview":
                self._json(200, overview(self._ns()))
            elif path == "/api/usage":
                self._json(200, usage(self._ns()))
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
        try:
            body = self._body()
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
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

    def do_PUT(self):                                        # noqa: N802
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            body = self._body()
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
            return
        try:
            # /api/devmachines/<name>/ssh-key
            if len(parts) == 4 and parts[:2] == ["api", "devmachines"] and parts[3] == "ssh-key":
                self._json(200, rotate_ssh_key(self._ns(), parts[2], body))
            else:
                self._json(404, {"error": "not found"})
        except ApiError as exc:
            self._json(exc.code, {"error": exc.message})
        except Exception as exc:                             # noqa: BLE001
            log("ERROR", "PUT failed", path=self.path[:120], error_class=type(exc).__name__)
            self._json(500, {"error": "internal error"})

    def do_DELETE(self):                                     # noqa: N802
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        # /api/<kind-plural>/<name>
        kinds = {"jobs": "job", "devmachines": "devmachine",
                 "volumes": "volume", "services": "service"}
        try:
            if len(parts) == 3 and parts[0] == "api" and parts[1] in kinds:
                dv = (self._qs().get("deleteVolume") or ["false"])[0].lower() == "true"
                self._json(200, delete_workload(self._ns(), kinds[parts[1]], parts[2], dv))
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
    Handler.timeout = 15
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
