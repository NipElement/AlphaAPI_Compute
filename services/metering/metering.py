#!/usr/bin/env python3
"""
Metering — the allocation ledger behind every invoice (plan: WS5).

=============================================================================
WHAT THIS IS
=============================================================================
The offer page sells $9.57 per GPU-hour, billed by the minute. Until this
service existed nothing in the platform recorded WHEN a tenant held a GPU.
This is the single writer of an append-only, hash-chained ledger of
allocation INTERVALS: one `open` record when a tenant pod holding GPUs (or
vCPUs) starts running, one `close` when it stops. Everything money-shaped —
per-tenant usage series, the CSV invoice, a future dispute — is derived from
that ledger, never from Prometheus (3-day emptyDir TSDB is not a source of
truth; the repo has always been explicit about that).

=============================================================================
THE THREE PROPERTIES THAT MATTER
=============================================================================
  1. EXACTLY-ONCE per pod. Intervals are keyed on the pod UID (a UID is never
     reused, unlike a name). A restart of this service replays the ledger
     tail, rebuilds the open set, and continues — the same discipline the
     capacity-controller uses with transitionIds (OWN-04, OWN-07).
  2. TAMPER-EVIDENT. Every record carries sha256(prev_hash + record). Editing
     or deleting a line breaks every hash after it. The chain is verified on
     start and exported as a metric, so a broken ledger is an ALERT, not a
     surprise at invoice time.
  3. HONEST TIMESTAMPS. `open.at` is the pod's own startTime (API-recorded,
     not our observation). `close.at` prefers the container's terminated
     finishedAt / the deletionTimestamp; a pod that vanished while the meter
     was down closes at the last instant it was SEEN holding resources —
     never at our restart time. Every record says where its timestamp came
     from (`at_source`), because a customer may ask.
  4. THE INTERVAL IS NODE RESIDENCY, not a container's run. A pod holds its
     GPUs from the moment it is scheduled (startTime) until its phase is
     terminal or it is gone — a container crash-looping inside it still
     holds the devices, so it neither closes nor re-opens the interval (the
     first cut did, and double-billed a restart from the original startTime).
     Init containers count: the effective request is k8s's own rule,
     max(max(initContainers), sum(containers)), so a job run entirely in an
     init container is billed exactly like one run in a main container.

What it deliberately does NOT do: price anything (billing/pricebook.yaml +
billing/invoice.py do), write to any other object, or mutate a pod.

Dependencies: Python standard library only.
"""

import calendar
import hashlib
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"

# Which resource IS "a GPU" here: the simulation resource in the lab, the real
# one on hardware. vCPU likewise (simulated magnitude vs native cpu).
GPU_RESOURCE = os.environ.get("GPU_RESOURCE", "arise.dev/fake-gpu")
VCPU_RESOURCE = os.environ.get("VCPU_RESOURCE", "arise.dev/sim-vcpu")
MEM_RESOURCE = os.environ.get("MEM_RESOURCE", "arise.dev/sim-mem-gi")
POLL = int(os.environ.get("POLL_SECONDS", "15"))
LEDGER_PATH = os.environ.get("LEDGER_PATH", "/ledger/allocations.jsonl")
# Side file (NOT part of the chain): last instant each open pod was SEEN
# holding its resources. When a pod vanishes while the meter was down, its
# interval closes at this instant — never at the meter's restart time. Meter
# downtime is our problem, not the customer's.
SEEN_PATH = os.environ.get("SEEN_PATH", "/ledger/last_seen.json")
TENANTS_PATH = os.environ.get("TENANTS_PATH", "/etc/arise/tenants.json")
PORT = int(os.environ.get("PORT", "8080"))
GENESIS = "0" * 64

TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")


def log(level, msg, **kw):
    rec = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "level": level, "component": "metering", "msg": msg}
    rec.update(kw)
    print(json.dumps(rec), flush=True)


