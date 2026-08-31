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
import hmac
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


# Optional keyed chain. Verified 2026-08-30: with a PLAIN sha256 chain, anyone
# who can write the file can rewrite history end-to-end and `chain_ok` stays
# true (the algorithm is in this repo) — a spliced record moved a test invoice
# from $114.84 to $51,563.16 undetected. An HMAC key that lives ONLY in the
# metering pod's Secret raises the bar to "you also need the key"; the key
# does NOT defend against the meter itself, which is what the external anchor
# (the head series Prometheus keeps, and invoice.py --expect-head) is for.
# Absent key = plain sha256, so the lab's existing ledger keeps verifying.
CHAIN_KEY_FILE = os.environ.get("CHAIN_KEY_FILE", "/etc/arise-chain/key")
try:
    with open(CHAIN_KEY_FILE, "rb") as _fh:
        CHAIN_KEY = _fh.read().strip()
except OSError:
    CHAIN_KEY = b""
CHAIN_MODE = "hmac-sha256" if CHAIN_KEY else "sha256"


def record_hash(prev: str, rec: dict) -> str:
    body = prev.encode() + canonical(rec)
    if CHAIN_KEY:
        return hmac.new(CHAIN_KEY, body, hashlib.sha256).hexdigest()
    return hashlib.sha256(body).hexdigest()


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
        if not self.chain_ok and self.broken_at == 1 and self.records:
            # Breaking at the FIRST record usually means the chain MODE does
            # not match the file, which looks exactly like tampering. Only one
            # direction is positively identifiable: we hold a key and record 1
            # verifies unkeyed. The reverse (a keyed file read without its key)
            # is indistinguishable from a real forgery — say so rather than
            # guess.
            first = self.records[0]
            plain = hashlib.sha256(GENESIS.encode() + canonical(first)).hexdigest()
            if CHAIN_KEY and first.get("hash") == plain:
                log("ERROR", "this ledger was written with the UNKEYED chain but "
                             "the process has a chain key: its history cannot be "
                             "verified in this mode. Archive it with its own "
                             "verification, then start a fresh keyed ledger; do "
                             "NOT invoice from a chain that does not verify",
                    running_mode=CHAIN_MODE, file_mode="sha256")
            else:
                log("ERROR", "ledger fails from record 1: either it was written "
                             "under a DIFFERENT chain key than this process holds, "
                             "or record 1 was altered. Both are stop-and-escalate "
                             "(runbooks/incident-metering.md)",
                    running_mode=CHAIN_MODE)
        log("INFO", "ledger loaded", records=len(self.records),
            chain_ok=self.chain_ok, head=self.head[:12], chain_mode=CHAIN_MODE)

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


TIER_LABEL = "arise.ai/tier"
_ns_cache: dict = {"at": 0.0, "names": ()}
NS_CACHE_TTL = 60.0


def metered_namespaces() -> tuple:
    """Every namespace whose usage must reach the ledger.

    The UNION of the mounted register and the cluster's own
    arise.ai/tier=tenant namespaces. Usage that is never MEASURED cannot be
    recovered later — there is no second copy of "what ran last Tuesday" — so
    the failure directions are not symmetric: a namespace missing here is
    revenue silently lost, while an extra one produces ledger records whose
    invoice refuses to guess a tenant kind until it is registered (a loud,
    fixable state).

    Until 2026-08-31 TENANT_NAMESPACES was a hardcoded tuple that an
    unreadable register left in place, so a tenant onboarded after this
    process started was not metered at all. Found by onboarding a third
    tenant end to end.
    """
    now = time.time()
    if now - _ns_cache["at"] < NS_CACHE_TTL and _ns_cache["names"]:
        return _ns_cache["names"]
    live = ()
    try:
        live = tuple(n["metadata"]["name"] for n in api_get(
            f"/api/v1/namespaces?labelSelector={TIER_LABEL}%3Dtenant"
        ).get("items", []))
    except Exception as exc:                                  # noqa: BLE001
        log("WARN", "could not list tenant namespaces; metering the register "
                    "alone", error_class=type(exc).__name__)
    names = tuple(sorted(set(TENANT_NAMESPACES) | set(live)))
    if names and set(names) != set(_ns_cache["names"]):
        log("INFO", "metered namespace set", register=list(TENANT_NAMESPACES),
            labelled=list(live), union=list(names))
    if names:
        _ns_cache.update(at=now, names=names)
    return names


