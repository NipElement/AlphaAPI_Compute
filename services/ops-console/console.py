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
        if target in ("ARISE", "DIRECT"):
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

    def _html(self, body_bytes):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # This console can request transitions; a clickjacked frame must not.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):                                        # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._html(PAGE.encode())
            elif path in ("/healthz", "/readyz"):
                self._json(200, {"status": "ok"})
            elif path == "/api/fleet":
                fleet = build_fleet()
                for n in fleet:
                    n["gates"] = {t: dict(zip(("allowed", "reasons"), gate_check(n, t)))
                                  for t in ("ARISE", "VAST", "DIRECT")}
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

        if target not in ("ARISE", "VAST", "DIRECT", "QUARANTINED"):
            self._json(400, {"error": "desiredOwner must be ARISE/VAST/DIRECT/QUARANTINED"})
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
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARISE B300 — Fleet Console</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#14161a; --muted:#666e7a; --line:#e2e5ea;
  --arise:#1f7a4d; --vast:#a86400; --direct:#5b3fa8; --quar:#b3261e; --unknown:#5c6470;
  --ok:#1f7a4d; --warn:#a86400; --bad:#b3261e;
  --shadow:0 1px 2px rgba(0,0,0,.06),0 4px 12px rgba(0,0,0,.04);
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#0f1216; --panel:#171b21; --ink:#e7ebf0; --muted:#98a2b0; --line:#262c35;
  --arise:#4ec98a; --vast:#e0a13a; --direct:#a58cf0; --quar:#f2837a; --unknown:#8b94a1;
  --ok:#4ec98a; --warn:#e0a13a; --bad:#f2837a;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
 padding:16px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
h1{font-size:16px;margin:0;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:12.5px}
.grow{flex:1}
main{padding:20px 22px;max-width:1400px;margin:0 auto}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;
 color:var(--muted);margin:26px 0 10px;font-weight:600}
.pairs{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:16px}
.pair{background:var(--panel);border:1px solid var(--line);border-radius:12px;
 padding:14px;box-shadow:var(--shadow)}
.pair h3{margin:0 0 10px;font-size:12.5px;color:var(--muted);font-weight:600;
 display:flex;justify-content:space-between}