def load_tenant_namespaces():
    """platform/tenants.yaml, rendered into the mounted register. Same soft
    fallback as every other consumer (scripts/tenant-check.py keeps them in
    agreement)."""
    global TENANT_NAMESPACES
    try:
        with open(TENANTS_PATH, encoding="utf-8") as fh:
            names = tuple(sorted(k for k in json.load(fh) if isinstance(k, str)))
        if names:
            TENANT_NAMESPACES = names
        log("INFO", "tenant register loaded", tenants=list(TENANT_NAMESPACES))
    except FileNotFoundError:
        log("INFO", "no tenant register mounted; using built-in defaults",
            tenants=list(TENANT_NAMESPACES))
    except Exception as exc:                                 # noqa: BLE001
        log("ERROR", "tenant register unreadable; using built-in defaults",
            error_class=type(exc).__name__)


# ================================================================ ledger ====
def canonical(rec: dict) -> bytes:
    """Stable bytes for hashing: the record WITHOUT its own hash field."""
    body = {k: v for k, v in rec.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def record_hash(prev: str, rec: dict) -> str:
    return hashlib.sha256(prev.encode() + canonical(rec)).hexdigest()


class Ledger:
    """Append-only JSONL with a sha256 chain. One writer (this process)."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.records: list[dict] = []
        self.head = GENESIS
        self.chain_ok = True
        self.broken_at = None
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            return
        prev = GENESIS
        raw = open(self.path, "rb").read()
        # Tolerate exactly ONE torn trailing fragment (a crash mid-append):
        # everything up to the last newline is authoritative; a partial last
        # line is truncated away and logged, instead of crash-looping the
        # meter until a human edits the PVC.
        if raw and not raw.endswith(b"\n"):
            cut = raw.rfind(b"\n") + 1
            log("WARN", "ledger has a torn trailing line; truncating it",
                dropped_bytes=len(raw) - cut)
            with open(self.path, "r+b") as fh:
                fh.truncate(cut)
                fh.flush()
                os.fsync(fh.fileno())
            raw = raw[:cut]
        lines = raw.decode("utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            if True:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                want = record_hash(prev, rec)
                if rec.get("prev") != prev or rec.get("hash") != want:
                    self.chain_ok = False
                    self.broken_at = lineno
                    log("ERROR", "LEDGER CHAIN BROKEN — records after this "
                        "line cannot be trusted", line=lineno,
                        expected_prev=prev, found_prev=rec.get("prev"))
                    # Keep loading so the open-set is still rebuilt, but the
                    # metric stays 0 until a human resolves it.
                self.records.append(rec)
                prev = rec.get("hash", want)
        self.head = prev
        log("INFO", "ledger loaded", records=len(self.records),
            chain_ok=self.chain_ok, head=self.head[:12])

    def append(self, rec: dict) -> dict:
        with self.lock:
            rec = dict(rec)
            rec["seq"] = len(self.records) + 1
            rec["prev"] = self.head
            rec["hash"] = record_hash(self.head, rec)
            line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())      # a ledger that can lose its tail
            self.records.append(rec)       # to a power cut is not a ledger
            self.head = rec["hash"]
            return rec

    def verify(self) -> tuple[bool, int | None]:
        prev = GENESIS
        for i, rec in enumerate(self.records, 1):
            if rec.get("prev") != prev or rec.get("hash") != record_hash(prev, rec):
                return False, i
            prev = rec["hash"]
        return True, None

    def open_set(self) -> dict:
        """pod_uid -> open record, for intervals not yet closed.

        ORDER-based: an open AFTER a close re-opens (segment semantics). The
        first cut subtracted "any uid with a close", which silently dropped a
        legitimately re-opened interval after a meter restart — an unbilled
        pod holding GPUs indefinitely."""
        opened = {}
        for r in self.records:
            if r["event"] == "open":
                opened[r["pod_uid"]] = r
            elif r["event"] == "close":
                opened.pop(r["pod_uid"], None)
        return opened

    def last_close_at(self) -> dict:
        """pod_uid -> at of its most recent close (for re-open floors)."""
        out = {}
        for r in self.records:
            if r["event"] == "close":
                out[r["pod_uid"]] = r["at"]
        return out


# ============================================================= k8s read =====
def _token():
    with open(f"{SA}/token", encoding="utf-8") as fh:
        return fh.read().strip()


def api_get(path: str):
    req = urllib.request.Request(f"{API}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Accept", "application/json")
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
        return json.loads(resp.read() or b"{}")


def list_tenant_pods() -> list[dict]:
    out = []
    for ns in TENANT_NAMESPACES:
        try:
            out.extend(api_get(f"/api/v1/namespaces/{ns}/pods").get("items", []))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:      # namespace not created yet: not an error
                continue
            raise
    return out


# ============================================================ observing =====
_MEM_UNITS = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4,
              "K": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12}


def _qty_int(v) -> int:
    """Extended resources are integers; native cpu may be '500m'. We only
    meter whole units (the flavor model is whole vCPUs), so '500m' rounds to
    0 — a platform sidecar, not a rentable core."""
    try:
        s = str(v)
        if s.endswith("m"):
            return int(s[:-1]) // 1000
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _mem_gi(v) -> int:
    """Memory in whole GiB: native quantities carry a unit suffix ('64Gi');
    the lab's simulated resource is already an integer of GiB."""
    try:
        s = str(v)
        for unit, mult in _MEM_UNITS.items():
            if s.endswith(unit):
                return int(float(s[:-len(unit)]) * mult) // (1024 ** 3)
        return int(float(s))                 # plain integer: already GiB
    except (TypeError, ValueError):
        return 0


def pod_footprint(pod: dict) -> dict:
    """The pod's EFFECTIVE request, by Kubernetes' own rule:
    max(max over initContainers, sum over containers). An init container that
    asks for 8 GPUs holds 8 GPUs while it runs — a job run entirely inside one
    is billed exactly like a main container."""
    spec = pod.get("spec", {})
    def one(c):
        req = (c.get("resources") or {}).get("requests") or {}
        return (_qty_int(req.get(GPU_RESOURCE, 0)),
                _qty_int(req.get(VCPU_RESOURCE, 0)),
                _mem_gi(req.get(MEM_RESOURCE, 0)))
    mains = [one(c) for c in spec.get("containers", [])]
    inits = [one(c) for c in spec.get("initContainers", [])]
    summed = tuple(sum(x[i] for x in mains) for i in range(3))
    peak_init = tuple(max((x[i] for x in inits), default=0) for i in range(3))
    eff = tuple(max(summed[i], peak_init[i]) for i in range(3))
    return {"gpu": eff[0], "vcpu": eff[1], "mem_gi": eff[2]}


TERMINAL = ("Succeeded", "Failed")


def pod_is_resident(pod: dict) -> bool:
    """Holding its resources on a node: scheduled (startTime set) and not in
    a terminal phase. A crash-looping container does NOT end residency — the
    devices stay allocated to the pod until it terminates or is deleted."""
    st = pod.get("status", {})
    return bool(st.get("startTime")) and st.get("phase") not in TERMINAL


def pod_is_terminal(pod: dict) -> bool:
    return pod.get("status", {}).get("phase") in TERMINAL


def pod_end_time(pod: dict) -> tuple[str, str]:
    """(iso timestamp, source) for when a pod stopped consuming."""
    st = pod.get("status", {})
    ends = []
    for cs in st.get("containerStatuses", []):
        term = (cs.get("state") or {}).get("terminated") or {}
        if term.get("finishedAt"):
            ends.append(term["finishedAt"])
    if ends:
        return max(ends), "containerStatuses.terminated.finishedAt"
    dt = pod.get("metadata", {}).get("deletionTimestamp")
    if dt:
        return dt, "metadata.deletionTimestamp"
    return now_iso(), "observed"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Meter:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.open = ledger.open_set()
        self.last_close = ledger.last_close_at()
        # uids we have already fully recorded (open+close) — so a terminal pod
        # lingering in the API is not re-recorded every tick
        self.done = set(self.last_close)
        self.seen = self._load_seen()
        self.ready = False
        self.errors = 0
        log("INFO", "open intervals rebuilt from ledger", count=len(self.open))

    # ---- last-seen side file --------------------------------------------
    def _load_seen(self) -> dict:
        try:
            with open(SEEN_PATH, encoding="utf-8") as fh:
                return {k: v for k, v in json.load(fh).items() if k in self.open}
        except (FileNotFoundError, ValueError):
            return {}

    def _save_seen(self):
        tmp = SEEN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.seen, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SEEN_PATH)

    # ---- one record each ------------------------------------------------
    def _open(self, pod, fp, at, src):
        uid = pod["metadata"]["uid"]
        # Re-open after an earlier close (segment semantics): the new segment
        # cannot start before the previous close, or the overlap bills twice.
        floor = self.last_close.get(uid)
        if floor and floor > at:
            at, src = floor, "previous close (re-open floor)"
        rec = {
            "event": "open", "pod_uid": uid,
            "tenant": pod["metadata"]["namespace"],
            "pod": pod["metadata"]["name"],
            "kind": (pod["metadata"].get("labels") or {}).get("arise.ai/kind", "pod"),
            "node": pod.get("spec", {}).get("nodeName", ""),
            **fp, "at": at, "at_source": src, "ts": now_iso(),
        }
        self.open[uid] = self.ledger.append(rec)
        log("INFO", "interval opened", tenant=rec["tenant"], pod=rec["pod"],
            gpu=fp["gpu"], vcpu=fp["vcpu"], at_source=src)

    def _close(self, uid, orec, at, src):
        rec = {"event": "close", "pod_uid": uid, "tenant": orec["tenant"],
               "pod": orec["pod"], "kind": orec["kind"], "node": orec["node"],
               "gpu": orec["gpu"], "vcpu": orec["vcpu"], "mem_gi": orec["mem_gi"],
               "at": at, "at_source": src, "ts": now_iso(),
               "opened_at": orec["at"]}
        self.ledger.append(rec)
        self.open.pop(uid, None)
        self.seen.pop(uid, None)
        self.last_close[uid] = at
        self.done.add(uid)
        log("INFO", "interval closed", tenant=rec["tenant"], pod=rec["pod"],
            gpu=rec["gpu"], at_source=src)

    def tick(self):
        pods = list_tenant_pods()
        now = now_iso()
        seen_pods = {}
        for pod in pods:
            uid = pod["metadata"]["uid"]
            seen_pods[uid] = pod
            fp = pod_footprint(pod)
            if fp["gpu"] == 0 and fp["vcpu"] == 0:
                continue                       # nothing rentable requested
            start = pod["status"].get("startTime")
            if not start:
                continue                       # not scheduled: holds nothing yet
            if uid in self.open:
                self.seen[uid] = now
                continue
            if pod_is_resident(pod):
                self._open(pod, fp, start, "status.startTime")
                self.seen[uid] = now
            elif pod_is_terminal(pod) and uid not in self.done:
                # Ran to completion BETWEEN two polls (or during a failed
                # tick): record the whole interval now, from the API's own
                # timestamps, instead of letting sub-poll work be free.
                self._open(pod, fp, start, "status.startTime")
                at, src = pod_end_time(pod)
                self._close(uid, self.open[uid], at, src)
        # Close: open intervals whose pod is terminal or gone.
        for uid, orec in list(self.open.items()):
            pod = seen_pods.get(uid)
            if pod is not None and pod_is_resident(pod):
                continue
            if pod is not None:
                at, src = pod_end_time(pod)
            else:
                # Gone while we were not looking: close at the last instant we
                # SAW it hold resources. If the meter was down for four hours,
                # those hours are ours, not the customer's.
                last = self.seen.get(uid)
                at, src = (last, "last-seen-holding") if last else (orec["at"], "opened_at (never re-seen)")
            self._close(uid, orec, at, src)
        self._save_seen()
        self.ready = True


