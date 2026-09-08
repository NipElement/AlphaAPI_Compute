#!/usr/bin/env python3
"""
Capacity Controller — the single writer of node ownership (plan §8.3, §8.5, §8.6).

=============================================================================
THE INVARIANT THIS EXISTS TO PROTECT
=============================================================================
A node has exactly one owner at any instant. If Kubernetes and VAST both
believe they may schedule onto the same GPU, that is a P0 incident (plan §2
"关键成功标准"). Everything below — cordon before evict, evict before list,
contract check before reclaim, quarantine on uncertainty — is downstream of
that one sentence.

Second, weaker-looking but equally hard rule: **unlist is not reclaim**. A
listed machine that has an active contract must be allowed to finish serving
it. So `activeContracts > 0` blocks SANITIZING unconditionally, and no retry,
timeout or operator impatience may bypass it.

Third: any external call whose outcome is UNKNOWN must never be blindly
retried. We key every side effect on the transitionId and, on uncertainty,
QUERY before acting. If the query itself is unavailable, we go to QUARANTINED
and stop — a stuck-but-safe node beats a double-listed one (plan §8.5
"失败语义").

Dependencies: Python standard library only.
"""

from dataclasses import dataclass
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
GROUP = "infrastructure.arise.ai"
VERSION = "v1alpha1"
PLURAL = "nodeownerships"

VAST_BASE = os.environ.get("VAST_API_BASE", "http://vast-mock.vast-mock.svc:8080")
VAST_ADAPTER = os.environ.get("VAST_ADAPTER", "mock-v1")
VAST_PRODUCTION_ENABLED = os.environ.get(
    "VAST_PRODUCTION_ADAPTER_ENABLED", "false").lower() == "true"
FAKE_GPU = os.environ.get("FAKE_GPU_RESOURCE", "arise.dev/fake-gpu")
INTERVAL = int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "10"))
DRAIN_TIMEOUT = int(os.environ.get("DRAIN_TIMEOUT_SECONDS", "120"))
PORT = int(os.environ.get("PORT", "8080"))
STATE_NS = os.environ.get("STATE_NAMESPACE", "platform-system")
STATE_CM = os.environ.get("STATE_CONFIGMAP", "capacity-controller-state")

OWNER_LABEL = "arise.ai/owner"
TENANT_LABEL = "arise.ai/tenant"
NODE_ID_LABEL = "arise.ai/node-id"
TRANSITION_TAINT = "arise.ai/transition"
VAST_TAINT = "arise.ai/vast-owned"
# A DIRECT node is reserved for a specific customer. Unlike a VAST node it
# stays SCHEDULABLE — the customer's own work must land on it — so isolation
# comes from a taint plus the admission gate, not from a cordon.
DIRECT_TAINT = "arise.ai/direct-owned"
# PLANNED downtime (firmware, cabling, RAID work). Distinct from QUARANTINED —
# no suspicion, no human-approved-repair ceremony — and distinct from the
# transition taint so ops tooling can tell "being moved" from "being serviced".
# Still arise.ai/*, so the tenant owner-gate's toleration ban covers it.
MAINT_TAINT = "arise.ai/maintenance"

TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")

# The tenant list is DATA, not a constant: platform/tenants.yaml is the single
# source of truth, rendered into the platform-tenants ConfigMap and mounted
# here. It was a hardcoded tuple until 2026-08-27, which meant a newly
# onboarded customer's pods were INVISIBLE to the drain path — their workload
# would keep running on a node being handed to the marketplace or returned to
# the ARISE pool, i.e. still executing on hardware sold to someone else.
# The literals above are the fallback for a pod without the mount.
TENANTS_PATH = os.environ.get("TENANTS_PATH", "/etc/arise/tenants.json")


# Tenants whose kind is "customer" — the ones a DIRECT reservation can be FOR.
CUSTOMER_TENANTS = ("tenant-direct",)


_TENANTS_MTIME = None


def maybe_reload_tenants():
    """Re-read the register whenever the mounted file changes. Onboarding a
    tenant edits platform/tenants.yaml and re-renders the ConfigMap; without
    this the new tenant stayed INVISIBLE to drains and volume gates until
    someone remembered to restart the controller (review 2026-08-27)."""
    global _TENANTS_MTIME
    try:
        m = os.stat(TENANTS_PATH).st_mtime_ns
    except FileNotFoundError:
        return
    if m != _TENANTS_MTIME:
        _TENANTS_MTIME = m
        load_tenant_namespaces()


def load_tenant_namespaces():
    """Adopt the mounted register if present. Fails SOFT to the built-ins: a
    malformed file must not stop reconciliation, and the L0 gate
    (scripts/tenant-check.py) catches a mismatch before it can deploy."""
    global TENANT_NAMESPACES, CUSTOMER_TENANTS
    try:
        with open(TENANTS_PATH, encoding="utf-8") as fh:
            reg = json.load(fh)
        names = tuple(sorted(k for k in reg if isinstance(k, str)))
        if not names:
            raise ValueError("register lists no tenants")
        TENANT_NAMESPACES = names
        CUSTOMER_TENANTS = tuple(
            n for n in names
            if (reg[n] or {}).get("kind", "customer") == "customer")
        log("INFO", "tenant register loaded", path=TENANTS_PATH,
            tenants=list(TENANT_NAMESPACES),
            customer_tenants=list(CUSTOMER_TENANTS))
    except FileNotFoundError:
        log("INFO", "no tenant register mounted; using built-in defaults",
            path=TENANTS_PATH, tenants=list(TENANT_NAMESPACES))
    except Exception as exc:                                 # noqa: BLE001
        log("ERROR", "tenant register unreadable; using built-in defaults",
            path=TENANTS_PATH, error_class=type(exc).__name__)


TIER_LABEL = "arise.ai/tier"
_ns_cache: dict = {"at": 0.0, "names": ()}
NS_CACHE_TTL = 30.0


def isolation_namespaces() -> tuple:
    """Every namespace whose pods must be off a node before it changes hands.

    The UNION of the mounted register and the cluster's own
    arise.ai/tier=tenant namespaces, because the two failure directions are
    not symmetric:

      - one namespace MISSING means a paying customer's pods stay on a machine
        handed to the marketplace — dual tenancy on sold hardware, the exact
        thing this platform exists to prevent;
      - one namespace EXTRA means we evict pods from a node that is being
        taken out of service anyway. Harmless.

    So every source is additive and none can subtract. Until 2026-08-31 this
    was a hardcoded tuple that `load_tenant_namespaces()` REPLACED, and an
    unreadable register silently fell back to it: a tenant onboarded after
    that constant was written would not be drained. Measured by onboarding a
    third tenant end to end — the static gate named the gap, and the runtime
    would have carried it.

    Fails CLOSED: if the API cannot be asked AND no register was loaded, the
    caller gets None and must refuse the handover rather than drain a set it
    cannot vouch for.
    """
    now = time.time()
    if now - _ns_cache["at"] < NS_CACHE_TTL and _ns_cache["names"]:
        return _ns_cache["names"]
    live = ()
    try:
        items = api("GET", f"/api/v1/namespaces?labelSelector={TIER_LABEL}%3Dtenant"
                    ).get("items", [])
        live = tuple(n["metadata"]["name"] for n in items)
    except Exception as exc:                                  # noqa: BLE001
        log("WARN", "could not list tenant namespaces; falling back to the "
                    "register alone", error_class=type(exc).__name__)
    names = tuple(sorted(set(TENANT_NAMESPACES) | set(live)))
    if not names:
        # Fail CLOSED. pods_on_node(node, ()) filters against an empty set and
        # returns NO pods, so an empty answer here would tell the drain the
        # node is clean and hand it over with a customer still on it — the
        # exact hole this function was written to close, reintroduced from the
        # other side (caught reviewing the change itself, 2026-08-31).
        # Raising aborts the reconcile before any state changes, leaves the
        # node cordoned in DRAINING, and counts toward ControllerReconcileErrors.
        raise RuntimeError(
            "cannot enumerate tenant namespaces: the register is empty and "
            "the API returned none. Refusing to reason about isolation.")
    if set(names) != set(_ns_cache["names"]):
        log("INFO", "isolation namespace set", register=list(TENANT_NAMESPACES),
            labelled=list(live), union=list(names))
    _ns_cache.update(at=now, names=names)
    return names


_metrics_lock = threading.Lock()
_metrics = {
    "reconcile_errors_total": {},
    "transitions_total": {},
    "owner": {},
    "contracts": {},
    "policy_denials_total": {},
    # node -> phase, straight from the NodeOwnership status. The owner series
    # is derived from a NODE LABEL, so it says nothing at all when the node
    # cannot be resolved — which is exactly the state a NodeNotFound
    # quarantine is in. The CR always exists (2026-08-31).
    "phase": {},
    # node -> (startedAt, phase) while a transition is IN FLIGHT. Absent for a
    # settled node. Without it a transition that hangs is invisible: every
    # other series (owner, contracts) reads perfectly healthy while the node
    # sits cordoned out of the fleet (audit 2026-08-31).
    "transition": {},
}

# Phases a node passes THROUGH. Everything else is a resting state a node may
# legitimately sit in for months.
TRANSITIONAL_PHASES = ("PENDING", "DRAINING", "SANITIZING", "HEALTH_CHECK")


# =========================================================== plumbing ======
def log(level: str, msg: str, **kw) -> None:
    """JSON logs with the fields plan §9.5 mandates."""
    rec = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "component": "capacity-controller",
        "msg": msg,
    }
    rec.update(kw)
    print(json.dumps(rec), flush=True)


def _token() -> str:
    with open(f"{SA}/token", encoding="utf-8") as fh:
        return fh.read().strip()


# Transient server-side conditions worth retrying. 429 in particular is what
# the API server returns (with Retry-After) for LIST/WATCH against a CRD that
# is still being established — observed on nodeownerships at 12:45:46 on
# 2026-08-11, self-resolving. That is a back-off condition, not an error.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRY_BUDGET = int(os.environ.get("RETRY_BUDGET", "4"))


