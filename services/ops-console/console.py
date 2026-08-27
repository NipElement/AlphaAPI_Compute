#!/usr/bin/env python3
"""
ARISE B300 Operations Console — read-mostly fleet UI.

=============================================================================
THE ONE ARCHITECTURAL RULE
=============================================================================
This console NEVER writes a node label, a taint, or a cordon. Its only
mutating capability is to create/patch a NodeOwnership object — i.e. to state
DESIRED intent. The Capacity Controller remains the single writer of observed
ownership.

That is not a stylistic preference. The entire safety argument of this system
rests on "one node, one owner, one writer". A console that could set
arise.ai/owner directly would be a supported, convenient, well-lit path around
every invariant the controller enforces — and it would be used, at 3am, by
someone under pressure. So the RBAC for this service account deliberately
omits nodes/* write verbs; see platform/overlays/lab/ui.yaml.

The UI also refuses to *offer* an action it knows is gated. Showing a live
"Reclaim" button on a node with active contracts, and only failing after the
click, trains operators to treat rejections as noise. Preconditions are
evaluated and displayed BEFORE the action is available.

Dependencies: Python standard library only.
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
GROUP = "infrastructure.arise.ai"
VERSION = "v1alpha1"
PLURAL = "nodeownerships"
PORT = int(os.environ.get("PORT", "8080"))
PROM = os.environ.get("PROMETHEUS_URL", "http://prometheus.monitoring.svc:9090")
FAKE_GPU = os.environ.get("FAKE_GPU_RESOURCE", "arise.dev/fake-gpu")
NODE_ID_LABEL = "arise.ai/node-id"
PAIR_LABEL = "arise.ai/pair"
OWNER_LABEL = "arise.ai/owner"
TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")

# The tenant list is DATA, not a constant: platform/tenants.yaml is the single
# source of truth, rendered into the platform-tenants ConfigMap and mounted
# here. It was a hardcoded tuple until 2026-08-27, which meant a newly
# onboarded customer's pods were INVISIBLE to the drain path — their workload
# would keep running on a node being handed to the marketplace or returned to
# the ARISE pool, i.e. still executing on hardware sold to someone else.
# The literals above are the fallback for a pod without the mount.
TENANTS_PATH = os.environ.get("TENANTS_PATH", "/etc/arise/tenants.json")


def load_tenant_namespaces():
    """Adopt the mounted register if present. Fails SOFT to the built-ins: a
    malformed file must not stop reconciliation, and the L0 gate
    (scripts/tenant-check.py) catches a mismatch before it can deploy."""
    global TENANT_NAMESPACES
    try:
        with open(TENANTS_PATH, encoding="utf-8") as fh:
            names = tuple(sorted(k for k in json.load(fh) if isinstance(k, str)))
        if not names:
            raise ValueError("register lists no tenants")
        TENANT_NAMESPACES = names
        log("INFO", "tenant register loaded", path=TENANTS_PATH,
            tenants=list(TENANT_NAMESPACES))
    except FileNotFoundError:
        log("INFO", "no tenant register mounted; using built-in defaults",
            path=TENANTS_PATH, tenants=list(TENANT_NAMESPACES))
    except Exception as exc:                                 # noqa: BLE001
        log("ERROR", "tenant register unreadable; using built-in defaults",
            path=TENANTS_PATH, error_class=type(exc).__name__)

SIM_VCPU = "arise.dev/sim-vcpu"
SIM_MEM = "arise.dev/sim-mem-gi"

# Hardware spec sheets keyed by the arise.ai/hw-profile node label. The
# dgx-b300 numbers come from the NVIDIA DGX B300 User Guide (versions.env has
# the citation); the CPU/storage SKUs are未定 placeholders and say so.
HW_PROFILES = {
    "dgx-b300": {
        "model": "NVIDIA DGX B300",
        "gpus": "8× B300 Blackwell Ultra · 288 GB HBM/卡 · 共 2.3 TB",
        "cpus": "2× Intel Xeon Platinum 6776P（64C/128T × 2 = 256 线程）",
        "memory": "2 TB DDR5（可扩 4 TB）",
        "storage": "8× 3.84 TB E1.S NVMe 缓存 + 2× 1.92 TB M.2 启动",
        "network": "8× ConnectX-8 800Gb/s IB + 2× BlueField-3 双口 400Gb/s",
        "interconnect": "2× NVLink 5 / NVSwitch",
        "power": "14.5 kW（12× 3.2 kW N+N）",
    },
    "generic-cpu": {
        "model": "CPU 池节点（SKU 未定 — 占位规格）",
        "cpus": "64 vCPU（占位）", "memory": "512 GiB（占位）",
        "storage": "本地 NVMe（待定）", "network": "待定",
    },
    "storage-server": {
        "model": "存储池服务器（SKU 未定）",
        "storage": "vePFS 等价并行文件系统（Lustre/Ceph/Weka 到货评估）",
    },
}


def log(level, msg, **kw):
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "component": "ops-console", "msg": msg}
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
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def prom(query):
    try:
        url = f"{PROM}/api/v1/query?query={urllib.parse.quote(query)}"
        with urllib.request.urlopen(url, timeout=6) as resp:
            return json.loads(resp.read())["data"]["result"]
    except Exception:                                        # noqa: BLE001
        return []


# =============================================================== model =====
def build_infra():
    """CPU pool + storage nodes: fleet members outside the DGX state machine.
    They have no owner lifecycle — surfacing them is inventory, not control."""
    nodes = api("GET", "/api/v1/nodes?labelSelector=arise.ai/role%20in%20(cpu,storage)"
                ).get("items", [])
    out = []
    for n in nodes:
        meta, spec, status = n["metadata"], n.get("spec", {}), n.get("status", {})
        alloc = status.get("allocatable", {})
        ready = next((c["status"] == "True" for c in status.get("conditions", [])
                      if c["type"] == "Ready"), False)
        out.append({
            "name": meta.get("labels", {}).get("arise.ai/aux-name",
                                               meta["name"]),
            "kindNode": meta["name"],
            "role": meta.get("labels", {}).get("arise.ai/role"),
            "hwProfile": meta.get("labels", {}).get("arise.ai/hw-profile"),
            "hw": HW_PROFILES.get(meta.get("labels", {}).get("arise.ai/hw-profile"), {}),
            "simVcpu": alloc.get(SIM_VCPU),
            "simMemGi": alloc.get(SIM_MEM),
            "ready": ready,
            "cordoned": bool(spec.get("unschedulable")),
            "cpu": alloc.get("cpu"),
            "memory": alloc.get("memory"),
            "tainted": any(t.get("key") == "arise.ai/storage-only"
                           for t in (spec.get("taints") or [])),
        })
    out.sort(key=lambda x: (x["role"], x["name"]))
    return out


def build_fleet():
    """Assemble the fleet view from OBSERVED cluster state.

    Contract facts come from NodeOwnership.status, which the controller
    populates from the adapter — the console does not query the marketplace
    itself. One reader of external truth, one writer of it.
    """
    nodes = api("GET", f"/api/v1/nodes?labelSelector={NODE_ID_LABEL}").get("items", [])
    try:
        crs = {c["metadata"]["name"]: c
               for c in api("GET", f"/apis/{GROUP}/{VERSION}/{PLURAL}").get("items", [])}
    except Exception:                                        # noqa: BLE001
        crs = {}
    pods = api("GET", "/api/v1/pods").get("items", [])

    alloc, tenant_pods = {}, {}
    sim_vcpu, sim_mem = {}, {}
    for p in pods:
        nn = p.get("spec", {}).get("nodeName")
        if not nn or p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        ns = p["metadata"]["namespace"]
        for c in p["spec"].get("containers", []):
            req = (c.get("resources", {}).get("requests") or {})
            if FAKE_GPU in req:
                alloc[nn] = alloc.get(nn, 0) + int(req[FAKE_GPU])
            for sim, bucket in ((SIM_VCPU, sim_vcpu), (SIM_MEM, sim_mem)):
                if sim in req:
                    bucket[nn] = bucket.get(nn, 0) + int(req[sim])
        if ns in TENANT_NAMESPACES:
            tenant_pods.setdefault(nn, []).append(f"{ns}/{p['metadata']['name']}")

    out = []
    for n in nodes:
        meta, spec, status = n["metadata"], n.get("spec", {}), n.get("status", {})
        name = meta["name"]
        labels = meta.get("labels", {})
        nid = labels.get(NODE_ID_LABEL)
        cr = crs.get(nid, {})
        crs_status = cr.get("status", {}) or {}
        crs_spec = cr.get("spec", {}) or {}
        capacity = int(status.get("allocatable", {}).get(FAKE_GPU, 0) or 0)
        ready = next((c["status"] == "True" for c in status.get("conditions", [])
                      if c["type"] == "Ready"), False)
        taints = [t.get("key") for t in (spec.get("taints") or [])]

        out.append({
            "nodeId": nid,
            "kindNode": name,
            "pair": labels.get(PAIR_LABEL, "-"),
            "owner": labels.get(OWNER_LABEL, "UNKNOWN"),
            "phase": crs_status.get("phase", "—"),
            "desiredOwner": crs_spec.get("desiredOwner"),
            "transitionId": crs_spec.get("transitionId"),
            "activeContracts": crs_status.get("activeContracts", 0),
            "listed": crs_status.get("listed", False),
            "rentalEndAt": crs_status.get("rentalEndAt"),
            "gpuTotal": capacity,
            "gpuUsed": alloc.get(name, 0),
            "hwProfile": labels.get("arise.ai/hw-profile"),
            "hw": HW_PROFILES.get(labels.get("arise.ai/hw-profile"), {}),
            "simVcpuTotal": int(status.get("allocatable", {}).get(SIM_VCPU, 0) or 0),
            "simVcpuUsed": sim_vcpu.get(name, 0),
            "simMemTotal": int(status.get("allocatable", {}).get(SIM_MEM, 0) or 0),
            "simMemUsed": sim_mem.get(name, 0),
            "tenantPods": tenant_pods.get(name, []),
            "ready": ready,
            "cordoned": bool(spec.get("unschedulable")),
            "taints": taints,
            "conditions": crs_status.get("conditions", []),
            "sanitization": crs_status.get("sanitizationResults", []),
            "hasCR": nid in crs,
        })
    out.sort(key=lambda x: x["nodeId"] or "")
    return out


def gate_check(node, target):
    """Why an action is or is not available — evaluated BEFORE it is offered.

    Returns (allowed, [reasons]). Reasons are shown to the operator whether or
    not the action is allowed, so the gate is legible rather than mysterious.
    """
    reasons, allowed = [], True
    owner = node["owner"]

    if target == owner:
        return False, [f"already {owner}"]

    if node["activeContracts"] > 0:
        if target in ("ARISE", "DIRECT", "MAINTENANCE"):
            allowed = False
            end = node["rentalEndAt"] or "unknown"
            reasons.append(
                f"BLOCKED — {node['activeContracts']} active contract(s); "
                f"earliest reclaim after {end}. unlist does not reclaim.")
        else:
            reasons.append(f"{node['activeContracts']} active contract(s) continue to run")

    if target == "VAST":
        if node["tenantPods"]:
            reasons.append(f"{len(node['tenantPods'])} tenant pod(s) will be drained first")
        if node["gpuUsed"] > 0:
            reasons.append(f"{node['gpuUsed']} simulated GPU(s) still allocated; "
                           "handover waits for zero")
        if not node["ready"]:
            allowed = False
            reasons.append("BLOCKED — node is NotReady; pre-list checks would fail")

    if target == "ARISE" and owner == "VAST" and node["activeContracts"] == 0:
        reasons.append("will run SANITIZING then the health gate before returning")

    if target == "MAINTENANCE":
        reasons.append("planned downtime: drains EVERY tenant (the DIRECT "
                       "resident included), cordons and taints; exit via "
                       "ARISE (sanitize + health) or DIRECT")
        if node["tenantPods"]:
            reasons.append(f"{len(node['tenantPods'])} tenant pod(s) will be drained")

    if owner == "MAINTENANCE" and target == "VAST":
        reasons.append("leaves maintenance straight onto the marketplace; "
                       "pre-list checks still gate")

    if owner == "QUARANTINED":
        allowed = False
        reasons.append("BLOCKED — node is QUARANTINED; requires human-approved repair")

    if node["phase"] in ("DRAINING", "SANITIZING", "HEALTH_CHECK"):
        allowed = False
        reasons.append(f"BLOCKED — transition already in progress ({node['phase']})")

    if not reasons:
        reasons.append("no gates outstanding")
    return allowed, reasons


def recent_events(limit=40):
    try:
        evs = api("GET", "/api/v1/namespaces/platform-system/events?limit=200").get("items", [])
    except Exception:                                        # noqa: BLE001
        return []
    rows = []
    for e in evs:
        if e.get("source", {}).get("component") != "capacity-controller":
            continue
        rows.append({
            "at": e.get("lastTimestamp") or e.get("eventTime") or "",
            "type": e.get("type", "Normal"),
            "reason": e.get("reason", ""),
            "object": e.get("involvedObject", {}).get("name", ""),
            "message": (e.get("message") or "")[:300],
        })
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:limit]


def active_alerts():
    out = []
    for r in prom('ALERTS{alertstate="firing"}'):
        m = r.get("metric", {})
        out.append({"name": m.get("alertname"), "severity": m.get("severity", "-"),
                    "node": m.get("node", "-")})
    return out


# ================================================================ HTTP =====
class Handler(BaseHTTPRequestHandler):
    server_version = "arise-ops-console/1.0"

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

    def do_GET(self):                                        # noqa: N802
        path = self.path.split("?")[0]
        try:
            # No HTML is served here. The SPA (web/, Vue 3 + Arco) is served
            # same-origin by the gateway, which proxies ONLY /api/* to this
            # process — so "/" is unreachable through the supported path.
            # The pre-gateway hand-rolled page was removed 2026-08-17 per the
            # decision recorded in runbooks/security-audit-2026-08.md.
            if path in ("/healthz", "/readyz"):
                self._json(200, {"status": "ok"})
            elif path == "/api/fleet":
                fleet = build_fleet()
                for n in fleet:
                    n["gates"] = {t: dict(zip(("allowed", "reasons"), gate_check(n, t)))
                                  for t in ("ARISE", "VAST", "DIRECT",
                                            "MAINTENANCE")}
                self._json(200, {
                    "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "nodes": fleet,
                    "infraNodes": build_infra(),
                    "alerts": active_alerts(),
                    "events": recent_events(),
                    "adapter": {"mode": os.environ.get("VAST_ADAPTER", "mock-v1"),
                                "productionEnabled": False},
                })
            else:
                self._json(404, {"error": "not found"})
        except urllib.error.HTTPError as exc:
            self._json(exc.code, {"error": exc.read().decode()[:400]})
        except Exception as exc:                             # noqa: BLE001
            log("ERROR", "request failed", path=path, error_class=type(exc).__name__)
            self._json(500, {"error": "internal error"})

    def do_POST(self):                                       # noqa: N802
        if self.path != "/api/transition":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return

        node = body.get("nodeId")
        target = body.get("desiredOwner")
        approver = (body.get("approvedBy") or "").strip()
        reason = (body.get("reason") or "").strip()

        if target not in ("ARISE", "VAST", "DIRECT", "QUARANTINED",
                          "MAINTENANCE"):
            self._json(400, {"error": "desiredOwner must be ARISE/VAST/DIRECT/"
                                      "QUARANTINED/MAINTENANCE"})
            return
        if not node:
            self._json(400, {"error": "nodeId required"})
            return
        # An unattributed ownership change is not auditable, and plan §9.4
        # requires an actor and approver on every P0 record.
        if len(approver) < 3:
            self._json(400, {"error": "approvedBy is required for an ownership change"})
            return

        # Re-evaluate the gate server-side. The UI hides blocked actions, but a
        # hidden button is a UX affordance, not a security control.
        fleet = {n["nodeId"]: n for n in build_fleet()}
        current = fleet.get(node)
        if not current:
            self._json(404, {"error": f"unknown node {node}"})
            return
        allowed, reasons = gate_check(current, target)
        if not allowed:
            log("WARN", "transition refused at the console gate",
                node=node, target=target, reasons=reasons)
            self._json(409, {"error": "blocked by gate", "reasons": reasons})
            return

        tid = body.get("transitionId") or f"ui-{int(time.time())}-{node}"
        spec = {"desiredOwner": target, "transitionId": tid,
                "requireSanitization": True, "approvedBy": approver}
        if current["pair"] and current["pair"] != "-":
            spec["pair"] = current["pair"]
        if reason:
            spec["reason"] = reason[:512]

        try:
            if current["hasCR"]:
                api("PATCH", f"/apis/{GROUP}/{VERSION}/{PLURAL}/{node}",
                    body={"spec": spec},
                    content_type="application/merge-patch+json")
                action = "patched"
            else:
                api("POST", f"/apis/{GROUP}/{VERSION}/{PLURAL}",
                    body={"apiVersion": f"{GROUP}/{VERSION}", "kind": "NodeOwnership",
                          "metadata": {"name": node}, "spec": spec})
                action = "created"
        except urllib.error.HTTPError as exc:
            self._json(exc.code, {"error": exc.read().decode()[:400]})
            return

        log("INFO", "transition requested", node=node, target=target,
            transition_id=tid, approver=approver, action=action)
        self._json(202, {"accepted": True, "action": action,
                         "transitionId": tid, "note": reasons})


# ================================================================ page =====

def main():
    load_tenant_namespaces()   # which namespaces count as tenant workloads
    log("INFO", "ops console listening", port=PORT,
        note="writes NodeOwnership intent only; never node labels")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