# ============================================================== metrics =====
def _iso_to_epoch(s: str) -> float:
    """UTC in, UTC out. time.mktime interprets the tuple in LOCAL time and
    time.timezone ignores DST, so the first cut drifted by an hour on any
    non-UTC host across a DST boundary — statements differed by machine."""
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


def render_metrics(meter: Meter) -> str:
    led = meter.ledger
    with led.lock:                       # tick() mutates these concurrently
        records = list(led.records)
        open_now = list(meter.open.values())
        head = led.head
    ok, _ = led.verify()
    allocated, closed_secs = {}, {}
    for r in records:
        if r["event"] == "close":
            try:
                secs = max(0.0, _iso_to_epoch(r["at"]) - _iso_to_epoch(r["opened_at"]))
            except (ValueError, KeyError):
                secs = 0.0
            closed_secs[r["tenant"]] = closed_secs.get(r["tenant"], 0.0) + secs * r["gpu"]
    for r in open_now:
        allocated[r["tenant"]] = allocated.get(r["tenant"], 0) + r["gpu"]
    out = [
        "# HELP arise_metering_ledger_chain_ok 1 if every ledger record hashes to its predecessor.",
        "# TYPE arise_metering_ledger_chain_ok gauge",
        f"arise_metering_ledger_chain_ok {1 if ok else 0}",
        "# HELP arise_metering_ledger_records Records in the allocation ledger.",
        "# TYPE arise_metering_ledger_records gauge",
        f"arise_metering_ledger_records {len(records)}",
        "# HELP arise_metering_ledger_head_info Chain head (seq + hash prefix): an external anchor — a seq that goes DOWN is a truncated ledger.",
        "# TYPE arise_metering_ledger_head_info gauge",
        f'arise_metering_ledger_head_info{{head="{head[:16]}"}} {len(records)}',
        "# HELP arise_metering_open_intervals Pods currently holding metered resources.",
        "# TYPE arise_metering_open_intervals gauge",
        f"arise_metering_open_intervals {len(meter.open)}",
        "# HELP arise_metering_gpu_allocated GPUs currently held, per tenant.",
        "# TYPE arise_metering_gpu_allocated gauge",
    ]
    for t in TENANT_NAMESPACES:
        out.append(f'arise_metering_gpu_allocated{{tenant="{t}"}} {allocated.get(t, 0)}')
    out += ["# HELP arise_metering_gpu_seconds_total GPU-seconds from CLOSED intervals, per tenant (ledger-derived).",
            "# TYPE arise_metering_gpu_seconds_total counter"]
    for t in TENANT_NAMESPACES:
        out.append(f'arise_metering_gpu_seconds_total{{tenant="{t}"}} {closed_secs.get(t, 0.0):.0f}')
    out.append("# HELP arise_metering_poll_errors_total Failed list cycles.")
    out.append("# TYPE arise_metering_poll_errors_total counter")
    out.append(f"arise_metering_poll_errors_total {meter.errors}")
    return "\n".join(out) + "\n"