def _backoff_delay(attempt: int, retry_after: str | None) -> float:
    """Exponential backoff with jitter, honouring Retry-After (plan VST-05)."""
    if retry_after:
        try:
            base = min(float(retry_after), 30.0)
        except ValueError:
            base = min(2.0 ** attempt, 16.0)
    else:
        base = min(2.0 ** attempt, 16.0)
    # Jitter avoids a thundering herd when several controllers wake together.
    return base + random.uniform(0.0, base * 0.25)


def _open_with_retry(req, ctx=None, timeout=20):
    """Retry ONLY safe methods.

    A GET may be repeated freely. A POST/PATCH may NOT be retried here: an
    external write whose outcome is unknown must be resolved by querying with
    its idempotency key, which is the state machine's job (plan §8.5
    "失败语义" — never blind-retry into a duplicate side effect). So mutating
    requests propagate their exception upward untouched.
    """
    safe = req.get_method() in ("GET", "HEAD")
    last: Exception | None = None
    for attempt in range(RETRY_BUDGET if safe else 1):
        try:
            return urllib.request.urlopen(req, context=ctx, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            if not safe or exc.code not in RETRYABLE_STATUS:
                raise
            if attempt == RETRY_BUDGET - 1:
                raise
            delay = _backoff_delay(attempt, exc.headers.get("Retry-After"))
            log("INFO", "transient response; backing off",
                code=exc.code, attempt=attempt + 1, delay_seconds=round(delay, 2),
                url=req.full_url)
            time.sleep(delay)
    raise last if last else RuntimeError("retry loop exhausted")


def api(method: str, path: str, body=None, content_type="application/json"):
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", content_type)
        req.data = json.dumps(body).encode()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    with _open_with_retry(req, ctx=ctx, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def http_json(method: str, url: str, body=None, timeout=10):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    with _open_with_retry(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


# ======================================================== VAST adapter ====
class VastAdapter:
    """Mock-only adapter.

    The production adapter is disabled by TWO independent mechanisms, as
    plan §8.4 requires ("通过构建标签和运行时开关双重禁用"):
      1. this class refuses to construct if the production flag is set, and
      2. no production endpoint exists anywhere in this file to call.
    Test VST-06 asserts all three of network, config and runtime state.
    """

    def __init__(self, base: str):
        if VAST_PRODUCTION_ENABLED:
            raise RuntimeError(
                "VAST production adapter is disabled in Phase A and this build "
                "contains no production code path. Refusing to start.")
        if VAST_ADAPTER != "mock-v1":
            raise RuntimeError(f"unknown adapter {VAST_ADAPTER!r}")
        self.base = base.rstrip("/")

    def get(self, mid: str):
        return http_json("GET", f"{self.base}/v1/machines/{mid}")

    def list_machine(self, mid: str, idempotency_key: str):
        return http_json("POST", f"{self.base}/v1/machines/{mid}/list",
                         {"idempotencyKey": idempotency_key})

    def unlist_machine(self, mid: str):
        return http_json("POST", f"{self.base}/v1/machines/{mid}/unlist", {})


class NoMarketplaceAdapter:
    """No-marketplace adapter (VAST_ADAPTER=none) — DGX day-0.

    On delivered hardware before VST-06 ships there IS no marketplace: nothing
    was ever listed and no marketplace contract can exist. This adapter states
    that fact instead of dialing a mock — the dgx overlay deliberately excludes
    vast-mock, because a fake marketplace living beside real contracts is how
    someone "tests" against it by accident. Without this adapter the reconciler
    would freeze every transition on AdapterUnavailable, including the
    ARISE<->DIRECT reservations the day-0 product actually sells.

      get()            -> the truthful steady state: unlisted, zero contracts.
      list_machine()   -> refuses. A handover to a marketplace that is not
                          configured must fail fast (reconcile also gates
                          desired=VAST upfront, before any drain starts).
      unlist_machine() -> success no-op: "not listed" is already true, and
                          quarantine() calls this best-effort — a refusal here
                          would weaken isolation, not improve safety.

    reconcile() additionally refuses to touch any node whose observed state
    mentions VAST while this adapter is active: that combination means the
    config was downgraded under live marketplace state, and UNKNOWN is never
    interpreted as zero (same doctrine as E2E-03).
    """

    def __init__(self):
        if VAST_PRODUCTION_ENABLED:
            raise RuntimeError(
                "VAST production adapter is disabled and this build contains "
                "no production code path. Refusing to start.")

    def get(self, mid: str):
        return 200, {"listed": False, "activeContracts": 0,
                     "rentalEndAt": None}

    def list_machine(self, mid: str, idempotency_key: str):
        raise RuntimeError(
            f"VAST_ADAPTER=none: no marketplace is configured; listing "
            f"{mid!r} is impossible until the production adapter ships "
            f"under VST-06 approval")

    def unlist_machine(self, mid: str):
        return 200, {"unlisted": True, "noop": True}


# ======================================================== k8s helpers =====

def list_crs():
    return api("GET", f"/apis/{GROUP}/{VERSION}/{PLURAL}").get("items", [])


def patch_status(name: str, status: dict):
    api("PATCH", f"/apis/{GROUP}/{VERSION}/{PLURAL}/{name}/status",
        body={"status": status}, content_type="application/merge-patch+json")


def get_node_by_logical(node_id: str):
    nodes = api("GET",
                f"/api/v1/nodes?labelSelector={NODE_ID_LABEL}%3D{node_id}"
                ).get("items", [])
    return nodes[0] if nodes else None


def patch_node(name: str, patch: dict):
    api("PATCH", f"/api/v1/nodes/{name}", body=patch,
        content_type="application/merge-patch+json")


def set_owner_label(node_name: str, owner: str):
    labels = {OWNER_LABEL: owner}
    if owner != "DIRECT":
        labels[TENANT_LABEL] = None
    patch_node(node_name, {"metadata": {"labels": labels}})


def set_direct_binding(node_name: str, tenant: str):
    patch_node(node_name, {"metadata": {"labels": {
        OWNER_LABEL: "DIRECT", TENANT_LABEL: tenant}}})


TAINT_CAS_ATTEMPTS = 4


def set_taints(node_name: str, taints: list[dict], resource_version: str | None = None):
    body: dict = {"spec": {"taints": taints}}
    if resource_version:
        # A merge patch carrying metadata.resourceVersion is a precondition:
        # the API server returns 409 instead of clobbering a concurrent write.
        body["metadata"] = {"resourceVersion": resource_version}
    patch_node(node_name, body)


def cordon(node_name: str, on: bool = True):
    patch_node(node_name, {"spec": {"unschedulable": on}})


def update_taints(node_name: str, add: list[dict] | None = None,
                  remove: list[str] | None = None):
    """Apply taint additions and removals in ONE patch, from a FRESH read.

    Two separate add/remove calls computed from the same cached Node object
    silently undo each other: the second patch writes a taint list derived
    from pre-first-patch state. That is how the arise.ai/vast-owned taint went
    missing on dgx03 on 2026-08-11 — added, then erased microseconds later by
    the adjacent remove. Cordoning also mutates taints underneath us, so the
    read must be fresh, not the object reconcile started with.

    The write is a COMPARE-AND-SET: the whole taint list is replaced, so a
    taint someone else added between our read and our write (the kubelet's
    condition taints, the GPU Operator's nvidia.com/gpu during driver
    install) would be silently erased by a stale list. Sending the
    resourceVersion we read makes the API server refuse that write with 409,
    and we retry from a fresh read (falsification audit 2026-08-30).
    """
    for attempt in range(TAINT_CAS_ATTEMPTS):
        node = api("GET", f"/api/v1/nodes/{node_name}")
        rv = (node.get("metadata") or {}).get("resourceVersion")
        taints = node.get("spec", {}).get("taints") or []
        drop = set(remove or [])
        drop.update(t["key"] for t in (add or []))
        taints = [t for t in taints if t.get("key") not in drop]
        taints.extend(add or [])
        try:
            set_taints(node_name, taints, resource_version=rv)
            return
        except urllib.error.HTTPError as exc:
            if exc.code != 409 or attempt == TAINT_CAS_ATTEMPTS - 1:
                raise
            log("INFO", "node changed under a taint update; re-reading",
                node=node_name, attempt=attempt + 1)


def taint(key: str, value: str, effect: str = "NoSchedule") -> dict:
    return {"key": key, "value": value, "effect": effect}


def pods_on_node(node_name: str, namespaces=None):
    pods = api("GET",
               f"/api/v1/pods?fieldSelector=spec.nodeName%3D{node_name}"
               ).get("items", [])
    if namespaces is None:
        return pods
    return [p for p in pods if p["metadata"]["namespace"] in namespaces]


TERMINAL_PHASES = ("Succeeded", "Failed")


def live_tenant_pods(node_name: str) -> list:
    """Tenant pods that are still RUNNING on this node.

    One definition, because three places used to disagree. The drain excluded
    Succeeded/Failed pods; run_sanitization() and pre_list_checks() counted
    every pod object. So a customer job that finished NORMALLY left a
    Succeeded pod, the drain said the node was clean, and the sanitization
    gate immediately after it said it was not — quarantining the node with
    "customer_workloads_stopped" on the happy path. Reproduced 2026-09-01:
    a single Succeeded pod drove ARISE reclaim to QUARANTINED, while an
    identical run with no pods reached HEALTH_CHECK.

    A terminal pod holds nothing — no CPU, no memory, no GPU; the kubelet has
    already released them — so it is not a workload that is "still running".
    It is NOT deleted here either: those objects are the customer's own record
    of their job, in the customer's namespace, and the node's on-disk residue
    is what data_erasure (HW-12) exists to handle. They are reported instead,
    so "there were leftovers" stays visible in the evidence rather than being
    silently dropped.
    """
    return [pod for pod in pods_on_node(node_name, isolation_namespaces())
            if (pod.get("status") or {}).get("phase") not in TERMINAL_PHASES]


def terminal_tenant_pods(node_name: str) -> list:
    return [pod for pod in pods_on_node(node_name, isolation_namespaces())
            if (pod.get("status") or {}).get("phase") in TERMINAL_PHASES]


def fake_gpu_allocated(node_name: str) -> int:
    total = 0
    for p in pods_on_node(node_name):
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for c in p["spec"].get("containers", []):
            req = (c.get("resources", {}).get("requests") or {})
            total += int(req.get(FAKE_GPU, 0) or 0)
    return total


PRODUCT_STORAGE_CLASSES = ("arise-shared", "arise-longterm")


def tenant_pvs_on_node(node_name: str, namespaces=None) -> list[str]:
    """Product-class PersistentVolumes pinned to node_name, as 'ns/claim'.

    A local-path PV carries required nodeAffinity on kubernetes.io/hostname —
    it is physically a directory on ONE node's NVMe. Ownership handovers gate
    on this (2026-08-26): moving a node under a DIRECT customer / a
    marketplace / back to the ARISE pool while another tenant's volumes still
    sit on it would strand the data behind a taint its owner cannot tolerate
    AND leave it on hardware someone else pays for. Restricting to the two
    product classes keeps the gate scoped to volumes this platform created.
    """
    out = []
    for pv in api("GET", "/api/v1/persistentvolumes").get("items", []):
        spec = pv.get("spec", {})
        if spec.get("storageClassName") not in PRODUCT_STORAGE_CLASSES:
            continue
        terms = (((spec.get("nodeAffinity") or {}).get("required") or {})
                 .get("nodeSelectorTerms") or [])
        pinned = any(
            any(e.get("key") == "kubernetes.io/hostname"
                and node_name in (e.get("values") or [])
                for e in (t.get("matchExpressions") or []))
            for t in terms)
        if not pinned:
            continue
        claim = spec.get("claimRef") or {}
        ns = claim.get("namespace", "")
        # No claimRef (the "clear claimRef to rebind" dance on a Retain PV)
        # means the data's owner is UNKNOWN. Unknown is never "nobody": it
        # counts as stranded for every gate, whatever namespace filter asked.
        if not ns or namespaces is None or ns in namespaces:
            out.append(f"{ns or '<unbound>'}/{claim.get('name', pv['metadata']['name'])}")
    return sorted(out)


def resolve_direct_tenant(spec: dict) -> tuple[str | None, str]:
    """Which tenant a DIRECT reservation is FOR. Returns (namespace, reason).

    `spec.tenant` names it explicitly. When absent we may INFER it only while
    exactly one customer-class tenant exists — which was the whole world until
    the tenant register arrived. With two customers, "reserved for a Direct
    customer" names nobody, and guessing would drain the wrong customer's pods
    off a node (or leave them on hardware someone else is paying for). So the
    ambiguous case refuses and says so, in keeping with this controller's
    "stuck but safe beats confidently wrong" rule.
    """
    want = (spec.get("tenant") or "").strip()
    if want:
        if want not in CUSTOMER_TENANTS:
            return None, (f"spec.tenant={want!r} is not a registered CUSTOMER "
                          f"tenant (customers: {', '.join(CUSTOMER_TENANTS)}); "
                          f"a DIRECT reservation is for a paying customer")
        return want, "named by spec.tenant"
    if len(CUSTOMER_TENANTS) == 1:
        return CUSTOMER_TENANTS[0], "inferred: the only customer-class tenant"
    return None, (
        "spec.tenant is unset and there are "
        f"{len(CUSTOMER_TENANTS)} customer tenants "
        f"({', '.join(CUSTOMER_TENANTS)}), so this reservation names nobody. "
        "Set spec.tenant to the namespace this node is reserved for.")


def evict_pod(pod: dict) -> tuple[bool, str]:
    """Request eviction. NEVER force-delete: a PDB rejection is a legitimate
    stop signal, not an obstacle to route around (plan §8.5, test OWN-02)."""
    ns = pod["metadata"]["namespace"]
    name = pod["metadata"]["name"]
    try:
        api("POST", f"/api/v1/namespaces/{ns}/pods/{name}/eviction",
            body={"apiVersion": "policy/v1", "kind": "Eviction",
                  "metadata": {"name": name, "namespace": ns}})
        return True, "evicted"
    except urllib.error.HTTPError as exc:
        # 429 == disruption budget would be violated
        return False, f"{exc.code}:{exc.read().decode()[:200]}"


def drain_stop_reason(live: list, blocked: list) -> str:
    """Why the drain deadline was hit, for the quarantine message.

    The deadline is on the OUTCOME — tenant pods still on the node — never on
    the reason. Until 2026-08-31 all three drain sites gated it on `blocked`
    being non-empty, i.e. on an eviction having been REFUSED. An eviction the
    API server ACCEPTS but which never completes then held the transition in
    DRAINING forever: the node stayed cordoned and tainted out of the sellable
    fleet, with no event, no quarantine and no alert. Reproduced by driving a
    DRAINING reconcile at 833x DRAIN_TIMEOUT with evict_pod returning
    (True, "evicted") and the pod still Running: five passes, five identical
    "1 tenant pod(s) remain" statuses, no escalation.

    Real causes, none of them exotic: terminationGracePeriodSeconds long
    enough to outlast the deadline (now capped in admission), a container
    that ignores SIGTERM, a pod finalizer, a stuck CSI unmount, a wedged
    container runtime on the node.
    """
    if blocked:
        return "eviction blocked past timeout: " + "; ".join(blocked)[:600]
    names = ", ".join(f"{p['metadata']['namespace']}/{p['metadata']['name']}"
                      for p in live)
    return (f"eviction was ACCEPTED but {len(live)} pod(s) never terminated "
            f"within {DRAIN_TIMEOUT}s: {names[:480]} — check "
            "terminationGracePeriodSeconds, pod finalizers and stuck volume "
            "unmounts on this node. NOT force-deleted; see "
            "runbooks/incident-quarantine-recovery.md")


def emit_event(cr_name: str, reason: str, message: str, etype="Normal"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        # NodeOwnership is CLUSTER-SCOPED, so its involvedObject has no
        # namespace, and the API server requires event.namespace to match
        # involvedObject.namespace — for a cluster-scoped object that means the
        # Event must be created in "default". Posting it into platform-system
        # got HTTP 422 ("involvedObject.namespace: Invalid value: '' does not
        # match event.namespace") on EVERY call, from the day this function
        # was written: zero NodeOwnership events ever existed on the cluster,
        # the quarantine runbook's first command returned nothing, and the
        # evidence helper captured nothing. Nobody read the WARN that said so,
        # and the 2026-09-07 rewrite then dropped the WARN (found 2026-09-08).
        api("POST", "/api/v1/namespaces/default/events", body={
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"generateName": f"{cr_name}-"},
            "involvedObject": {"apiVersion": f"{GROUP}/{VERSION}",
                               "kind": "NodeOwnership", "name": cr_name},
            "reason": reason, "message": message[:900], "type": etype,
            "source": {"component": "capacity-controller"},
            "firstTimestamp": ts, "lastTimestamp": ts,
        })
    except Exception as exc:                              # noqa: BLE001
        # Best-effort, but never SILENT. The 2026-09-07 rewrite made this
        # `except Exception: pass`, and on the live cluster every controller
        # event since the restart was being dropped with nothing in the log
        # and nothing in the metrics — the operator-facing audit trail
        # (DrainStarted, ReclaimComplete, Quarantined...) simply stopped, and
        # the matrix could not tell because it asserts status.conditions.
        # An event that cannot be written is logged and counted, so
        # ControllerReconcileErrors-style dashboards can see the trail is dead.
        log("WARN", "event not recorded", cr=cr_name, reason=reason,
            error_class=type(exc).__name__, detail=str(exc)[:200])
        with _metrics_lock:
            _metrics["policy_denials_total"]["EventWriteFailed"] = \
                _metrics["policy_denials_total"].get("EventWriteFailed", 0) + 1


# ------------------------- durable transition state (OWN-07) --------------
def load_state() -> dict:
    try:
        cm = api("GET", f"/api/v1/namespaces/{STATE_NS}/configmaps/{STATE_CM}")
        return json.loads(cm.get("data", {}).get("state", "{}"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def save_state(state: dict) -> None:
    body = {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": STATE_CM, "namespace": STATE_NS},
            "data": {"state": json.dumps(state)}}
    try:
        api("PUT", f"/api/v1/namespaces/{STATE_NS}/configmaps/{STATE_CM}",
            body=body)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            api("POST", f"/api/v1/namespaces/{STATE_NS}/configmaps", body=body)
        else:
            raise


# =========================================================== reconcile ====
def condition(ctype: str, status: str, reason: str, message: str) -> dict:
    return {"type": ctype, "status": status, "reason": reason,
            "message": message[:1000],
            "lastTransitionTime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime())}


def quarantine(name: str, node_id: str, adapter: "VastAdapter", reason: str,
               message: str, node: dict | None = None):
    # adapter is REQUIRED and positional: it used to be read as a non-existent
    # module global, so the unlist below silently raised NameError (caught and
    # logged as WARN) and a quarantined node stayed sellable on VAST. Making it
    # a required parameter means a missing wire is a hard TypeError at the call
    # site, not a dead safety mechanism. All callers are inside reconcile(),
    # which already has the adapter.
    log("ERROR", "quarantine", node=node_id, reason=reason, detail=message)
    emit_event(name, reason, message, etype="Warning")
    # Quarantine ENDS the transition. Drop the age series here rather than
    # waiting for the next reconcile to notice the settled phase, so a
    # quarantined node pages once (NodeQuarantined) instead of twice.
    with _metrics_lock:
        _metrics["transition"].pop(node_id, None)
        _metrics["phase"][node_id] = "QUARANTINED"
    if node:
        try:
            # A quarantined node must stop accepting NEW VAST contracts, or it is
            # "stuck" in k8s while VAST keeps selling it (dual schedulability).
            # Best-effort: unlisting is idempotent and must not block isolation.
            try:
                adapter.unlist_machine(node_id)
            except Exception as exc:                      # noqa: BLE001
                log("WARN", "quarantine unlist failed (continuing to isolate)",
                    node=node_id, error_class=type(exc).__name__)
            update_taints(node["metadata"]["name"],
                          add=[taint(TRANSITION_TAINT, "quarantined")])
            cordon(node["metadata"]["name"], True)
            set_owner_label(node["metadata"]["name"], "QUARANTINED")
        except Exception as exc:                          # noqa: BLE001
            log("ERROR", "quarantine enforcement failed",
                node=node_id, detail=str(exc)[:300])
    patch_status(name, {
        "phase": "QUARANTINED",
        "observedOwner": "QUARANTINED",
        # Stamped so a quarantine issued under the none adapter is not later
        # mistaken for pre-none inherited state (which the gate must hold).
        "marketplaceAdapter": VAST_ADAPTER,
        "conditions": [condition("Quarantined", "True", reason, message)],
    })
    with _metrics_lock:
        _metrics["policy_denials_total"][reason] = \
            _metrics["policy_denials_total"].get(reason, 0) + 1



def refresh_owner_metrics(managed_ids: set | None = None) -> None:
    """Emit an owner series for EVERY managed node, not just those with a CR.

    Without this, arise_node_owner only exists for nodes that happen to have a
    NodeOwnership object, and the P0 OwnerConflict alert
    (`sum by (node) (arise_node_owner) != 1`) is structurally unable to fire
    for the rest: a missing series is not a series equal to zero. The alert
    that exists to catch dual ownership would sit silent on three of four
    nodes. A node whose owner label is absent reports UNKNOWN, which renders
    as all-zeros and therefore trips the alert — which is the correct outcome.

    The mirror of that rule, and the reason `managed_ids` exists: a STALE
    series is not a CURRENT series either. This loop only ever WROTE entries
    for nodes it could see, and never removed or invalidated one — so a node
    whose arise.ai/node-id label was stripped (a re-image, a careless
    kubectl) kept reporting its last owner forever. Measured on the lab
    cluster 2026-08-31: dgx03 was QUARANTINED with reason NodeNotFound while
    arise_node_owner{node="dgx03",owner="ARISE"} still read 1 — sum exactly
    1, so OwnerConflict stayed silent, and every dashboard showed a healthy
    fleet for a machine that had left it.

    `managed_ids` is the set of node-ids that have a NodeOwnership object. An
    id we manage but cannot see reports UNKNOWN (all zeros -> OwnerConflict
    fires, correctly: a node we own is not there). An id with neither a Node
    nor a CR is genuinely unmanaged and its series is dropped.
    """
    try:
        nodes = api("GET",
                    f"/api/v1/nodes?labelSelector={NODE_ID_LABEL}"
                    ).get("items", [])
    except Exception as exc:                                  # noqa: BLE001
        # Deliberately leave the existing series alone: we could not observe,
        # which is not the same as observing an absence.
        log("WARN", "could not refresh owner metrics",
            error_class=type(exc).__name__)
        return
    seen = set()
    with _metrics_lock:
        for n in nodes:
            labels = n["metadata"].get("labels", {})
            nid = labels.get(NODE_ID_LABEL)
            if nid:
                seen.add(nid)
                _metrics["owner"][nid] = labels.get(OWNER_LABEL, "UNKNOWN")
        managed = managed_ids or set()
        # Union, not just the existing keys: after a controller restart the
        # dict starts EMPTY, so iterating it alone would leave a managed node
        # we cannot see with no series at all — the very "a missing series is
        # not a zero series" hole this function exists to close, reintroduced
        # from the other side. Measured on the lab cluster 2026-08-31.
        for nid in (managed | set(_metrics["owner"])) - seen:
            if nid in managed:
                _metrics["owner"][nid] = "UNKNOWN"    # all zeros -> P0
            else:
                _metrics["owner"].pop(nid, None)


def _reopen_ready(name: str, node_id: str, node_name: str, node: dict) -> None:
    """Steady-state for the pool: an ARISE node reporting READY must be OPEN —
    schedulable, no arise.ai/* taint. Aborting a drain into ARISE used to
    leave the node cordoned + tainted while reporting READY (a pool node
    nobody could use)."""
    spec_now = node.get("spec", {}) or {}
    have = {tt.get("key") for tt in (spec_now.get("taints") or [])}
    stale = [k for k in (VAST_TAINT, DIRECT_TAINT, TRANSITION_TAINT, MAINT_TAINT) if k in have]
    if stale or spec_now.get("unschedulable"):
        log("WARN", "READY node not open; correcting", node=node_id,
            stale_taints=stale, cordoned=bool(spec_now.get("unschedulable")))
        # Visible in `kubectl describe`: an operator who cordoned by hand must
        # learn that MAINTENANCE is the sanctioned path, not discover the node
        # quietly reopened.
        emit_event(name, "ReadyNodeReopened",
                   f"READY node was cordoned/tainted ({stale}); reopened — use "
                   "desiredOwner=MAINTENANCE to take a node out of the pool",
                   etype="Warning")
        if stale:
            update_taints(node_name, remove=stale)
        cordon(node_name, False)


def reconcile(cr: dict, adapter: VastAdapter, state: dict) -> None:
    name = cr["metadata"]["name"]
    spec = cr.get("spec", {})
    status = cr.get("status", {}) or {}
    desired = spec["desiredOwner"]
    transition_id = spec["transitionId"]
    node_id = name

    not_before = spec.get("notBefore")
    # A future notBefore delays only the START of a transition — "not started"
    # means no phase yet, or a transitionId the status has never adopted (a
    # node resting in READY/DIRECT_ASSIGNED from its LAST transition is
    # exactly the node a scheduled maintenance is issued against). The wait is
    # NOT a drift holiday: the steady-state enforcement below (owner-label
    # drift map, READY re-open) still runs every cycle; only the supersede to
    # PENDING and the transition's first step are held (reviews 2026-08-27).
    gated = bool(not_before and time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime()) < not_before
                 and ((status.get("phase") or "") in ("", "PENDING")
                      or status.get("lastTransitionId") != transition_id))

    node = get_node_by_logical(node_id)
    if not node:
        quarantine(name, node_id, adapter, "NodeNotFound",
                   f"no Node carries {NODE_ID_LABEL}={node_id}")
        return
    node_name = node["metadata"]["name"]
    labels = node["metadata"].get("labels", {})
    observed_owner = labels.get(OWNER_LABEL, "UNKNOWN")

    # ---- operator emergency quarantine: BEFORE any marketplace call ---------
    # quarantine() reads no marketplace fact and tolerates a failed unlist;
    # it is the operator's only in-band tool exactly when the marketplace is
    # unreachable, so it must not wait on adapter.get() below.
    if desired == "QUARANTINED":
        quarantine(name, node_id, adapter, "OperatorRequested",
                   f"quarantine requested by {spec.get('approvedBy','-')}",
                   node)
        return

    # ---- no-marketplace mode gates (VAST_ADAPTER=none, dgx day-0) -------
    # These run BEFORE the contract-fact refresh below, because in this mode
    # the adapter's "zero contracts" answer is only truthful for nodes that
    # provably never touched a marketplace. An explicit operator quarantine
    # request BYPASSES the gates: quarantine reads no marketplace fact and is
    # strictly isolation-increasing, and it is the operator's only in-band
    # tool for exactly the nodes these gates hold.
    if isinstance(adapter, NoMarketplaceAdapter) and desired != "QUARANTINED":
        prev_phase = status.get("phase") or ""
        hold_reason = None
        # Recorded tells that marketplace state exists: the owner label, a
        # VAST phase, a last-known listed=True (covers a QUARANTINED node
        # whose best-effort unlist failed while listed), or a recorded
        # operationId (a list call once succeeded).
        if (observed_owner == "VAST"
                or prev_phase in ("VAST_READY", "VAST_RENTED")
                or bool(status.get("listed"))
                or status.get("operationId")):
            hold_reason = ("VastStateWithoutMarketplace",
                           "node carries VAST state but VAST_ADAPTER=none; "
                           "refusing to assume zero contracts. Restore the "
                           "marketplace adapter or resolve the node's VAST "
                           "state out-of-band first.")
        # UNRECORDED tells: a crash inside the list window leaves a machine
        # listed on the marketplace with nothing in status (the readback logic
        # exists because that window is real). Any in-flight phase whose
        # status was NOT stamped by this adapter mode predates the none
        # regime and may carry exactly that unrecorded state — hold it.
        # Statuses written under none are stamped (base_status below), so
        # legitimate day-0 transitions never trip this.
        elif (prev_phase in ("DRAINING", "SANITIZING", "HEALTH_CHECK",
                             "QUARANTINED")
              and status.get("marketplaceAdapter") != "none"):
            hold_reason = ("InheritedTransitionState",
                           "in-flight phase " + prev_phase + " was written "
                           "before VAST_ADAPTER=none and may carry unrecorded "
                           "marketplace effects (e.g. a list that committed "
                           "in the crash window). Resolve out-of-band, then "
                           "quarantine-and-release or restore the adapter.")
        if hold_reason:
            reason, msg = hold_reason
            log("ERROR", "marketplace-state node under none adapter; holding",
                node=node_id, observed_owner=observed_owner,
                phase=prev_phase, reason=reason)
            # Holding is not passive: re-assert isolation every cycle so the
            # held position cannot erode under label/taint tampering (OWN-06
            # still applies to held nodes — these actions read no marketplace
            # fact and are strictly isolation-increasing).
            try:
                spec_now = node.get("spec", {}) or {}
                have = {t.get("key") for t in (spec_now.get("taints") or [])}
                if ((observed_owner == "VAST"
                     or prev_phase in ("VAST_READY", "VAST_RENTED"))
                        and VAST_TAINT not in have):
                    update_taints(node_name, add=[taint(VAST_TAINT, "true")])
                if not spec_now.get("unschedulable"):
                    cordon(node_name, True)
            except Exception as exc:                      # noqa: BLE001
                log("WARN", "hold isolation re-assert failed",
                    node=node_id, error_class=type(exc).__name__)
            patch_status(name, {"conditions": [condition(
                "ContractStateKnown", "False", reason, msg)]})
            return
        if desired == "VAST":
            # Fail fast, before cordon/drain/sanitize would run for a listing
            # that can never happen. Stuck but safe, with an explicit reason.
            patch_status(name, {"conditions": [condition(
                "MarketplaceConfigured", "False", "NoMarketplaceAdapter",
                "VAST_ADAPTER=none: handover to a marketplace is impossible "
                "on this cluster until the production adapter ships "
                "(VST-06)")]})
            return

    # ---- always refresh the contract fact FIRST -------------------------
    # Every gate below depends on it, and a stale value is the one error that
    # can strand a paying customer.
    try:
        _, machine = adapter.get(node_id)
        active = int(machine.get("activeContracts", 0))
        listed = bool(machine.get("listed", False))
        rental_end = machine.get("rentalEndAt")
    except Exception as exc:                              # noqa: BLE001
        # UNKNOWN contract state is never interpreted as zero (test E2E-03).
        log("WARN", "contract state unavailable; holding position",
            node=node_id, error_class=type(exc).__name__)
        patch_status(name, {"conditions": [condition(
            "ContractStateKnown", "False", "AdapterUnavailable",
            f"{type(exc).__name__}: {str(exc)[:200]}")]})
        return

    with _metrics_lock:
        _metrics["contracts"][node_id] = active
        _metrics["owner"][node_id] = observed_owner

    if active > 0 and observed_owner != "VAST" and not (
            desired == "VAST" and listed and (status.get("phase") or "") in
            ("DRAINING", "VAST_READY", "VAST_RENTED")):
        # The marketplace says someone is RENTING this machine while our
        # label says it is not theirs: dual ownership, the P0 this controller
        # exists to prevent. Isolate now. (The earlier code patched
        # observedOwner=ARISE with a contract, which the CRD's CEL rightly
        # rejects — a 422 loop every cycle and no cordon.) The one legitimate
        # shape is excluded: a node we LISTED ourselves (still DRAINING: the
        # label flips to VAST only after list + readback) whose first contract
        # has just arrived — the VAST branch below relabels it this cycle.
        quarantine(name, node_id, adapter, "ContractOnNonVastNode",
                   f"{active} active marketplace contract(s) on a node labelled "
                   f"{observed_owner}", node)
        return

    phase = status.get("phase") or "PENDING"

    # A new transitionId supersedes whatever phase the PREVIOUS transition
    # ended in. Without this a node resting in READY after a completed reclaim
    # can never begin a subsequent handover: READY matches none of the VAST
    # path's guards, so reconcile falls through and does nothing, silently and
    # forever. Observed on dgx03, 2026-08-11.
    last_tid = status.get("lastTransitionId")
    if last_tid and last_tid != transition_id and not gated and phase not in (
            "DRAINING", "SANITIZING", "HEALTH_CHECK"):
        log("INFO", "new transition supersedes previous phase",
            node=node_id, previous_transition=last_tid,
            previous_phase=phase, transition_id=transition_id)
        phase = "PENDING"

    key = f"{node_id}:{transition_id}"
    tstate = state.setdefault(key, {"startedAt": time.time()})

    # Publish the transition clock the deadline itself reads, so a hang is
    # observable from outside the controller — including a hang in a phase no
    # deadline covers, or one caused by the deadline logic breaking.
    with _metrics_lock:
        _metrics["phase"][node_id] = phase
        if phase in TRANSITIONAL_PHASES:
            _metrics["transition"][node_id] = (tstate["startedAt"], phase)
        else:
            _metrics["transition"].pop(node_id, None)

    base_status = {
        "observedOwner": observed_owner,
        "activeContracts": active,
        "listed": listed,
        "rentalEndAt": rental_end,
        "lastTransitionId": transition_id,
        "observedGeneration": cr["metadata"].get("generation"),
        # Stamp which adapter regime wrote this status. The no-marketplace
        # gate uses it to tell day-0 in-flight state (trustworthy: no
        # marketplace effect was possible) from state inherited across an
        # adapter downgrade (held, never trusted).
        "marketplaceAdapter": VAST_ADAPTER,
    }

    # ---- drift correction: a human edited the owner label (OWN-06) -------
    if phase in ("READY", "VAST_RENTED", "DIRECT_ASSIGNED", "MAINTENANCE",
                 "QUARANTINED"):
        expected = {"READY": "ARISE", "VAST_RENTED": "VAST",
                    "DIRECT_ASSIGNED": "DIRECT",
                    "MAINTENANCE": "MAINTENANCE",
                    # A hand-edited label must not launder a quarantine into
                    # READY; release is a new transitionId, never a label.
                    "QUARANTINED": "QUARANTINED"}[phase]
        if observed_owner != expected:
            log("WARN", "owner label drift detected; correcting",
                node=node_id, found=observed_owner, expected=expected)
            emit_event(name, "OwnerDriftCorrected",
                       f"label was {observed_owner}, restored to {expected}",
                       etype="Warning")
            set_owner_label(node_name, expected)
            with _metrics_lock:
                _metrics["policy_denials_total"]["OwnerDrift"] = \
                    _metrics["policy_denials_total"].get("OwnerDrift", 0) + 1
            return

    if gated:
        # Held before its notBefore: keep the node's CURRENT steady state
        # honest (a READY pool node stays open) and do nothing else.
        if phase == "READY" and observed_owner == "ARISE":
            _reopen_ready(name, node_id, node_name, node)
        return

    handler = OWNER_HANDLERS.get(desired)
    if handler is not None:
        handler(TransitionContext(
            name=name, node_name=node_name, node=node, labels=labels,
            observed_owner=observed_owner, spec=spec, phase=phase, active=active,
            listed=listed, rental_end=rental_end, transition_id=transition_id,
            base_status=base_status, tstate=tstate, key=key, state=state, adapter=adapter))


@dataclass
class TransitionContext:
    """One cycle's observed facts plus the durable transition progress.

    Shared safety gates run before dispatch. Each owner handler owns its
    phase transitions; only state/tstate are durable across cycles.
    """
    name: str
    node_name: str
    node: dict
    labels: dict
    observed_owner: str
    spec: dict
    phase: str
    active: int
    listed: bool
    rental_end: object
    transition_id: str
    base_status: dict
    tstate: dict
    key: str
    state: dict
    adapter: object


def _start_drain(tx: TransitionContext, message: str) -> None:
    """Stop admission before eviction and start the deadline once."""
    cordon(tx.node_name, True)
    update_taints(tx.node_name, add=[taint(TRANSITION_TAINT, "draining")])
    tx.tstate["startedAt"] = time.time()
    patch_status(tx.name, {**tx.base_status, "phase": "DRAINING"})
    emit_event(tx.name, "DrainStarted", message)


def _drain_pending(tx: TransitionContext, live: list, audience="tenant") -> bool:
    """True while residents remain, including accepted but incomplete evictions.

    A PDB rejection and a workload that never exits both hit the same deadline.
    Neither permits force deletion or proceeding to ownership transfer.
    """
    if not live:
        return False
    elapsed = time.time() - tx.tstate["startedAt"]
    blocked = []
    for pod in live:
        ok, detail = evict_pod(pod)
        if not ok:
            blocked.append(f"{pod['metadata']['namespace']}/{pod['metadata']['name']}: {detail}")
    if elapsed > DRAIN_TIMEOUT:
        quarantine(tx.name, tx.name, tx.adapter, "DrainBlocked",
                   drain_stop_reason(live, blocked), tx.node)
    else:
        patch_status(tx.name, {**tx.base_status, "phase": "DRAINING",
            "conditions": [condition("Draining", "True", "PodsRemaining",
                                      f"{len(live)} {audience} pod(s) remain")]})
    return True


def _reconcile_arise(tx: TransitionContext) -> None:
    # Already ARISE with nothing outstanding: nothing to do. Keyed on the
    # observed facts rather than on `phase`, so that resetting phase for a
    # new transitionId (above) cannot re-trigger sanitization on a node
    # that never left ARISE.
    if tx.observed_owner == "ARISE" and tx.active == 0 and not tx.listed:
        if tx.phase == "READY":
            _reopen_ready(tx.name, tx.name, tx.node_name, tx.node)
        patch_status(tx.name, {**tx.base_status, "phase": "READY"})
        return

    # 1. stop accepting new contracts
    if tx.listed:
        try:
            tx.adapter.unlist_machine(tx.name)
            emit_event(tx.name, "Unlisted", "unlist requested; active "
                                         "contracts continue to run")
        except Exception as exc:                      # noqa: BLE001
            log("ERROR", "unlist failed", node=tx.name,
                error_class=type(exc).__name__)
            patch_status(tx.name, {**tx.base_status, "phase": "VAST_RENTED"})
            return

    # 2. THE CONTRACT GATE. Nothing below this line may run while a
    #    rental is live — no cleanup, no uncordon, no profile switch.
    if tx.active > 0:
        log("INFO", "reclaim blocked by active contracts",
            node=tx.name, active_contracts=tx.active, rental_end=tx.rental_end)
        emit_event(tx.name, "ContractReclaimBlocked",
                   f"{tx.active} active contract(s); earliest reclaim after "
                   f"{tx.rental_end}", etype="Warning")
        patch_status(tx.name, {
            **tx.base_status, "phase": "VAST_RENTED",
            "conditions": [condition(
                "ReclaimBlocked", "True", "ActiveContracts",
                f"{tx.active} active; latest end {tx.rental_end}")]})
        return

    # 3. contracts are zero -> sanitize
    if tx.phase != "HEALTH_CHECK":
        # FENCE FIRST. Every other transition calls _start_drain() — cordon,
        # then evict — before its gate. This path never did: it was written
        # for VAST -> ARISE, where the node arrives already cordoned, and
        # DIRECT -> ARISE inherited it. A departing DIRECT customer's node is
        # therefore still SCHEDULABLE for that customer while it is being
        # handed back to the pool, and admission still lets their pods land
        # (owner label DIRECT, direct-owned toleration allowed). The gate
        # below ran once, recorded HEALTH_CHECK, and the flip happened one
        # full reconcile later with no re-check — a ReplicaSet recreating a
        # pod in that >=10 s gap ended with two tenants on one node, every
        # alert reading clean (reproduced 2026-09-08, audit finding
        # direct-reclaim-sanitization-toctou). Cordon closes the window; the
        # re-check at step 4 closes it even if someone uncordons by hand.
        if tx.observed_owner != "ARISE" and not (tx.node.get("spec") or {}).get("unschedulable"):
            cordon(tx.node_name, True)
            emit_event(tx.name, "ReclaimFenced",
                       "cordoned before sanitization: no new pods may land "
                       "on a node that is leaving its tenant")
        # Volume gate (2026-08-26, scope fixed 2026-08-27): a node
        # re-entering the ARISE pool must carry no CUSTOMER-tenant volumes.
        # A departing DIRECT customer's retained (arise-longterm) data
        # surviving onto pool hardware would leak to the next resident.
        # INTERNAL (tenant-arise) volumes deliberately do NOT block: the
        # pool serves that tenant, its volumes are legitimate residents,
        # and blocking on them made every MAINTENANCE round-trip of a pool
        # node a one-way door. Deleting customer volumes stays a human
        # decision — report and hold.
        stranded = tenant_pvs_on_node(tx.node_name, CUSTOMER_TENANTS)
        if stranded:
            emit_event(tx.name, "ReclaimBlocked",
                       f"{len(stranded)} tenant volume(s) still on node: "
                       + ", ".join(stranded)[:400], etype="Warning")
            patch_status(tx.name, {**tx.base_status, "conditions": [condition(
                "ReclaimBlocked", "True", "StrandedVolumes",
                "tenant volumes remain on this node's local storage: "
                + ", ".join(stranded)[:600]
                + ". Delete (or migrate) them before the node returns "
                "to the pool.")]})
            return
        patch_status(tx.name, {**tx.base_status, "phase": "SANITIZING"})
        results = run_sanitization(tx.node_name, tx.name)
        failed = [r for r in results if not r["passed"]]
        if failed:
            quarantine(tx.name, tx.name, tx.adapter, "SanitizationFailed",
                       f"failed checks: {[r['check'] for r in failed]}",
                       tx.node)
            return
        patch_status(tx.name, {**tx.base_status, "phase": "HEALTH_CHECK",
                            "sanitizationResults": results})
        return

    # 4. health gate passed -> restore to ARISE
    # The gate is re-run HERE, on this reconcile, before anything is written.
    # HEALTH_CHECK was recorded by the previous cycle; acting on it now
    # would be acting on a check that is at least one interval old. A
    # customer pod (or GPU allocation) that appeared since sends the node
    # back through SANITIZING instead of into the shared pool with a tenant
    # still on it.
    late = live_tenant_pods(tx.node_name)
    held = fake_gpu_allocated(tx.node_name)
    if late or held:
        who = ", ".join(f"{p['metadata']['namespace']}/{p['metadata']['name']}" for p in late)[:300]
        log("WARN", "reclaim re-check failed at the flip; holding",
            node=tx.name, pods=len(late), gpu_allocated=held)
        emit_event(tx.name, "ReclaimRecheckFailed",
                   f"tenant work appeared after sanitization passed: "
                   f"{len(late)} pod(s) [{who}], {held} GPU(s) allocated — "
                   f"not returning the node to the pool", etype="Warning")
        patch_status(tx.name, {**tx.base_status, "phase": "SANITIZING",
                            "conditions": [condition(
                                "Sanitizing", "True", "RecheckFailed",
                                f"{len(late)} tenant pod(s) / {held} GPU(s) "
                                f"appeared after the gate passed")]})
        return
    # ORDER MATTERS. The label goes first: set_owner_label("ARISE") is one
    # merge patch that both flips the owner and clears arise.ai/tenant, so
    # from that instant no pod selecting owner=DIRECT / tenant=<customer> can
    # match this node. Taints next. The uncordon LAST, because it is the one
    # write that lets the scheduler bind. The previous order (taints,
    # uncordon, THEN label) left a window in which the node was schedulable
    # while still labelled DIRECT for the departing tenant — and a pod that
    # had been waiting Pending on that selector bound itself in exactly that
    # gap on the live cluster (2026-09-08, after the re-check above had
    # already passed). A fence and a re-check are only as good as the order
    # of the writes they protect.
    set_owner_label(tx.node_name, "ARISE")
    update_taints(tx.node_name,
                  remove=[VAST_TAINT, DIRECT_TAINT, TRANSITION_TAINT,
                          MAINT_TAINT])
    cordon(tx.node_name, False)
    patch_status(tx.name, {**tx.base_status, "observedOwner": "ARISE",
                        "phase": "READY",
                        "conditions": [condition(
                            "Ready", "True", "ReclaimComplete",
                            "sanitized, health-checked and uncordoned")]})
    emit_event(tx.name, "ReclaimComplete", "node returned to ARISE")
    log("INFO", "reclaim complete", node=tx.name,
        transition_id=tx.transition_id)
    tx.state.pop(tx.key, None)
    return


def _reconcile_vast(tx: TransitionContext) -> None:
    if tx.phase in ("VAST_READY", "VAST_RENTED"):
        # Steady state is not "do nothing": plan §8.3 / OWN-06 requires
        # that tampering with the owner label OR THE TAINTS be corrected
        # within a reconcile cycle. Enforce the isolation invariant on
        # every pass, not just at the moment of handover — a node whose
        # VAST taint was stripped is schedulable by ARISE workloads while
        # VAST believes it owns the machine, which is the exact dual
        # ownership this controller exists to prevent.
        spec_now = tx.node.get("spec", {}) or {}
        have = {t.get("key") for t in (spec_now.get("taints") or [])}
        drifted = []
        if VAST_TAINT not in have:
            drifted.append(f"missing taint {VAST_TAINT}")
        if not spec_now.get("unschedulable"):
            drifted.append("node not cordoned")
        if drifted:
            log("WARN", "VAST isolation drift; correcting",
                node=tx.name, drift=drifted)
            emit_event(tx.name, "IsolationDriftCorrected",
                       "; ".join(drifted), etype="Warning")
            update_taints(tx.node_name, add=[taint(VAST_TAINT, "true")])
            cordon(tx.node_name, True)
            with _metrics_lock:
                _metrics["policy_denials_total"]["IsolationDrift"] = \
                    _metrics["policy_denials_total"].get(
                        "IsolationDrift", 0) + 1

        new_phase = "VAST_RENTED" if tx.active > 0 else "VAST_READY"
        patch_status(tx.name, {**tx.base_status, "observedOwner": "VAST",
                            "phase": new_phase})
        return

    # 1. cordon + taint BEFORE evicting anything
    if tx.phase in ("PENDING", ""):
        _start_drain(tx, f'cordoned and tainted for {tx.transition_id}')
        return

    if tx.phase == "DRAINING":
        live = live_tenant_pods(tx.node_name)
        if _drain_pending(tx, live):
            return

        # 2. hard precondition before ANY external call
        allocated = fake_gpu_allocated(tx.node_name)
        if allocated != 0:
            quarantine(tx.name, tx.name, tx.adapter, "AllocationNonZero",
                       f"{FAKE_GPU} still allocated: {allocated}", tx.node)
            return

        # Volume gate (2026-08-26): a machine handed to the marketplace
        # must carry no tenant volumes — the renter gets the hardware, and
        # any tenant data still on the NVMe would go with it. Volumes
        # legitimately outlive the drained pods, so this is a hold with a
        # clear condition, not a quarantine.
        stranded = tenant_pvs_on_node(tx.node_name, isolation_namespaces())
        if stranded:
            emit_event(tx.name, "HandoverBlocked",
                       f"{len(stranded)} tenant volume(s) still on node: "
                       + ", ".join(stranded)[:400], etype="Warning")
            patch_status(tx.name, {**tx.base_status, "phase": "DRAINING",
                                "conditions": [condition(
                "HandoverBlocked", "True", "StrandedVolumes",
                "tenant volumes remain on this node's local storage: "
                + ", ".join(stranded)[:600]
                + ". Delete (or migrate) them before listing.")]})
            return

        checks = run_pre_list_checks(tx.node_name, tx.name)
        if any(not c["passed"] for c in checks):
            quarantine(tx.name, tx.name, tx.adapter, "PreListCheckFailed",
                       f"failed: {[c['check'] for c in checks if not c['passed']]}",
                       tx.node)
            return

        # 3. the external side effect, keyed for idempotency
        try:
            code, resp = tx.adapter.list_machine(tx.name, tx.transition_id)
            op = resp.get("operationId")
        except Exception as exc:                      # noqa: BLE001
            # UNKNOWN outcome. Query by the same key; never blind-retry.
            log("WARN", "list outcome uncertain; querying state",
                node=tx.name, transition_id=tx.transition_id,
                error_class=type(exc).__name__)
            try:
                _, machine2 = tx.adapter.get(tx.name)
            except Exception as exc2:                 # noqa: BLE001
                quarantine(tx.name, tx.name, tx.adapter, "ListOutcomeUnknown",
                           "list result unknown and readback unavailable: "
                           f"{type(exc2).__name__}", tx.node)
                return
            if not machine2.get("listed"):
                patch_status(tx.name, {**tx.base_status, "phase": "DRAINING",
                                    "conditions": [condition(
                                        "ListPending", "True",
                                        "RetryAfterReadback",
                                        "list did not take effect; safe "
                                        "to retry next cycle")]})
                return
            op = "recovered-by-readback"
            log("INFO", "list had in fact committed; adopting state",
                node=tx.name, transition_id=tx.transition_id)

        # 4. verify by readback before declaring success
        _, machine3 = tx.adapter.get(tx.name)
        if not machine3.get("listed"):
            quarantine(tx.name, tx.name, tx.adapter, "ListReadbackMismatch",
                       "adapter accepted list but readback says unlisted",
                       tx.node)
            return

        # add the VAST taint and drop the transition taint together;
        # doing it in two patches erases the first (see update_taints)
        update_taints(tx.node_name, add=[taint(VAST_TAINT, "true")],
                      remove=[TRANSITION_TAINT, DIRECT_TAINT, MAINT_TAINT])
        set_owner_label(tx.node_name, "VAST")
        active_now = int(machine3.get("activeContracts", 0))
        patch_status(tx.name, {
            **tx.base_status, "observedOwner": "VAST",
            "activeContracts": active_now,
            "listed": True, "operationId": op,
            "phase": "VAST_RENTED" if active_now > 0 else "VAST_READY",
            "conditions": [condition("Listed", "True", "ListVerified",
                                     f"operationId={op}")]})
        emit_event(tx.name, "Listed", f"machine listed, operationId={op}")
        log("INFO", "handover complete", node=tx.name,
            transition_id=tx.transition_id, operation_id=op)
        with _metrics_lock:
            _metrics["transitions_total"]["ARISE->VAST"] = \
                _metrics["transitions_total"].get("ARISE->VAST", 0) + 1
        return


def _reconcile_direct(tx: TransitionContext) -> None:
    # Stop NEW VAST bookings FIRST — before the contract gate — exactly as the
    # ARISE reclaim path does. A still-listed node is rentable, so leaving it
    # listed while we wait for an existing contract to end lets VAST keep
    # booking, and the reservation never converges; and reserving an idle-but-
    # listed node without unlisting first is outright dual ownership (the P0
    # the OwnerConflict alert can't see, since the label reads one owner).
    # listed is re-read every cycle, so we proceed only once it delists.
    if tx.listed:
        try:
            tx.adapter.unlist_machine(tx.name)
            emit_event(tx.name, "Unlisted",
                       "unlisted from VAST before Direct reservation")
        except Exception as exc:                      # noqa: BLE001
            log("ERROR", "unlist before Direct failed", node=tx.name,
                error_class=type(exc).__name__)
            patch_status(tx.name, {**tx.base_status, "phase": "VAST_RENTED"})
            return
        patch_status(tx.name, {**tx.base_status, "phase": "PENDING"})
        return

    if tx.active > 0:
        # Delisted but a contract is still live: the existing rental must run
        # to its end before we can hand the node over (no double-selling).
        emit_event(tx.name, "DirectBlocked",
                   f"{tx.active} active VAST contract(s); cannot reserve for "
                   "a Direct customer until they end", etype="Warning")
        patch_status(tx.name, {**tx.base_status, "phase": "VAST_RENTED"})
        return

    # A reservation that was BLOCKED on a live contract sat in
    # VAST_RENTED; once the contract ends (unlisted, zero active) that
    # phase is just history — start the drain. Without this the node
    # never converged and the DirectBlocked condition outlived the
    # contract it described.
    if tx.phase in ("VAST_RENTED", "VAST_READY"):
        tx.phase = "PENDING"

    # 0. Resolve WHO the node is for BEFORE touching it: a typo in
    #    spec.tenant must not cost the pool a cordoned node.
    reserved_for, why = resolve_direct_tenant(tx.spec)
    if not reserved_for:
        log("ERROR", "ambiguous DIRECT reservation; holding", node=tx.name,
            detail=why)
        emit_event(tx.name, "DirectTenantAmbiguous", why, etype="Warning")
        patch_status(tx.name, {**tx.base_status, "conditions": [condition(
            "TenantResolved", "False", "AmbiguousReservation", why)]})
        return

    if tx.phase == "DIRECT_ASSIGNED":
        if tx.labels.get(TENANT_LABEL) != reserved_for:
            # Repair under cordon, before reopening admission. On upgrade
            # existing single-customer reservations gain their binding.
            cordon(tx.node_name, True)
            set_direct_binding(tx.node_name, reserved_for)
            cordon(tx.node_name, False)
        # Steady state still enforces the invariant every cycle (OWN-06).
        spec_now = tx.node.get("spec", {}) or {}
        have = {t.get("key") for t in (spec_now.get("taints") or [])}
        drift = []
        if DIRECT_TAINT not in have:
            drift.append(f"missing taint {DIRECT_TAINT}")
        if spec_now.get("unschedulable"):
            # A cordoned DIRECT node silently denies the customer the
            # capacity they are paying for, which is as much a breach as
            # letting someone else onto it.
            drift.append("node cordoned; customer cannot schedule")
        if drift:
            log("WARN", "DIRECT isolation drift; correcting",
                node=tx.name, drift=drift)
            emit_event(tx.name, "IsolationDriftCorrected",
                       "; ".join(drift), etype="Warning")
            update_taints(tx.node_name, add=[taint(DIRECT_TAINT, "true")],
                          remove=[TRANSITION_TAINT, VAST_TAINT])
            cordon(tx.node_name, False)
        patch_status(tx.name, {**tx.base_status, "observedOwner": "DIRECT",
                            "phase": "DIRECT_ASSIGNED"})
        return

    # 1. cordon + taint before evicting, exactly as for a VAST handover.
    if tx.phase in ("PENDING", ""):
        _start_drain(tx, f'reserving for Direct customer, transition {tx.transition_id}')
        return

    if tx.phase == "DRAINING":
        # Drain EVERY tenant except the one this node is reserved FOR —
        # that tenant's workloads are the intended residents, everyone
        # else's are another customer's work sitting on hardware this
        # customer is paying for. `reserved_for` is spec.tenant; with a
        # single customer-class tenant it is inferred, and with several it
        # is required (resolve_direct_tenant refuses to guess).
        others = tuple(n for n in isolation_namespaces() if n != reserved_for)
        live = [p for p in pods_on_node(tx.node_name, others)
                if p.get("status", {}).get("phase") not in
                ("Succeeded", "Failed")]
        if _drain_pending(tx, live, "other-tenant"):
            return

        allocated = fake_gpu_allocated(tx.node_name)
        if allocated != 0:
            quarantine(tx.name, tx.name, tx.adapter, "AllocationNonZero",
                       f"{FAKE_GPU} still allocated: {allocated}", tx.node)
            return

        # Volume gate (2026-08-26): the customer pays for the WHOLE node,
        # NVMe included. Another tenant's volumes surviving the drain
        # would sit on the customer's disk AND be stranded for their own
        # owner (pinned by nodeAffinity behind a taint only tenant-direct
        # may tolerate, so their pods would Pend forever with no
        # explanation). tenant-direct's own volumes are the intended
        # residents and do not block.
        stranded = tenant_pvs_on_node(tx.node_name, others)
        if stranded:
            emit_event(tx.name, "DirectBlocked",
                       f"{len(stranded)} other-tenant volume(s) still on "
                       "node: " + ", ".join(stranded)[:400],
                       etype="Warning")
            patch_status(tx.name, {**tx.base_status, "phase": "DRAINING",
                                "conditions": [condition(
                "DirectBlocked", "True", "StrandedVolumes",
                "volumes belonging to another tenant remain on this node's "
                "local storage: " + ", ".join(stranded)[:600]
                + ". Delete (or migrate) them before reserving the node "
                "for a Direct customer.")]})
            return

        # 2. Hand over: taint against everyone else, then UNCORDON so the
        #    customer's own workloads can actually be placed. Removing
        #    VAST_TAINT too: a node reclaimed from VAST still carries its
        #    NoSchedule taint, which would deny the Direct customer the node
        #    they reserved (the drain path leaves it behind).
        # Publish both labels while still cordoned/transition-tainted.
        # Uncordoning before labelling created an ARISE scheduling window.
        set_direct_binding(tx.node_name, reserved_for)
        update_taints(tx.node_name, add=[taint(DIRECT_TAINT, "true")],
                      remove=[TRANSITION_TAINT, VAST_TAINT, MAINT_TAINT])
        cordon(tx.node_name, False)
        patch_status(tx.name, {**tx.base_status, "observedOwner": "DIRECT",
                            "phase": "DIRECT_ASSIGNED",
                            "conditions": [condition(
                                "Reserved", "True", "DirectAssigned",
                                "node reserved for a Direct customer")]})
        emit_event(tx.name, "DirectAssigned",
                   "node drained of internal work and reserved")
        log("INFO", "direct reservation complete", node=tx.name,
            transition_id=tx.transition_id)
        with _metrics_lock:
            _metrics["transitions_total"]["ARISE->DIRECT"] = \
                _metrics["transitions_total"].get("ARISE->DIRECT", 0) + 1
        tx.state.pop(tx.key, None)
        return
    return


def _reconcile_maintenance(tx: TransitionContext) -> None:
    # A rented machine finishes its contract first, exactly as reclaim:
    # stop NEW bookings, then wait. Unlist is not reclaim (plan §8.6).
    if tx.listed:
        try:
            tx.adapter.unlist_machine(tx.name)
            emit_event(tx.name, "Unlisted",
                       "unlisted ahead of maintenance; active contracts "
                       "continue to run")
        except Exception as exc:                      # noqa: BLE001
            log("ERROR", "unlist before maintenance failed",
                node=tx.name, error_class=type(exc).__name__)
            patch_status(tx.name, {**tx.base_status, "phase": "VAST_RENTED"})
            return
    if tx.active > 0:
        emit_event(tx.name, "MaintenanceBlocked",
                   f"{tx.active} active contract(s); maintenance waits for "
                   f"their end ({tx.rental_end})", etype="Warning")
        patch_status(tx.name, {**tx.base_status, "phase": "VAST_RENTED",
                            "conditions": [condition(
                                "MaintenanceBlocked", "True",
                                "ActiveContracts",
                                f"{tx.active} active; latest end "
                                f"{tx.rental_end}")]})
        return

    if tx.phase in ("VAST_RENTED", "VAST_READY"):
        tx.phase = "PENDING"          # contract over (see the DIRECT path)

    if tx.phase == "MAINTENANCE":
        # Steady state enforces the invariant every cycle (OWN-06): a
        # maintenance node that lost its cordon or taint is schedulable
        # mid-firmware-flash.
        spec_now = tx.node.get("spec", {}) or {}
        have = {t.get("key") for t in (spec_now.get("taints") or [])}
        drift = []
        if MAINT_TAINT not in have:
            drift.append(f"missing taint {MAINT_TAINT}")
        if not spec_now.get("unschedulable"):
            drift.append("node not cordoned")
        if drift:
            log("WARN", "MAINTENANCE isolation drift; correcting",
                node=tx.name, drift=drift)
            emit_event(tx.name, "IsolationDriftCorrected",
                       "; ".join(drift), etype="Warning")
            update_taints(tx.node_name, add=[taint(MAINT_TAINT, "true")])
            cordon(tx.node_name, True)
        patch_status(tx.name, {**tx.base_status, "observedOwner": "MAINTENANCE",
                            "phase": "MAINTENANCE"})
        return

    # 1. isolate before evicting, exactly as every other transition.
    if tx.phase in ("PENDING", ""):
        _start_drain(tx, f'draining for maintenance, transition {tx.transition_id}')
        return

    if tx.phase == "DRAINING":
        live = live_tenant_pods(tx.node_name)
        if _drain_pending(tx, live):
            return

        # 2. NO volume gate: ownership does not change, the node comes
        #    back, and its volumes are exactly where their owners expect
        #    them. Swap the transition taint for the maintenance one,
        #    KEEP the cordon, mark the steady state.
        update_taints(tx.node_name, add=[taint(MAINT_TAINT, "true")],
                      remove=[TRANSITION_TAINT, VAST_TAINT, DIRECT_TAINT])
        set_owner_label(tx.node_name, "MAINTENANCE")
        patch_status(tx.name, {**tx.base_status,
                            "observedOwner": "MAINTENANCE",
                            "phase": "MAINTENANCE",
                            "conditions": [condition(
                                "UnderMaintenance", "True", "Drained",
                                "drained, cordoned and tainted; exit via "
                                "desiredOwner=ARISE (sanitize + health) "
                                "or DIRECT")]})
        emit_event(tx.name, "MaintenanceStarted",
                   "node drained and isolated for planned maintenance")
        log("INFO", "maintenance engaged", node=tx.name,
            transition_id=tx.transition_id)
        with _metrics_lock:
            _metrics["transitions_total"]["->MAINTENANCE"] =                     _metrics["transitions_total"].get("->MAINTENANCE", 0) + 1
        tx.state.pop(tx.key, None)
        return
    return


OWNER_HANDLERS = {
    "ARISE": _reconcile_arise,
    "VAST": _reconcile_vast,
    "DIRECT": _reconcile_direct,
    "MAINTENANCE": _reconcile_maintenance,
}



# --------------------------- simulated gates ------------------------------
def run_pre_list_checks(node_name: str, node_id: str) -> list[dict]:
    """Phase A uses synthetic probes; production replaces these with the
    hardware runbook (plan §8.5). Labelled SIMULATED in every report."""
    node = get_node_by_logical(node_id) or {}
    conds = {c["type"]: c["status"]
             for c in node.get("status", {}).get("conditions", [])}
    return [
        {"check": "node_ready", "passed": conds.get("Ready") == "True",
         "detail": f"Ready={conds.get('Ready')}", "kind": "SIMULATED"},
        {"check": "no_tenant_pods",
         # Same definition as the drain and the sanitize gate (2026-09-01):
         # a finished pod object is not a running workload.
         "passed": len(live_tenant_pods(node_name)) == 0,
         "detail": "tenant namespaces drained", "kind": "CONTROL-PLANE"},
        {"check": "fake_gpu_free", "passed": fake_gpu_allocated(node_name) == 0,
         "detail": f"{FAKE_GPU} allocation is zero", "kind": "CONTROL-PLANE"},
        {"check": "disk_pressure",
         "passed": conds.get("DiskPressure", "False") == "False",
         "detail": f"DiskPressure={conds.get('DiskPressure')}",
         "kind": "SIMULATED"},
    ]


def run_sanitization(node_name: str, node_id: str) -> list[dict]:
    """Cleanup gate on the way back to ARISE (plan §8.6). In Phase A this
    proves the ORDERING and the gate, not any real data erasure."""
    remaining = live_tenant_pods(node_name)
    done = terminal_tenant_pods(node_name)
    return [
        {"check": "customer_workloads_stopped", "passed": len(remaining) == 0,
         "detail": f"{len(remaining)} tenant pod(s) still running",
         "kind": "CONTROL-PLANE"},
        # Reported, never blocking: a finished job's pod object holds no
        # resources, and quarantining a node because a customer's training run
        # SUCCEEDED broke the happy path (2026-09-01). Visible so an operator
        # reading the evidence knows what was left behind.
        {"check": "terminal_pods_left_behind", "passed": True,
         "detail": (f"{len(done)} finished tenant pod object(s) remain: "
                    + ", ".join(f"{x['metadata']['namespace']}/{x['metadata']['name']}"
                                for x in done)[:300]) if done
                   else "no finished tenant pod objects remain",
         "kind": "CONTROL-PLANE"},
        {"check": "fake_gpu_released",
         "passed": fake_gpu_allocated(node_name) == 0,
         "detail": "no simulated GPU held", "kind": "CONTROL-PLANE"},
        {"check": "data_erasure", "passed": True,
         "detail": "SIMULATED ONLY — real NVMe erase is HW-12, DGX phase",
         "kind": "SIMULATED"},
        {"check": "health_score", "passed": True,
         "detail": "SIMULATED ONLY — real GPU/NVLink/IB health is HW-03..07",
         "kind": "SIMULATED"},
    ]


# ------------------------------- metrics ----------------------------------
class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):                                    # noqa: N802
        if self.path in ("/healthz", "/readyz"):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/metrics":
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
        else:
            body = b'{"error":"not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def render_metrics() -> str:
    with _metrics_lock:
        out = [
            "# HELP arise_node_owner Current owner per node (exactly one =1).",
            "# TYPE arise_node_owner gauge",
        ]
        for node, owner in _metrics["owner"].items():
            # UNKNOWN is deliberately NOT a candidate: a node with no owner
            # label renders as 1x0 for every real owner, so the sum is 0 and
            # OwnerConflict fires. Publishing an UNKNOWN=1 series would make
            # the sum 1 and hide the very condition we need to see.
            for candidate in ("ARISE", "DIRECT", "VAST", "QUARANTINED",
                              "MAINTENANCE"):
                out.append(f'arise_node_owner{{node="{node}",'
                           f'owner="{candidate}"}} '
                           f'{1 if owner == candidate else 0}')
        out += ["# HELP arise_active_contracts Active contracts per node.",
                "# TYPE arise_active_contracts gauge"]
        for node, n in _metrics["contracts"].items():
            out.append(f'arise_active_contracts{{node="{node}",'
                       f'platform="vast-mock"}} {n}')
        out += ["# HELP arise_node_phase NodeOwnership phase per node "
                "(exactly one =1), read from the CR, not from a node label.",
                "# TYPE arise_node_phase gauge"]
        for node, ph in _metrics["phase"].items():
            for cand in ("PENDING", "DRAINING", "SANITIZING", "HEALTH_CHECK",
                         "READY", "VAST_RENTED", "DIRECT_ASSIGNED",
                         "MAINTENANCE", "QUARANTINED"):
                out.append(f'arise_node_phase{{node="{node}",phase="{cand}"}} '
                           f'{1 if ph == cand else 0}')
        out += ["# HELP arise_node_transition_seconds Age of the in-flight "
                "transition, per node. Absent when the node is settled.",
                "# TYPE arise_node_transition_seconds gauge"]
        now = time.time()
        for node, (started, ph) in _metrics["transition"].items():
            out.append(f'arise_node_transition_seconds{{node="{node}",'
                       f'phase="{ph}"}} {max(0.0, now - started):.1f}')
        out += ["# HELP arise_reconcile_errors_total Reconcile errors.",
                "# TYPE arise_reconcile_errors_total counter"]
        for cls, n in _metrics["reconcile_errors_total"].items():
            out.append(f'arise_reconcile_errors_total{{controller='
                       f'"capacity-controller",class="{cls}"}} {n}')
        out += ["# HELP arise_policy_denials_total Policy denials.",
                "# TYPE arise_policy_denials_total counter"]
        for reason, n in _metrics["policy_denials_total"].items():
            out.append(f'arise_policy_denials_total{{policy='
                       f'"capacity-controller",reason="{reason}"}} {n}')
        return "\n".join(out) + "\n"


def main() -> int:
    load_tenant_namespaces()   # drain/volume gates read this
    try:
        # Exactly two adapters exist: the lab mock and the honest "no
        # marketplace yet" mode for delivered hardware. Anything else —
        # including any future production value — still refuses to start
        # until the VST-06 code path actually ships.
        adapter = (NoMarketplaceAdapter() if VAST_ADAPTER == "none"
                   else VastAdapter(VAST_BASE))
    except RuntimeError as exc:
        log("ERROR", "adapter refused to start", detail=str(exc))
        return 1

    log("INFO", "capacity-controller starting", adapter=VAST_ADAPTER,
        vast_base=VAST_BASE, production_adapter_enabled=False,
        reconcile_interval=INTERVAL)
    threading.Thread(
        target=lambda: ThreadingHTTPServer(("0.0.0.0", PORT),
                                           MetricsHandler).serve_forever(),
        daemon=True).start()

    state = load_state()
    while True:
        try:
            maybe_reload_tenants()          # register edits take effect live
            # Metrics first: ownership must be observable even for nodes that
            # currently have no NodeOwnership object.
            crs = list_crs()
            # CRs first: a node-id we manage but cannot see must report
            # UNKNOWN rather than keep its last owner (2026-08-31).
            refresh_owner_metrics({(c.get("spec") or {}).get("nodeId")
                                   or c["metadata"]["name"] for c in crs})
            # Prune per-CR metrics for nodes whose NodeOwnership was deleted.
            # A contracts gauge frozen at its last reconciled value outlives
            # the CR and keeps firing ContractReclaimAttempt forever — a fake
            # P0 observed on dgx04 (2026-08-14) after test-fixture cleanup.
            # Absent series is the honest state for an unmanaged node.
            live = {c["metadata"]["name"] for c in crs}
            with _metrics_lock:
                for gone in [n for n in _metrics["contracts"] if n not in live]:
                    del _metrics["contracts"][gone]
                for gone in [n for n in _metrics["transition"] if n not in live]:
                    del _metrics["transition"][gone]
                for gone in [n for n in _metrics["phase"] if n not in live]:
                    del _metrics["phase"][gone]
            for cr in crs:
                nm = cr["metadata"]["name"]
                try:
                    reconcile(cr, adapter, state)
                except Exception as exc:                  # noqa: BLE001
                    cls = type(exc).__name__
                    with _metrics_lock:
                        _metrics["reconcile_errors_total"][cls] = \
                            _metrics["reconcile_errors_total"].get(cls, 0) + 1
                    log("ERROR", "reconcile failed", node=nm,
                        error_class=cls, detail=str(exc)[:400])
            save_state(state)
        except Exception as exc:                          # noqa: BLE001
            log("ERROR", "control loop error",
                error_class=type(exc).__name__, detail=str(exc)[:400])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
