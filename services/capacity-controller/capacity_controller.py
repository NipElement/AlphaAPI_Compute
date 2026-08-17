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
NODE_ID_LABEL = "arise.ai/node-id"
PAIR_LABEL = "arise.ai/pair"
TRANSITION_TAINT = "arise.ai/transition"
VAST_TAINT = "arise.ai/vast-owned"
# A DIRECT node is reserved for a specific customer. Unlike a VAST node it
# stays SCHEDULABLE — the customer's own work must land on it — so isolation
# comes from a taint plus the admission gate, not from a cordon.
DIRECT_TAINT = "arise.ai/direct-owned"

TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")

_metrics_lock = threading.Lock()
_metrics = {
    "reconcile_errors_total": {},
    "transitions_total": {},
    "owner": {},
    "contracts": {},
    "phase": {},
    "policy_denials_total": {},
}


# =========================================================== plumbing ======
def log(level: str, msg: str, **kw) -> None:
    """JSON logs with the fields plan §9.5 mandates."""
    rec = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "controller": "capacity-controller",
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


# ======================================================== k8s helpers =====
def get_cr(name: str):
    return api("GET", f"/apis/{GROUP}/{VERSION}/{PLURAL}/{name}")


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
    patch_node(node_name, {"metadata": {"labels": {OWNER_LABEL: owner}}})