METER: Meter | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                    # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            self._send(200, {"status": "ok"})
        elif path == "/readyz":
            ok = METER is not None and METER.ready and METER.ledger.chain_ok
            self._send(200 if ok else 503,
                       {"status": "ok" if ok else "not ready or ledger chain broken"})
        elif path == "/metrics":
            self._send(200, render_metrics(METER), "text/plain; version=0.0.4")
        elif path == "/ledger":
            # Internal read surface (platform-internal-ingress fences it):
            # records for one pod UID, or the chain status. Used by MTR-01
            # and by the invoice tooling; never by tenants.
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            uid = params.get("uid")
            ok, broken = METER.ledger.verify()
            recs = [r for r in METER.ledger.records if not uid or r["pod_uid"] == uid]
            self._send(200, {"chain_ok": ok, "broken_at": broken,
                             "records": recs[-200:], "total": len(METER.ledger.records)})
        else:
            self._send(404, {"error": "not found"})


def main():
    global METER
    load_tenant_namespaces()
    ledger = Ledger(LEDGER_PATH)
    METER = Meter(ledger)
    log("INFO", "metering starting", gpu_resource=GPU_RESOURCE,
        vcpu_resource=VCPU_RESOURCE, ledger=LEDGER_PATH, poll_seconds=POLL,
        tenants=list(TENANT_NAMESPACES))
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever(),
        daemon=True).start()
    while True:
        try:
            METER.tick()
        except Exception as exc:                             # noqa: BLE001
            METER.errors += 1
            log("ERROR", "poll failed", error_class=type(exc).__name__,
                detail=str(exc)[:300])
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