.nodes{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.node{border:1px solid var(--line);border-radius:10px;padding:12px;position:relative;
 border-left:4px solid var(--owner,var(--unknown))}
.node .id{font-weight:650;font-size:15px;letter-spacing:-.01em}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;
 font-weight:650;letter-spacing:.02em;color:#fff;background:var(--owner,var(--unknown))}
.k{color:var(--muted);font-size:11.5px}
.row{display:flex;justify-content:space-between;gap:8px;margin-top:5px;font-size:12.5px}
.bar{height:5px;border-radius:3px;background:var(--line);overflow:hidden;margin-top:7px}
.bar>i{display:block;height:100%;background:var(--owner,var(--unknown))}
.flag{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:4px;
 border:1px solid var(--line);color:var(--muted);margin:5px 4px 0 0}
.flag.bad{color:var(--bad);border-color:var(--bad)}
.flag.warn{color:var(--warn);border-color:var(--warn)}
.contract{margin-top:8px;padding:7px 9px;border-radius:7px;font-size:12px;
 background:color-mix(in srgb,var(--warn) 12%,transparent);
 border:1px solid color-mix(in srgb,var(--warn) 45%,transparent)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;
 padding:14px;box-shadow:var(--shadow)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.cols{grid-template-columns:1fr}.nodes{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase}
td.msg{color:var(--muted)}
.alert{padding:10px 12px;border-radius:9px;margin-bottom:8px;font-size:13px;
 background:color-mix(in srgb,var(--bad) 12%,transparent);
 border:1px solid color-mix(in srgb,var(--bad) 50%,transparent)}
select,input,button{font:inherit;padding:7px 9px;border-radius:7px;
 border:1px solid var(--line);background:var(--bg);color:var(--ink)}
button{cursor:pointer;font-weight:600;background:var(--ink);color:var(--bg);border:none}
button:disabled{opacity:.4;cursor:not-allowed}
.field{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}
.field label{font-size:11.5px;color:var(--muted);font-weight:600}
.gate{font-size:12px;margin-top:8px;padding:9px 10px;border-radius:7px;
 border:1px solid var(--line);color:var(--muted)}
.gate .blocked{color:var(--bad);font-weight:600}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
.note{color:var(--muted);font-size:11.5px;margin-top:10px;line-height:1.5}
</style></head><body>
<header>
  <h1>ARISE B300 — Fleet Console</h1>
  <span class="sub" id="meta">loading…</span>
  <span class="grow"></span>
  <span class="sub" id="adapter"></span>
</header>
<main>
  <div id="alerts"></div>
  <h2>Fleet</h2>
  <div class="pairs" id="pairs"></div>
  <h2>Infrastructure pool (outside the ownership state machine)</h2>
  <div class="pairs" id="infra"></div>
  <div class="cols" style="margin-top:26px">
    <div>
      <h2>Request ownership change</h2>
      <div class="panel">
        <div class="field"><label>Node</label><select id="f-node"></select></div>
        <div class="field"><label>Target owner</label><select id="f-target">
          <option>ARISE</option><option>VAST</option><option>DIRECT</option></select></div>
        <div class="field"><label>Approver (recorded in the audit trail)</label>
          <input id="f-approver" placeholder="you@ariselabs.ai"></div>
        <div class="field"><label>Reason (optional)</label>
          <input id="f-reason" placeholder="e.g. CHG-20260811-002 capacity release"></div>
        <div class="gate" id="gate"></div>
        <div style="margin-top:12px"><button id="submit">Request transition</button></div>
        <div class="note">
          This console only writes the <b>desired</b> state (a NodeOwnership object).
          The Capacity Controller remains the sole writer of node labels, taints and
          cordons — so nothing here can bypass the drain, contract or sanitization gates.
        </div>
        <div id="result" class="note"></div>
      </div>
    </div>
    <div>
      <h2>Controller audit trail</h2>
      <div class="panel" style="max-height:520px;overflow:auto">
        <table><thead><tr><th>Time</th><th>Node</th><th>Reason</th><th>Message</th></tr></thead>
        <tbody id="events"></tbody></table>
      </div>
    </div>
  </div>
</main>
<script>
const OWNER_VAR={ARISE:'--arise',VAST:'--vast',DIRECT:'--direct',
                 QUARANTINED:'--quar',UNKNOWN:'--unknown'};
let STATE=null;

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

function nodeCard(n){
  const v=OWNER_VAR[n.owner]||'--unknown';
  const pct=n.gpuTotal? Math.round(100*n.gpuUsed/n.gpuTotal):0;
  const flags=[];
  if(!n.ready) flags.push('<span class="flag bad">NotReady</span>');
  if(n.cordoned) flags.push('<span class="flag warn">cordoned</span>');
  if(n.listed) flags.push('<span class="flag warn">listed</span>');
  if(n.taints.includes('arise.ai/vast-owned')) flags.push('<span class="flag">vast-owned taint</span>');
  if(!n.hasCR) flags.push('<span class="flag">no NodeOwnership</span>');
  const contract = n.activeContracts>0 ? `<div class="contract">
      <b>${n.activeContracts} active contract${n.activeContracts>1?'s':''}</b> —
      reclaim is blocked until they end${n.rentalEndAt?`, earliest <span class="mono">${esc(n.rentalEndAt)}</span>`:''}.
      <div class="k" style="margin-top:3px">unlist stops new contracts; it does not reclaim a rented machine.</div>
    </div>` : '';
  return `<div class="node" style="--owner:var(${v})">
    <div class="row" style="margin:0">
      <span class="id">${esc(n.nodeId)}</span>
      <span class="badge">${esc(n.owner)}</span>
    </div>
    <div class="row"><span class="k">phase</span><span>${esc(n.phase)}</span></div>
    <div class="row"><span class="k">simulated GPU</span><span>${n.gpuUsed} / ${n.gpuTotal}</span></div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="row"><span class="k">tenant pods</span><span>${n.tenantPods.length}</span></div>
    ${n.transitionId?`<div class="row"><span class="k">transition</span><span class="mono">${esc(n.transitionId)}</span></div>`:''}
    <div>${flags.join('')}</div>
    ${contract}
  </div>`;
}

function render(d){
  STATE=d;
  document.getElementById('meta').textContent =
    `${d.nodes.length} nodes · updated ${d.generatedAt}`;
  document.getElementById('adapter').textContent =
    `VAST adapter: ${d.adapter.mode} · production ${d.adapter.productionEnabled?'ENABLED':'disabled'}`;

  document.getElementById('alerts').innerHTML = (d.alerts||[]).map(a=>
    `<div class="alert"><b>${esc(a.severity)} ${esc(a.name)}</b>${a.node&&a.node!=='-'?` — node ${esc(a.node)}`:''}</div>`
  ).join('');

  const pairs={};
  d.nodes.forEach(n=>{(pairs[n.pair]=pairs[n.pair]||[]).push(n);});
  document.getElementById('pairs').innerHTML = Object.keys(pairs).sort().map(p=>{
    const ns=pairs[p];
    const owners=[...new Set(ns.map(n=>n.owner))];
    const split = owners.length>1
      ? `<span style="color:var(--warn)">split ownership: ${owners.join(' / ')}</span>`
      : `<span>${owners[0]}</span>`;
    return `<div class="pair"><h3><span>pair ${esc(p)}</span>${split}</h3>
      <div class="nodes">${ns.map(nodeCard).join('')}</div></div>`;
  }).join('');

  document.getElementById('infra').innerHTML = (d.infraNodes||[]).map(n=>{
    const flags=[];
    if(!n.ready) flags.push('<span class="flag bad">NotReady</span>');
    if(n.cordoned) flags.push('<span class="flag warn">cordoned</span>');
    if(n.tainted) flags.push('<span class="flag">storage-only taint</span>');
    return `<div class="pair"><div class="node" style="--owner:var(--unknown)">
      <div class="row" style="margin:0"><span class="id">${esc(n.name)}</span>
        <span class="badge">${esc(n.role)}</span></div>
      <div class="row"><span class="k">allocatable</span>
        <span>${esc(n.cpu)} cpu · ${esc(n.memory)}</span></div>
      <div>${flags.join('')||'<span class="flag">healthy</span>'}</div>
    </div></div>`;
  }).join('') || '<div class="pair"><span class="k">no infra nodes</span></div>';

  const sel=document.getElementById('f-node'), keep=sel.value;
  sel.innerHTML=d.nodes.map(n=>`<option value="${esc(n.nodeId)}">${esc(n.nodeId)} — ${esc(n.owner)}</option>`).join('');
  if(keep) sel.value=keep;

  document.getElementById('events').innerHTML=(d.events||[]).map(e=>
    `<tr><td class="mono">${esc((e.at||'').replace('T',' ').replace('Z',''))}</td>
     <td>${esc(e.object)}</td>
     <td style="color:${e.type==='Warning'?'var(--warn)':'inherit'}">${esc(e.reason)}</td>
     <td class="msg">${esc(e.message)}</td></tr>`).join('')
     || '<tr><td colspan="4" class="msg">no controller events yet</td></tr>';

  updateGate();
}

function updateGate(){
  if(!STATE) return;
  const id=document.getElementById('f-node').value;
  const target=document.getElementById('f-target').value;
  const n=STATE.nodes.find(x=>x.nodeId===id);
  const g=n&&n.gates?n.gates[target]:null;
  const box=document.getElementById('gate');
  if(!g){box.textContent='select a node';document.getElementById('submit').disabled=true;return;}
  box.innerHTML = g.reasons.map(r=>
    r.startsWith('BLOCKED')?`<div class="blocked">${esc(r)}</div>`:`<div>${esc(r)}</div>`
  ).join('');
  // A blocked action is not offered. Failing only after the click teaches
  // operators to treat rejections as noise.
  document.getElementById('submit').disabled = !g.allowed;
}

async function load(){
  try{
    const r=await fetch('/api/fleet',{cache:'no-store'});
    render(await r.json());
  }catch(e){ document.getElementById('meta').textContent='refresh failed: '+e; }
}

document.getElementById('f-node').addEventListener('change',updateGate);
document.getElementById('f-target').addEventListener('change',updateGate);
document.getElementById('submit').addEventListener('click',async()=>{
  const body={nodeId:document.getElementById('f-node').value,
              desiredOwner:document.getElementById('f-target').value,
              approvedBy:document.getElementById('f-approver').value,
              reason:document.getElementById('f-reason').value};
  const out=document.getElementById('result');
  out.textContent='submitting…';
  const r=await fetch('/api/transition',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  out.innerHTML = r.ok
    ? `<b style="color:var(--ok)">accepted</b> — ${esc(j.action)} transitionId <span class="mono">${esc(j.transitionId)}</span>.
       The controller will act on the next reconcile; watch the audit trail.`
    : `<b style="color:var(--bad)">rejected</b> — ${esc(j.error)}${j.reasons?': '+esc(j.reasons.join('; ')):''}`;
  load();
});
load(); setInterval(load,5000);
</script></body></html>
"""


def main():
    log("INFO", "ops console listening", port=PORT,
        note="writes NodeOwnership intent only; never node labels")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