def set_taints(node_name: str, taints: list[dict]):
    patch_node(node_name, {"spec": {"taints": taints}})


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
    """
    node = api("GET", f"/api/v1/nodes/{node_name}")
    taints = node.get("spec", {}).get("taints") or []
    drop = set(remove or [])
    drop.update(t["key"] for t in (add or []))
    taints = [t for t in taints if t.get("key") not in drop]
    taints.extend(add or [])
    set_taints(node_name, taints)


def taint(key: str, value: str, effect: str = "NoSchedule") -> dict:
    return {"key": key, "value": value, "effect": effect}


def pods_on_node(node_name: str, namespaces=None):
    pods = api("GET",
               f"/api/v1/pods?fieldSelector=spec.nodeName%3D{node_name}"
               ).get("items", [])
    if namespaces is None:
        return pods
    return [p for p in pods if p["metadata"]["namespace"] in namespaces]


def fake_gpu_allocated(node_name: str) -> int:
    total = 0
    for p in pods_on_node(node_name):
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for c in p["spec"].get("containers", []):
            req = (c.get("resources", {}).get("requests") or {})
            total += int(req.get(FAKE_GPU, 0) or 0)
    return total


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


def emit_event(cr_name: str, reason: str, message: str, etype="Normal"):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        api("POST", f"/api/v1/namespaces/{STATE_NS}/events", body={
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"generateName": f"{cr_name}-"},
            "involvedObject": {"apiVersion": f"{GROUP}/{VERSION}",
                               "kind": "NodeOwnership", "name": cr_name},
            "reason": reason, "message": message[:900], "type": etype,
            "source": {"component": "capacity-controller"},
            "firstTimestamp": ts, "lastTimestamp": ts,
        })
    except Exception:                                     # noqa: BLE001
        pass


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
        "conditions": [condition("Quarantined", "True", reason, message)],
    })
    with _metrics_lock:
        _metrics["policy_denials_total"][reason] = \
            _metrics["policy_denials_total"].get(reason, 0) + 1



def refresh_owner_metrics() -> None:
    """Emit an owner series for EVERY managed node, not just those with a CR.

    Without this, arise_node_owner only exists for nodes that happen to have a
    NodeOwnership object, and the P0 OwnerConflict alert
    (`sum by (node) (arise_node_owner) != 1`) is structurally unable to fire
    for the rest: a missing series is not a series equal to zero. The alert
    that exists to catch dual ownership would sit silent on three of four
    nodes. A node whose owner label is absent reports UNKNOWN, which renders
    as all-zeros and therefore trips the alert — which is the correct outcome.
    """
    try:
        nodes = api("GET",
                    f"/api/v1/nodes?labelSelector={NODE_ID_LABEL}"
                    ).get("items", [])
    except Exception as exc:                                  # noqa: BLE001
        log("WARN", "could not refresh owner metrics",
            error_class=type(exc).__name__)
        return
    with _metrics_lock:
        for n in nodes:
            labels = n["metadata"].get("labels", {})
            nid = labels.get(NODE_ID_LABEL)
            if nid:
                _metrics["owner"][nid] = labels.get(OWNER_LABEL, "UNKNOWN")


def reconcile(cr: dict, adapter: VastAdapter, state: dict) -> None:
    name = cr["metadata"]["name"]
    spec = cr.get("spec", {})
    status = cr.get("status", {}) or {}
    desired = spec["desiredOwner"]
    transition_id = spec["transitionId"]
    node_id = name

    not_before = spec.get("notBefore")
    if not_before and time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime()) < not_before:
        return

    node = get_node_by_logical(node_id)
    if not node:
        quarantine(name, node_id, adapter, "NodeNotFound",
                   f"no Node carries {NODE_ID_LABEL}={node_id}")
        return
    node_name = node["metadata"]["name"]
    labels = node["metadata"].get("labels", {})
    observed_owner = labels.get(OWNER_LABEL, "UNKNOWN")

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

    phase = status.get("phase") or "PENDING"

    # A new transitionId supersedes whatever phase the PREVIOUS transition
    # ended in. Without this a node resting in READY after a completed reclaim
    # can never begin a subsequent handover: READY matches none of the VAST
    # path's guards, so reconcile falls through and does nothing, silently and
    # forever. Observed on dgx03, 2026-08-11.
    last_tid = status.get("lastTransitionId")
    if last_tid and last_tid != transition_id and phase not in (
            "DRAINING", "SANITIZING", "HEALTH_CHECK"):
        log("INFO", "new transition supersedes previous phase",
            node=node_id, previous_transition=last_tid,
            previous_phase=phase, transition_id=transition_id)
        phase = "PENDING"

    key = f"{node_id}:{transition_id}"
    tstate = state.setdefault(key, {"startedAt": time.time()})

    base_status = {
        "observedOwner": observed_owner,
        "activeContracts": active,
        "listed": listed,
        "rentalEndAt": rental_end,
        "lastTransitionId": transition_id,
        "observedGeneration": cr["metadata"].get("generation"),
    }

    # ---- drift correction: a human edited the owner label (OWN-06) -------
    if phase in ("READY", "VAST_RENTED", "DIRECT_ASSIGNED"):
        expected = {"READY": "ARISE", "VAST_RENTED": "VAST",
                    "DIRECT_ASSIGNED": "DIRECT"}[phase]
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

    # =================== desired ARISE (reclaim path, §8.6) ==============
    if desired == "ARISE":
        # Already ARISE with nothing outstanding: nothing to do. Keyed on the
        # observed facts rather than on `phase`, so that resetting phase for a
        # new transitionId (above) cannot re-trigger sanitization on a node
        # that never left ARISE.
        if observed_owner == "ARISE" and active == 0 and not listed:
            patch_status(name, {**base_status, "phase": "READY"})
            return

        # 1. stop accepting new contracts
        if listed:
            try:
                adapter.unlist_machine(node_id)
                emit_event(name, "Unlisted", "unlist requested; active "
                                             "contracts continue to run")
            except Exception as exc:                      # noqa: BLE001
                log("ERROR", "unlist failed", node=node_id,
                    error_class=type(exc).__name__)
                patch_status(name, {**base_status, "phase": "VAST_RENTED"})
                return

        # 2. THE CONTRACT GATE. Nothing below this line may run while a
        #    rental is live — no cleanup, no uncordon, no profile switch.
        if active > 0:
            log("INFO", "reclaim blocked by active contracts",
                node=node_id, active_contracts=active, rental_end=rental_end)
            emit_event(name, "ContractReclaimBlocked",
                       f"{active} active contract(s); earliest reclaim after "
                       f"{rental_end}", etype="Warning")
            patch_status(name, {
                **base_status, "phase": "VAST_RENTED",
                "conditions": [condition(
                    "ReclaimBlocked", "True", "ActiveContracts",
                    f"{active} active; latest end {rental_end}")]})
            return

        # 3. contracts are zero -> sanitize
        if phase != "HEALTH_CHECK":
            patch_status(name, {**base_status, "phase": "SANITIZING"})
            results = run_sanitization(node_name, node_id)
            failed = [r for r in results if not r["passed"]]
            if failed:
                quarantine(name, node_id, adapter, "SanitizationFailed",
                           f"failed checks: {[r['check'] for r in failed]}",
                           node)
                return
            patch_status(name, {**base_status, "phase": "HEALTH_CHECK",
                                "sanitizationResults": results})
            return

        # 4. health gate passed -> restore to ARISE
        # single atomic patch: both taints go in one write
        update_taints(node_name,
                      remove=[VAST_TAINT, DIRECT_TAINT, TRANSITION_TAINT])
        cordon(node_name, False)
        set_owner_label(node_name, "ARISE")
        patch_status(name, {**base_status, "observedOwner": "ARISE",
                            "phase": "READY",
                            "conditions": [condition(
                                "Ready", "True", "ReclaimComplete",
                                "sanitized, health-checked and uncordoned")]})
        emit_event(name, "ReclaimComplete", "node returned to ARISE")
        log("INFO", "reclaim complete", node=node_id,
            transition_id=transition_id)
        state.pop(key, None)
        return

    # ==================== desired VAST (handover path, §8.5) =============
    if desired == "VAST":
        if phase in ("VAST_READY", "VAST_RENTED"):
            # Steady state is not "do nothing": plan §8.3 / OWN-06 requires
            # that tampering with the owner label OR THE TAINTS be corrected
            # within a reconcile cycle. Enforce the isolation invariant on
            # every pass, not just at the moment of handover — a node whose
            # VAST taint was stripped is schedulable by ARISE workloads while
            # VAST believes it owns the machine, which is the exact dual
            # ownership this controller exists to prevent.
            spec_now = node.get("spec", {}) or {}
            have = {t.get("key") for t in (spec_now.get("taints") or [])}
            drifted = []
            if VAST_TAINT not in have:
                drifted.append(f"missing taint {VAST_TAINT}")
            if not spec_now.get("unschedulable"):
                drifted.append("node not cordoned")
            if drifted:
                log("WARN", "VAST isolation drift; correcting",
                    node=node_id, drift=drifted)
                emit_event(name, "IsolationDriftCorrected",
                           "; ".join(drifted), etype="Warning")
                update_taints(node_name, add=[taint(VAST_TAINT, "true")])
                cordon(node_name, True)
                with _metrics_lock:
                    _metrics["policy_denials_total"]["IsolationDrift"] = \
                        _metrics["policy_denials_total"].get(
                            "IsolationDrift", 0) + 1

            new_phase = "VAST_RENTED" if active > 0 else "VAST_READY"
            patch_status(name, {**base_status, "observedOwner": "VAST",
                                "phase": new_phase})
            return

        # 1. cordon + taint BEFORE evicting anything
        if phase in ("PENDING", ""):
            cordon(node_name, True)
            update_taints(node_name, add=[taint(TRANSITION_TAINT, "draining")])
            patch_status(name, {**base_status, "phase": "DRAINING"})
            emit_event(name, "DrainStarted",
                       f"cordoned and tainted for {transition_id}")
            return

        if phase == "DRAINING":
            tenant_pods = pods_on_node(node_name, TENANT_NAMESPACES)
            live = [p for p in tenant_pods
                    if p.get("status", {}).get("phase") not in
                    ("Succeeded", "Failed")]
            if live:
                elapsed = time.time() - tstate["startedAt"]
                blocked = []
                for pod in live:
                    ok, detail = evict_pod(pod)
                    if not ok:
                        blocked.append(
                            f"{pod['metadata']['namespace']}/"
                            f"{pod['metadata']['name']}: {detail}")
                if blocked and elapsed > DRAIN_TIMEOUT:
                    # Report the specific blocking objects and STOP. Do not
                    # force-delete (plan §8.5, test OWN-02).
                    quarantine(name, node_id, adapter, "DrainBlocked",
                               "eviction blocked past timeout: " +
                               "; ".join(blocked)[:600], node)
                    return
                patch_status(name, {**base_status, "phase": "DRAINING",
                                    "conditions": [condition(
                                        "Draining", "True", "PodsRemaining",
                                        f"{len(live)} tenant pod(s) remain")]})
                return

            # 2. hard precondition before ANY external call
            allocated = fake_gpu_allocated(node_name)
            if allocated != 0:
                quarantine(name, node_id, adapter, "AllocationNonZero",
                           f"{FAKE_GPU} still allocated: {allocated}", node)
                return

            checks = run_pre_list_checks(node_name, node_id)
            if any(not c["passed"] for c in checks):
                quarantine(name, node_id, adapter, "PreListCheckFailed",
                           f"failed: {[c['check'] for c in checks if not c['passed']]}",
                           node)
                return

            # 3. the external side effect, keyed for idempotency
            try:
                code, resp = adapter.list_machine(node_id, transition_id)
                op = resp.get("operationId")
            except Exception as exc:                      # noqa: BLE001
                # UNKNOWN outcome. Query by the same key; never blind-retry.
                log("WARN", "list outcome uncertain; querying state",
                    node=node_id, transition_id=transition_id,
                    error_class=type(exc).__name__)
                try:
                    _, machine2 = adapter.get(node_id)
                except Exception as exc2:                 # noqa: BLE001
                    quarantine(name, node_id, adapter, "ListOutcomeUnknown",
                               "list result unknown and readback unavailable: "
                               f"{type(exc2).__name__}", node)
                    return
                if not machine2.get("listed"):
                    patch_status(name, {**base_status, "phase": "DRAINING",
                                        "conditions": [condition(
                                            "ListPending", "True",
                                            "RetryAfterReadback",
                                            "list did not take effect; safe "
                                            "to retry next cycle")]})
                    return
                op = "recovered-by-readback"
                log("INFO", "list had in fact committed; adopting state",
                    node=node_id, transition_id=transition_id)

            # 4. verify by readback before declaring success
            _, machine3 = adapter.get(node_id)
            if not machine3.get("listed"):
                quarantine(name, node_id, adapter, "ListReadbackMismatch",
                           "adapter accepted list but readback says unlisted",
                           node)
                return

            # add the VAST taint and drop the transition taint together;
            # doing it in two patches erases the first (see update_taints)
            update_taints(node_name, add=[taint(VAST_TAINT, "true")],
                          remove=[TRANSITION_TAINT])
            set_owner_label(node_name, "VAST")
            active_now = int(machine3.get("activeContracts", 0))
            patch_status(name, {
                **base_status, "observedOwner": "VAST",
                "activeContracts": active_now,
                "listed": True, "operationId": op,
                "phase": "VAST_RENTED" if active_now > 0 else "VAST_READY",
                "conditions": [condition("Listed", "True", "ListVerified",
                                         f"operationId={op}")]})
            emit_event(name, "Listed", f"machine listed, operationId={op}")
            log("INFO", "handover complete", node=node_id,
                transition_id=transition_id, operation_id=op)
            with _metrics_lock:
                _metrics["transitions_total"]["ARISE->VAST"] = \
                    _metrics["transitions_total"].get("ARISE->VAST", 0) + 1
            return

    # ==================== desired DIRECT / QUARANTINED ===================
    # ==================== desired DIRECT (customer reservation) ==========
    # This is the mechanism the platform relies on for customer capacity
    # guarantees, after cross-queue reclaim was found not to deliver them
    # (runbooks/gaps.md §7). The guarantee is structural rather than a
    # scheduler heuristic: the customer is given whole NODES, and internal
    # work is excluded by admission policy and taint — not by a fairness
    # calculation that can be renegotiated under load.
    if desired == "DIRECT":
        # Stop NEW VAST bookings FIRST — before the contract gate — exactly as the
        # ARISE reclaim path does. A still-listed node is rentable, so leaving it
        # listed while we wait for an existing contract to end lets VAST keep
        # booking, and the reservation never converges; and reserving an idle-but-
        # listed node without unlisting first is outright dual ownership (the P0
        # the OwnerConflict alert can't see, since the label reads one owner).
        # listed is re-read every cycle, so we proceed only once it delists.
        if listed:
            try:
                adapter.unlist_machine(node_id)
                emit_event(name, "Unlisted",
                           "unlisted from VAST before Direct reservation")
            except Exception as exc:                      # noqa: BLE001
                log("ERROR", "unlist before Direct failed", node=node_id,
                    error_class=type(exc).__name__)
                patch_status(name, {**base_status, "phase": "VAST_RENTED"})
                return
            patch_status(name, {**base_status, "phase": "PENDING"})
            return

        if active > 0:
            # Delisted but a contract is still live: the existing rental must run
            # to its end before we can hand the node over (no double-selling).
            emit_event(name, "DirectBlocked",
                       f"{active} active VAST contract(s); cannot reserve for "
                       "a Direct customer until they end", etype="Warning")
            patch_status(name, {**base_status, "phase": "VAST_RENTED"})
            return

        if phase == "DIRECT_ASSIGNED":
            # Steady state still enforces the invariant every cycle (OWN-06).
            spec_now = node.get("spec", {}) or {}
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
                    node=node_id, drift=drift)
                emit_event(name, "IsolationDriftCorrected",
                           "; ".join(drift), etype="Warning")
                update_taints(node_name, add=[taint(DIRECT_TAINT, "true")],
                              remove=[TRANSITION_TAINT, VAST_TAINT])
                cordon(node_name, False)
            patch_status(name, {**base_status, "observedOwner": "DIRECT",
                                "phase": "DIRECT_ASSIGNED"})
            return

        # 1. cordon + taint before evicting, exactly as for a VAST handover.
        if phase in ("PENDING", ""):
            cordon(node_name, True)
            update_taints(node_name, add=[taint(TRANSITION_TAINT, "draining")])
            patch_status(name, {**base_status, "phase": "DRAINING"})
            emit_event(name, "DrainStarted",
                       f"reserving for Direct customer, transition {transition_id}")
            return

        if phase == "DRAINING":
            # Only ARISE-side tenants are drained. tenant-direct workloads are
            # the intended residents of this node, not obstacles to remove.
            live = [p for p in pods_on_node(node_name, ("tenant-arise",))
                    if p.get("status", {}).get("phase") not in
                    ("Succeeded", "Failed")]
            if live:
                elapsed = time.time() - tstate["startedAt"]
                blocked = []
                for pod in live:
                    ok_, detail = evict_pod(pod)
                    if not ok_:
                        blocked.append(f"{pod['metadata']['namespace']}/"
                                       f"{pod['metadata']['name']}: {detail}")
                if blocked and elapsed > DRAIN_TIMEOUT:
                    quarantine(name, node_id, adapter, "DrainBlocked",
                               "eviction blocked past timeout: " +
                               "; ".join(blocked)[:600], node)
                    return
                patch_status(name, {**base_status, "phase": "DRAINING",
                                    "conditions": [condition(
                                        "Draining", "True", "PodsRemaining",
                                        f"{len(live)} internal pod(s) remain")]})
                return

            allocated = fake_gpu_allocated(node_name)
            if allocated != 0:
                quarantine(name, node_id, adapter, "AllocationNonZero",
                           f"{FAKE_GPU} still allocated: {allocated}", node)
                return

            # 2. Hand over: taint against everyone else, then UNCORDON so the
            #    customer's own workloads can actually be placed. Removing
            #    VAST_TAINT too: a node reclaimed from VAST still carries its
            #    NoSchedule taint, which would deny the Direct customer the node
            #    they reserved (the drain path leaves it behind).
            update_taints(node_name, add=[taint(DIRECT_TAINT, "true")],
                          remove=[TRANSITION_TAINT, VAST_TAINT])
            cordon(node_name, False)
            set_owner_label(node_name, "DIRECT")
            patch_status(name, {**base_status, "observedOwner": "DIRECT",
                                "phase": "DIRECT_ASSIGNED",
                                "conditions": [condition(
                                    "Reserved", "True", "DirectAssigned",
                                    "node reserved for a Direct customer")]})
            emit_event(name, "DirectAssigned",
                       "node drained of internal work and reserved")
            log("INFO", "direct reservation complete", node=node_id,
                transition_id=transition_id)
            with _metrics_lock:
                _metrics["transitions_total"]["ARISE->DIRECT"] = \
                    _metrics["transitions_total"].get("ARISE->DIRECT", 0) + 1
            state.pop(key, None)
            return
        return

    if desired == "QUARANTINED":
        quarantine(name, node_id, adapter, "OperatorRequested",
                   f"quarantine requested by {spec.get('approvedBy','-')}",
                   node)
        return


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
         "passed": len(pods_on_node(node_name, TENANT_NAMESPACES)) == 0,
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
    remaining = pods_on_node(node_name, TENANT_NAMESPACES)
    return [
        {"check": "customer_workloads_stopped", "passed": len(remaining) == 0,
         "detail": f"{len(remaining)} tenant pod(s) remain",
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
            for candidate in ("ARISE", "DIRECT", "VAST", "QUARANTINED"):
                out.append(f'arise_node_owner{{node="{node}",'
                           f'owner="{candidate}"}} '
                           f'{1 if owner == candidate else 0}')
        out += ["# HELP arise_active_contracts Active contracts per node.",
                "# TYPE arise_active_contracts gauge"]
        for node, n in _metrics["contracts"].items():
            out.append(f'arise_active_contracts{{node="{node}",'
                       f'platform="vast-mock"}} {n}')
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
    try:
        adapter = VastAdapter(VAST_BASE)
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
            # Metrics first: ownership must be observable even for nodes that
            # currently have no NodeOwnership object.
            refresh_owner_metrics()
            crs = list_crs()
            # Prune per-CR metrics for nodes whose NodeOwnership was deleted.
            # A contracts gauge frozen at its last reconciled value outlives
            # the CR and keeps firing ContractReclaimAttempt forever — a fake
            # P0 observed on dgx04 (2026-08-14) after test-fixture cleanup.
            # Absent series is the honest state for an unmanaged node.
            live = {c["metadata"]["name"] for c in crs}
            with _metrics_lock:
                for gone in [n for n in _metrics["contracts"] if n not in live]:
                    del _metrics["contracts"][gone]
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
