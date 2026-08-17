#!/usr/bin/env python3
"""
fake-gpu-advertiser — simulated sizing resources + fault router.

=============================================================================
HONEST SCOPE STATEMENT (plan §8.1 requires every item be labelled
SIMULATED / CONTROL-PLANE / HARDWARE — this component is CONTROL-PLANE)
=============================================================================
Since 2026-08-16 the extended resource `arise.dev/fake-gpu` is served by a
REAL kubelet device plugin (services/fake-gpu-plugin, DaemonSet
fake-gpu-plugin). This component no longer touches that resource on GPU
nodes; kubelet's device manager owns its capacity/allocatable, device IDs
are injected into containers via Allocate (ARISE_FAKE_GPU_IDS), and health
flows through ListAndWatch — the three §8.2 rows that used to be OPEN here
are closed there. See runbooks/gaps.md §1.

What THIS component still does:
  1. PATCH the *sizing* resources arise.dev/sim-vcpu / sim-mem-gi onto GPU
     and CPU-pool nodes (status-capacity patch; these have no device
     identity, so a device plugin would add nothing).
  2. Keep the public fault API of plan §11.1 (/test/unhealthy, /test/healthy,
     /test/reset) at its historical address, now ROUTING each request to the
     plugin pod on the device's node. SCH-07 is byte-compatible: same URL,
     same payload — but the allocatable drop it asserts is now produced by
     kubelet, not by this controller.
  3. Serve the aggregate /devices and /metrics view. Health state shown here
     is the mirror of faults injected through this API; it is reconciled from
     the plugins' own /devices on every reconcile tick, so a plugin restart
     (which clears its in-memory faults) converges the mirror too.

Dependencies: Python standard library only. No pip install, no image build.
"""

import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
RESOURCE = os.environ.get("FAKE_GPU_RESOURCE", "arise.dev/fake-gpu")
PER_NODE = int(os.environ.get("FAKE_GPU_PER_NODE", "8"))
# Real DGX B300 magnitudes (versions.env cites the NVIDIA user guide). These
# are advertised as simulated extended resources so scheduler capacity math
# runs on the true numbers while the kind node's tiny native cpu/memory only
# carries the sleep processes.
SIM_VCPU = "arise.dev/sim-vcpu"
SIM_MEM = "arise.dev/sim-mem-gi"
GPU_NODE_PROFILE = {SIM_VCPU: int(os.environ.get("SIM_VCPU_GPU", "256")),
                    SIM_MEM:  int(os.environ.get("SIM_MEM_GPU", "2048"))}
CPU_NODE_PROFILE = {SIM_VCPU: int(os.environ.get("SIM_VCPU_CPU", "64")),
                    SIM_MEM:  int(os.environ.get("SIM_MEM_CPU", "512"))}
# kind node name -> role for aux nodes (cpu pool); storage stays unadvertised.
AUX_MAP = json.loads(os.environ.get("AUX_MAP_JSON", "{}"))
INTERVAL = int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "20"))
PORT = int(os.environ.get("PORT", "8080"))

# node-map: kind node name -> logical DGX id. Injected via env as JSON so the
# mapping stays in kind/node-map.yaml as the single source of truth.
NODE_MAP = json.loads(os.environ.get("NODE_MAP_JSON", "{}"))
REV_NODE_MAP = {v: k for k, v in NODE_MAP.items()}

# Where the per-node device plugin pods live (fault routing target).
PLUGIN_NAMESPACE = os.environ.get("PLUGIN_NAMESPACE", "platform-system")
PLUGIN_LABEL = os.environ.get("PLUGIN_LABEL",
                              "app.kubernetes.io/name=fake-gpu-plugin")
PLUGIN_PORT = int(os.environ.get("PLUGIN_PORT", "8080"))

# Simulated per-device health. device id -> healthy(bool).
# Key format: "<logical-node>-fake-gpu-<n>", stable and globally unique
# (plan §8.2 "设备 ID ... 稳定且全局唯一").
_health_lock = threading.Lock()
_unhealthy: set[str] = set()


def log(level: str, msg: str, **kw) -> None:
    """JSON logs — plan §9.5 requires machine-parsable controller logs."""
    rec = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "controller": "fake-gpu-advertiser",
        "msg": msg,
    }
    rec.update(kw)
    print(json.dumps(rec), flush=True)


# ------------------------------------------------------------- API client --
def _token() -> str:
    with open(f"{SA}/token", encoding="utf-8") as fh:
        return fh.read().strip()


def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=f"{SA}/ca.crt")