def list_tenant_pods() -> list[dict]:
    out = []
    for ns in metered_namespaces():
        try:
            out.extend(api_get(f"/api/v1/namespaces/{ns}/pods").get("items", []))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:      # namespace not created yet: not an error
                continue
            raise
    return out


def list_tenant_pvcs() -> list[dict]:
    """Bound claims hold NVMe the offer sells as '30 TB included'. Metered as
    intervals of their own (kind=volume) so a statement shows what a tenant
    holds even when the line prices at $0 (billing/pricebook.yaml)."""
    out = []
    for ns in metered_namespaces():
        try:
            out.extend(api_get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims").get("items", []))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
    return out


def _storage_gib(q) -> int:
    s = str(q or "0")
    for suf, mult in _MEM_UNITS.items():
        if s.endswith(suf):
            try:
                return int(float(s[:-len(suf)]) * mult) // (1024 ** 3)
            except ValueError:
                return 0
    try:
        return int(float(s)) // (1024 ** 3)
    except ValueError:
        return 0


def pvc_as_subject(pvc: dict):
    """A Bound claim in the same shape _open() consumes for a pod: metadata +
    a footprint. Capacity is what the cluster GRANTED (status.capacity), not
    what was asked for."""
    if (pvc.get("status") or {}).get("phase") != "Bound":
        return None, None
    md = pvc["metadata"]
    gib = _storage_gib((pvc.get("status") or {}).get("capacity", {}).get("storage")
                       or (pvc.get("spec") or {}).get("resources", {}).get("requests", {}).get("storage"))
    if gib <= 0:
        return None, None
    subject = {"metadata": {"uid": md["uid"], "namespace": md["namespace"], "name": md["name"],
                            "labels": {"arise.ai/kind": "volume"}},
               "spec": {"nodeName": ""}}
    fp = {"gpu": 0, "vcpu": 0, "mem_gi": 0, "storage_gib": gib,
          "storage_class": (pvc.get("spec") or {}).get("storageClassName", "")}
    return subject, fp


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
        """Best-effort. This file is deliberately OUTSIDE the hash chain, so
        losing it must never be fatal — and it is loaded in __init__, which
        means an exception here is a metering CRASH LOOP, i.e. no billing
        records at all.

        Until 2026-08-31 only FileNotFoundError and ValueError were caught, so
        a file that parses as JSON but is not an object (`[]`, `null`, `5` —
        a hand-edit during an incident, a filesystem that returned a plausible
        block) raised AttributeError on .items() and took the meter down. The
        blast radius of ignoring it is bounded and known: a missing last-seen
        entry closes its interval at opened_at, which bills the customer
        ZERO for it. Under-billing on unreadable state, never over-billing.
        """
        try:
            with open(SEEN_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:
            log("ERROR", "last-seen side file unreadable; every open interval "
                         "will close at opened_at (billed as zero) if its pod "
                         "vanishes before the next poll",
                path=SEEN_PATH, error_class=type(exc).__name__)
            return {}
        if not isinstance(raw, dict):
            log("ERROR", "last-seen side file is not a JSON object; ignoring it",
                path=SEEN_PATH, found=type(raw).__name__)
            return {}
        return {k: v for k, v in raw.items() if k in self.open}

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
        if orec.get("kind") == "volume":
            rec["storage_gib"] = orec.get("storage_gib", 0)
            rec["storage_class"] = orec.get("storage_class", "")
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
        # Volumes: a Bound claim holds capacity from the first tick we see it
        # bound (at_source observed(bound): the API records no bind time) until
        # it is gone (last-seen, like a pod). Never re-opened while bound.
        try:
            pvcs = list_tenant_pvcs()
        except Exception as exc:                        # noqa: BLE001
            # Volumes are the $0 line; GPUs are the money. An RBAC slip on
            # claims must degrade to "no storage lines this tick", never stop
            # GPU metering (the lab rollout of 2026-08-27 hit exactly this).
            log("ERROR", "pvc list failed; skipping volumes this tick",
                error_class=type(exc).__name__, detail=str(exc)[:200])
            self.errors += 1
            pvcs = [{"__skip__": True}]
        if pvcs and pvcs[0].get("__skip__"):
            for uid, orec in self.open.items():
                if orec.get("kind") == "volume":
                    seen_pods[uid] = {"__volume__": True}   # keep open, do not close on a blind tick
            pvcs = []
        for pvc in pvcs:
            subject, fp = pvc_as_subject(pvc)
            if subject is None:
                continue
            uid = subject["metadata"]["uid"]
            seen_pods[uid] = {"__volume__": True}
            if uid in self.open:
                self.seen[uid] = now
                continue
            self._open(subject, fp, now, "observed(bound)")
            self.seen[uid] = now
        # Close: open intervals whose pod is terminal or gone.
        for uid, orec in list(self.open.items()):
            pod = seen_pods.get(uid)
            if pod is not None and pod.get("__volume__"):
                continue                       # still bound this tick
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


def usage_summary(meter: "Meter", tenant: str, now: str | None = None) -> dict:
    """What ONE tenant holds and has held, from the ledger — the customer-
    facing read (portal /api/usage). Open intervals count up to `now`; the
    numbers are seconds, not money: prices live in billing/pricebook.yaml and
    the statement is billing/invoice.py (D6)."""
    now = now or now_iso()
    now_e = _iso_to_epoch(now)
    with meter.ledger.lock:
        records = list(meter.ledger.records)
        open_now = dict(meter.open)
    rows, gpu_s, gpu_open, stor_gib_s, stor_gib_open = [], 0.0, 0, 0.0, 0
    for r in records:
        if r.get("tenant") != tenant or r["event"] != "close":
            continue
        try:
            secs = max(0.0, _iso_to_epoch(r["at"]) - _iso_to_epoch(r["opened_at"]))
        except (ValueError, KeyError):
            secs = 0.0
        if r.get("kind") == "volume":
            stor_gib_s += secs * int(r.get("storage_gib", 0))
        else:
            gpu_s += secs * int(r.get("gpu", 0))
        rows.append({"name": r["pod"], "kind": r.get("kind"), "opened_at": r.get("opened_at"),
                     "closed_at": r["at"], "seconds": int(secs), "gpu": r.get("gpu", 0),
                     "storage_gib": r.get("storage_gib", 0), "open": False})
    for uid, o in open_now.items():
        if o.get("tenant") != tenant:
            continue
        try:
            secs = max(0.0, now_e - _iso_to_epoch(o["at"]))
        except (ValueError, KeyError):
            secs = 0.0
        if o.get("kind") == "volume":
            stor_gib_s += secs * int(o.get("storage_gib", 0)); stor_gib_open += int(o.get("storage_gib", 0))
        else:
            gpu_s += secs * int(o.get("gpu", 0)); gpu_open += int(o.get("gpu", 0))
        rows.append({"name": o["pod"], "kind": o.get("kind"), "opened_at": o["at"], "closed_at": None,
                     "seconds": int(secs), "gpu": o.get("gpu", 0), "storage_gib": o.get("storage_gib", 0), "open": True})
    rows.sort(key=lambda x: x["opened_at"] or "", reverse=True)
    return {"tenant": tenant, "as_of": now,
            "gpu_hours": round(gpu_s / 3600, 2), "gpu_allocated_now": gpu_open,
            "storage_gib_hours": round(stor_gib_s / 3600, 1), "storage_gib_now": stor_gib_open,
            "intervals": rows[:500], "interval_count": len(rows),
            "note": "seconds from the allocation ledger (pod residency / bound volumes); prices are on the statement"}


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
    storage = {}
    for r in open_now:
        allocated[r["tenant"]] = allocated.get(r["tenant"], 0) + r["gpu"]
        if r.get("kind") == "volume":
            storage[r["tenant"]] = storage.get(r["tenant"], 0) + int(r.get("storage_gib", 0))
    out = [
        "# HELP arise_metering_ledger_chain_ok 1 if every ledger record hashes to its predecessor.",
        "# TYPE arise_metering_ledger_chain_ok gauge",
        f"arise_metering_ledger_chain_ok {1 if ok else 0}",
        "# HELP arise_metering_ledger_records Records in the allocation ledger.",
        "# TYPE arise_metering_ledger_records gauge",
        f"arise_metering_ledger_records {len(records)}",
        "# HELP arise_metering_ledger_chain_mode 1 for the chain mode in use; hmac-sha256 needs the key to forge.",
        "# TYPE arise_metering_ledger_chain_mode gauge",
        f'arise_metering_ledger_chain_mode{{mode="{CHAIN_MODE}"}} 1',
        "# HELP arise_metering_ledger_head_info Chain head (seq + hash prefix): an external anchor — a seq that goes DOWN is a truncated ledger.",
        "# TYPE arise_metering_ledger_head_info gauge",
        # The FULL head, not a prefix: this label is the only external anchor
        # an auditor can compare a month later, and a truncated one would let a
        # determined rewrite grind for a collision (2026-08-30 review of this
        # very metric). Cardinality is unchanged — the head changes per append
        # either way; only the string is longer.
        f'arise_metering_ledger_head_info{{head="{head}"}} {len(records)}',
        "# HELP arise_metering_open_intervals Pods currently holding metered resources.",
        "# TYPE arise_metering_open_intervals gauge",
        f"arise_metering_open_intervals {len(meter.open)}",
        "# HELP arise_metering_storage_gib_allocated Bound volume capacity (GiB) currently held, per tenant.",
        "# TYPE arise_metering_storage_gib_allocated gauge",
        *[f'arise_metering_storage_gib_allocated{{tenant="{t}"}} {storage.get(t, 0)}' for t in sorted(set(allocated) | set(storage))],
        "# HELP arise_metering_gpu_allocated GPUs currently held, per tenant.",
        "# TYPE arise_metering_gpu_allocated gauge",
    ]
    # The union, not the register: a tenant onboarded after startup would
    # otherwise have its usage in the LEDGER but no series in Prometheus —
    # invisible on every dashboard while it is being billed (2026-08-31).
    for t in metered_namespaces():
        out.append(f'arise_metering_gpu_allocated{{tenant="{t}"}} {allocated.get(t, 0)}')
    out += ["# HELP arise_metering_gpu_seconds_total GPU-seconds from CLOSED intervals, per tenant (ledger-derived).",
            "# TYPE arise_metering_gpu_seconds_total counter"]
    for t in metered_namespaces():
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
        elif path == "/usage":
            # Customer-facing summary for ONE tenant (the portal calls it on
            # the tenant's behalf; the gateway pins the tenant upstream).
            params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            tenant = params.get("tenant", "")
            if tenant not in metered_namespaces():
                self._send(404, {"error": "unknown tenant"})
            else:
                self._send(200, usage_summary(METER, tenant))
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
    log("INFO", "metering starting", chain_mode=CHAIN_MODE, gpu_resource=GPU_RESOURCE,
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