def api(method: str, path: str, body=None, content_type="application/json"):
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Accept", "application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", content_type)
        req.data = data
    with urllib.request.urlopen(req, context=_ctx(), timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def list_nodes() -> list[dict]:
    return api("GET", "/api/v1/nodes").get("items", [])


# ---------------------------------------------------------- fault routing --
def plugin_pods(node_name: str | None = None) -> list[dict]:
    """Running fake-gpu-plugin pods, optionally narrowed to one node."""
    fields = "status.phase=Running"
    if node_name:
        fields = f"spec.nodeName={node_name},{fields}"
    query = (f"labelSelector={urllib.parse.quote(PLUGIN_LABEL)}"
             f"&fieldSelector={urllib.parse.quote(fields)}")
    return api("GET",
               f"/api/v1/namespaces/{PLUGIN_NAMESPACE}/pods?{query}"
               ).get("items", [])


def plugin_call(pod_ip: str, method: str, path: str, body=None):
    req = urllib.request.Request(
        f"http://{pod_ip}:{PLUGIN_PORT}{path}", method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def route_device_fault(device: str, path: str, payload: dict):
    """Forward a fault to the plugin pod owning `device`; (code, body)."""
    logical = device.split("-fake-gpu-")[0] if "-fake-gpu-" in device else ""
    node = REV_NODE_MAP.get(logical)
    if not node:
        return 400, {"error": f"unknown device '{device}'"}
    pods = plugin_pods(node)
    if not pods:
        return 502, {"error": f"no running fake-gpu-plugin pod on {node}"}
    pod_ip = pods[0].get("status", {}).get("podIP")
    if not pod_ip:
        return 502, {"error": f"plugin pod on {node} has no IP yet"}
    try:
        return 200, plugin_call(pod_ip, "POST", path, payload)
    except Exception as exc:                              # noqa: BLE001
        return 502, {"error": f"forward to {node} failed: {exc}"}


def sync_health_mirror() -> None:
    """Converge the local fault mirror with each plugin's authoritative
    state, so /devices and /metrics stay honest across plugin restarts."""
    seen: set[str] = set()
    for pod in plugin_pods():
        pod_ip = pod.get("status", {}).get("podIP")
        if not pod_ip:
            continue
        try:
            seen.update(
                plugin_call(pod_ip, "GET", "/devices").get("unhealthy", []))
        except Exception:                                 # noqa: BLE001
            return  # plugin briefly unready; keep mirror, next tick retries
    with _health_lock:
        _unhealthy.clear()
        _unhealthy.update(seen)


def healthy_count(logical: str) -> int:
    """Display/metrics view of the fault mirror (kubelet owns the real
    allocatable; this feeds /devices and /metrics only)."""
    with _health_lock:
        dead = sum(
            1 for d in _unhealthy if d.startswith(f"{logical}-fake-gpu-")
        )
    return max(0, PER_NODE - dead)


def advertise(node_name: str, resources: dict) -> None:
    """PATCH status.capacity with extended resources.

    Resource names contain '/', which must be escaped as '~1' in a JSON Patch
    pointer (RFC 6901). Getting this wrong silently writes the wrong key.
    """
    patch = []
    for res, val in resources.items():
        pointer = res.replace("~", "~0").replace("/", "~1")
        patch.append({"op": "add",
                      "path": f"/status/capacity/{pointer}",
                      "value": str(val)})
    api(
        "PATCH",
        f"/api/v1/nodes/{node_name}/status",
        body=patch,
        content_type="application/json-patch+json",
    )


def reconcile_once() -> dict:
    """Idempotent: only patches nodes whose advertised value is wrong."""
    result = {"checked": 0, "patched": 0, "errors": 0, "nodes": {}}
    for node in list_nodes():
        name = node["metadata"]["name"]
        logical = NODE_MAP.get(name)
        aux_role = AUX_MAP.get(name)
        if not logical and aux_role != "cpu":
            # control-plane / storage / unmapped: ZERO simulated resources.
            continue
        result["checked"] += 1
        cap = node.get("status", {}).get("capacity", {})
        if logical:
            # arise.dev/fake-gpu is deliberately ABSENT here: the device
            # plugin DaemonSet owns it via kubelet (gaps.md §1 closure).
            # Patching it from this controller again would fight the device
            # manager's own status writes.
            want = dict(GPU_NODE_PROFILE)
        else:
            want = dict(CPU_NODE_PROFILE)
        key = logical or name
        result["nodes"][key] = {"node": name,
                                "want": want,
                                "have": {r: cap.get(r) for r in want}}
        if all(str(cap.get(r)) == str(v) for r, v in want.items()):
            continue
        try:
            advertise(name, want)
            result["patched"] += 1
            log("INFO", "advertised capacity", node=name,
                logical=logical or aux_role, now=want)
        except urllib.error.HTTPError as exc:
            result["errors"] += 1
            log("ERROR", "advertise failed", node=name,
                error_class="HTTPError", code=exc.code,
                detail=exc.read().decode()[:300])
        except Exception as exc:                      # noqa: BLE001
            result["errors"] += 1
            log("ERROR", "advertise failed", node=name,
                error_class=type(exc).__name__, detail=str(exc)[:300])
    return result


# ------------------------------------------------------------ HTTP surface --
class Handler(BaseHTTPRequestHandler):
    """Test + metrics endpoints. Bound to the pod IP, ClusterIP service only."""

    def _send(self, code: int, payload, ctype="application/json"):
        body = (json.dumps(payload) if ctype == "application/json"
                else payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):      # silence default stderr logging
        pass

    def do_GET(self):                                    # noqa: N802
        if self.path in ("/healthz", "/readyz"):
            self._send(200, {"status": "ok"})
        elif self.path == "/devices":
            with _health_lock:
                dead = sorted(_unhealthy)
            self._send(200, {
                "resource": RESOURCE,
                "perNode": PER_NODE,
                "nodes": {
                    logical: {
                        "devices": [f"{logical}-fake-gpu-{i}"
                                    for i in range(PER_NODE)],
                        "healthy": healthy_count(logical),
                    }
                    for logical in sorted(NODE_MAP.values())
                },
                "unhealthy": dead,
            })
        elif self.path == "/metrics":
            self._send(200, self._metrics(), "text/plain; version=0.0.4")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):                                   # noqa: N802
        # Fault injection surface (plan §11.1 "Device Plugin test endpoint
        # 标记一个 ID"). Deliberately additive-only and reversible.
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        if self.path == "/test/unhealthy":
            device = payload.get("device")
            if not device:
                self._send(400, {"error": "device required"})
                return
            code, body = route_device_fault(
                device, "/unhealthy",
                {"device": device, "faultId": payload.get("faultId")})
            if code != 200:
                log("ERROR", "fault routing failed", device=device, **body)
                self._send(code, body)
                return
            with _health_lock:
                _unhealthy.add(device)
            log("WARN", "device marked unhealthy (routed to plugin)",
                device=device, fault_id=payload.get("faultId"))
            self._send(200, {"device": device, "healthy": False})
        elif self.path == "/test/healthy":
            device = payload.get("device")
            if not device:
                self._send(400, {"error": "device required"})
                return
            code, body = route_device_fault(
                device, "/healthy", {"device": device})
            if code != 200:
                self._send(code, body)
                return
            with _health_lock:
                _unhealthy.discard(device)
            log("INFO", "device restored (routed to plugin)", device=device)
            self._send(200, {"device": device, "healthy": True})
        elif self.path == "/test/reset":
            errors = []
            for pod in plugin_pods():
                pod_ip = pod.get("status", {}).get("podIP")
                if not pod_ip:
                    continue
                try:
                    plugin_call(pod_ip, "POST", "/reset", {})
                except Exception as exc:                  # noqa: BLE001
                    errors.append(str(exc))
            with _health_lock:
                _unhealthy.clear()
            if errors:
                log("ERROR", "reset partially failed", errors=errors)
                self._send(502, {"reset": False, "errors": errors})
                return
            log("INFO", "all devices restored (routed to plugins)")
            self._send(200, {"reset": True})
        else:
            self._send(404, {"error": "not found"})

    def _metrics(self) -> str:
        lines = [
            "# HELP arise_fake_gpu_healthy Simulated GPU device health (1=healthy).",
            "# TYPE arise_fake_gpu_healthy gauge",
        ]
        with _health_lock:
            dead = set(_unhealthy)
        for logical in sorted(NODE_MAP.values()):
            for i in range(PER_NODE):
                dev = f"{logical}-fake-gpu-{i}"
                val = 0 if dev in dead else 1
                lines.append(
                    f'arise_fake_gpu_healthy{{node="{logical}",device="{dev}"}} {val}'
                )
        lines += [
            "# HELP arise_fake_gpu_capacity Advertised fake GPU capacity per node.",
            "# TYPE arise_fake_gpu_capacity gauge",
        ]
        for logical in sorted(NODE_MAP.values()):
            lines.append(
                f'arise_fake_gpu_capacity{{node="{logical}"}} {healthy_count(logical)}'
            )
        return "\n".join(lines) + "\n"


def serve() -> None:
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def main() -> int:
    if not NODE_MAP:
        log("ERROR", "NODE_MAP_JSON is empty; refusing to start")
        return 1
    log("INFO", "starting", resource=RESOURCE, per_node=PER_NODE,
        nodes=sorted(NODE_MAP.values()), port=PORT)
    threading.Thread(target=serve, daemon=True).start()
    while True:
        try:
            res = reconcile_once()
            if res["patched"] or res["errors"]:
                log("INFO", "reconcile", **{k: res[k] for k in
                                            ("checked", "patched", "errors")})
            sync_health_mirror()
        except Exception as exc:                          # noqa: BLE001
            log("ERROR", "reconcile loop error",
                error_class=type(exc).__name__, detail=str(exc)[:300])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
