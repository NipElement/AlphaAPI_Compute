#!/usr/bin/env python3
"""
VAST Mock API — isolated stand-in for the vast.ai host API (plan §8.4).

=============================================================================
WHY A MOCK AT ALL, AND WHY IT MAY NEVER BE POINTED AT PRODUCTION
=============================================================================
The commercial fact this whole state machine exists to respect: once a rental
contract exists, it cannot be terminated by editing an offer or by unlisting.
The host must honour it until every active contract ends (plan §8.6 / §1.3).
So `unlist` is NOT `reclaim`, and any code that conflates them can strand a
paying customer. This mock exists to let us prove the controller never makes
that mistake, WITHOUT touching a real marketplace.

Hard guarantees of this process:
  - holds no credential of any kind, and reads no credential from anywhere
  - has no outbound network code path whatsoever
  - is reachable only from the capacity-controller and test-system namespaces
    (see platform/base/networkpolicies.yaml)
  - binds a ClusterIP service; never a host port, never a public address

Time is driven by an INJECTABLE CLOCK. Contract expiry is simulated by moving
the mock's clock, never by changing the EC2 system clock — doing that would
break TLS, SSH and log ordering on a shared production host (plan §8.4).

Dependencies: Python standard library only.
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
STATE_PATH = os.environ.get("STATE_PATH", "/data/state.json")

_lock = threading.RLock()


def now() -> float:
    """Injectable clock: real time plus a test-controlled offset."""
    return time.time() + _state["clockOffsetSeconds"]


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def log(level: str, msg: str, **kw) -> None:
    rec = {
        "timestamp": iso(time.time()),
        "level": level,
        "component": "vast-mock",
        "msg": msg,
    }
    rec.update(kw)
    print(json.dumps(rec), flush=True)


# ---------------------------------------------------------------- state ----
def _blank_machine() -> dict:
    return {
        "listed": False,
        "contracts": [],          # each: {id, status, endAt, createdAt}
        "listOperations": {},     # idempotencyKey -> operationId
        "sideEffectCounts": {"list": 0, "unlist": 0},
    }


_state = {
    "machines": {},               # machineId -> machine dict
    "faults": {},                 # faultId -> fault dict
    "clockOffsetSeconds": 0.0,
    "auditLog": [],               # append-only; the evidence trail
}


def machine(mid: str) -> dict:
    with _lock:
        if mid not in _state["machines"]:
            _state["machines"][mid] = _blank_machine()
        return _state["machines"][mid]


def active_contracts(mid: str) -> list[dict]:
    """A contract is active until the injectable clock passes its endAt.

    Note this is computed, never cached: a stale cached count is exactly the
    bug that would let a rented node be reclaimed.
    """
    m = machine(mid)
    out = []
    for c in m["contracts"]:
        if c["status"] == "ended":
            continue
        if c.get("endAt") and now() >= c["endAt"]:
            continue
        out.append(c)
    return out


def latest_end(mid: str):
    ends = [c["endAt"] for c in active_contracts(mid) if c.get("endAt")]
    return iso(max(ends)) if ends else None


def audit(action: str, mid: str, **kw) -> None:
    entry = {"at": iso(now()), "action": action, "machineId": mid}
    entry.update(kw)
    with _lock:
        _state["auditLog"].append(entry)
    log("INFO", "audit", **entry)


def persist() -> None:
    """Best-effort durability so a mock restart does not lose contracts
    (E2E-03 restarts components and expects the block to survive)."""
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = f"{STATE_PATH}.tmp"
        with _lock, open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_state, fh)
        os.replace(tmp, STATE_PATH)      # atomic
    except OSError as exc:
        log("WARN", "state persist failed", detail=str(exc)[:200])


def restore() -> None:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            loaded = json.load(fh)
        with _lock:
            _state.update(loaded)
        log("INFO", "state restored", machines=len(_state["machines"]))
    except (OSError, json.JSONDecodeError):
        log("INFO", "no prior state; starting empty")


# ---------------------------------------------------------------- faults ---
def consume_fault(path: str):
    """Return a fault to apply to this request, decrementing its budget.

    Faults are fixed-count so a test cannot accidentally wedge the mock
    permanently (plan §8.4 "固定次数").
    """
    with _lock:
        for fid, f in list(_state["faults"].items()):
            if f["remaining"] <= 0:
                continue
            if f.get("pathContains") and f["pathContains"] not in path:
                continue
            f["remaining"] -= 1
            return fid, f
    return None, None


# ------------------------------------------------------------- HTTP layer --
class Handler(BaseHTTPRequestHandler):
    server_version = "vast-mock/1.0"

    def _send(self, code: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def _apply_fault(self) -> bool:
        """Returns True if the request was consumed by a fault."""
        fid, f = consume_fault(self.path)
        if not f:
            return False
        kind = f["kind"]
        log("WARN", "fault injected", fault_id=fid, kind=kind, path=self.path)

        if kind == "timeout":
            # Hang past any sane client timeout, then close without replying.
            time.sleep(f.get("delaySeconds", 30))
            try:
                self.close_connection = True
            except Exception:                            # noqa: BLE001
                pass
            return True
        if kind == "delay":
            time.sleep(f.get("delaySeconds", 5))
            return False          # delayed, but still served normally
        if kind == "drop_after_write":
            # VST-04 / E2E-06: the side effect COMMITS, then the connection
            # dies before the client learns the outcome. This is the hardest
            # real-world case and the reason list must be idempotency-keyed.
            return "drop_after_write"
        self._send(int(kind), {"error": f"injected {kind}", "faultId": fid})
        return True

    # ------------------------------------------------------------- GET ----
    def do_GET(self):                                    # noqa: N802
        if self.path in ("/healthz", "/readyz"):
            self._send(200, {"status": "ok", "clockOffsetSeconds":
                             _state["clockOffsetSeconds"]})
            return
        if self._apply_fault() is True:
            return

        parts = [p for p in self.path.split("?")[0].split("/") if p]
        # GET /v1/machines/{id}
        if len(parts) == 3 and parts[:2] == ["v1", "machines"]:
            mid = parts[2]
            m = machine(mid)
            act = active_contracts(mid)
            self._send(200, {
                "machineId": mid,
                "listed": m["listed"],
                "activeContracts": len(act),
                "rentalEndAt": latest_end(mid),
                "contracts": act,
                "sideEffectCounts": m["sideEffectCounts"],
            })
            return
        if self.path == "/v1/test/audit":
            self._send(200, {"auditLog": _state["auditLog"]})
            return
        if self.path == "/v1/test/state":
            self._send(200, _state)
            return
        self._send(404, {"error": "not found"})

    # ------------------------------------------------------------ POST ----
    def do_POST(self):                                   # noqa: N802
        fault_result = self._apply_fault()
        if fault_result is True:
            return
        drop_after_write = (fault_result == "drop_after_write")

        parts = [p for p in self.path.split("?")[0].split("/") if p]
        body = self._body()

        # ---- POST /v1/machines/{id}/list -------------------------------
        if len(parts) == 4 and parts[:2] == ["v1", "machines"] and parts[3] == "list":
            mid = parts[2]
            key = body.get("idempotencyKey") or self.headers.get("Idempotency-Key")
            if not key:
                self._send(400, {"error": "idempotencyKey required"})
                return
            with _lock:
                m = machine(mid)
                if key in m["listOperations"]:
                    # Replay: same key -> same operationId, NO second side
                    # effect. This is what OWN-04 / VST-04 assert.
                    op = m["listOperations"][key]
                    audit("list.replay", mid, operationId=op, idempotencyKey=key)
                    self._send(202, {"operationId": op, "replayed": True,
                                     "listed": m["listed"]})
                    return
                op = f"op-{uuid.uuid4().hex[:12]}"
                m["listOperations"][key] = op
                m["listed"] = True
                m["sideEffectCounts"]["list"] += 1
                audit("list", mid, operationId=op, idempotencyKey=key,
                      sideEffectCount=m["sideEffectCounts"]["list"])
            persist()
            if drop_after_write:
                # committed, but the caller never finds out
                log("WARN", "dropping connection after committed write",
                    machine_id=mid, operation_id=op)
                self.close_connection = True
                return
            self._send(202, {"operationId": op, "replayed": False, "listed": True})
            return

        # ---- POST /v1/machines/{id}/unlist -----------------------------
        if len(parts) == 4 and parts[:2] == ["v1", "machines"] and parts[3] == "unlist":
            mid = parts[2]
            with _lock:
                m = machine(mid)
                m["listed"] = False
                m["sideEffectCounts"]["unlist"] += 1
                remaining = len(active_contracts(mid))
                audit("unlist", mid, activeContractsRemaining=remaining)
            persist()
            # CRITICAL: unlisting stops NEW contracts. It does not end existing
            # ones. The response says so explicitly so no caller can misread it.
            self._send(200, {
                "machineId": mid,
                "listed": False,
                "activeContracts": remaining,
                "rentalEndAt": latest_end(mid),
                "note": "unlist stops new contracts only; active rentals continue",
            })
            return

        # ---- POST /v1/test/contracts -----------------------------------
        if self.path == "/v1/test/contracts":
            mid = body.get("machineId")
            if not mid:
                self._send(400, {"error": "machineId required"})
                return
            action = body.get("action", "create")
            with _lock:
                m = machine(mid)
                if action == "create":
                    cid = body.get("contractId") or f"c-{uuid.uuid4().hex[:10]}"
                    dur = float(body.get("durationSeconds", 3600))
                    c = {"id": cid, "status": "active",
                         "createdAt": now(), "endAt": now() + dur}
                    m["contracts"].append(c)
                    audit("contract.create", mid, contractId=cid,
                          endAt=iso(c["endAt"]))
                    persist()
                    self._send(201, {"contractId": cid, "status": "active",
                                     "endAt": iso(c["endAt"])})
                    return
                if action == "end":
                    cid = body.get("contractId")
                    for c in m["contracts"]:
                        if c["id"] == cid:
                            c["status"] = "ended"
                            c["endAt"] = now()
                            audit("contract.end", mid, contractId=cid)
                            persist()
                            self._send(200, {"contractId": cid,
                                             "status": "ended"})
                            return
                    self._send(404, {"error": "contract not found"})
                    return
            self._send(400, {"error": "unknown action"})
            return

        # ---- POST /v1/test/faults --------------------------------------
        if self.path == "/v1/test/faults":
            kind = str(body.get("kind", ""))
            if kind not in ("429", "500", "503", "timeout", "delay",
                            "drop_after_write"):
                self._send(400, {"error": "unsupported fault kind"})
                return
            fid = body.get("faultId") or f"f-{uuid.uuid4().hex[:8]}"
            with _lock:
                _state["faults"][fid] = {
                    "kind": kind,
                    "remaining": int(body.get("count", 1)),
                    "delaySeconds": float(body.get("delaySeconds", 5)),
                    "pathContains": body.get("pathContains"),
                }
            audit("fault.create", body.get("machineId", "-"),
                  faultId=fid, kind=kind)
            self._send(201, {"faultId": fid, **_state["faults"][fid]})
            return

        if self.path == "/v1/test/faults/clear":
            with _lock:
                _state["faults"].clear()
            self._send(200, {"cleared": True})
            return

        # ---- POST /v1/test/clock ---------------------------------------
        if self.path == "/v1/test/clock":
            with _lock:
                _state["clockOffsetSeconds"] = float(
                    body.get("offsetSeconds", _state["clockOffsetSeconds"]))
            audit("clock.set", "-",
                  offsetSeconds=_state["clockOffsetSeconds"])
            persist()
            self._send(200, {"clockOffsetSeconds": _state["clockOffsetSeconds"],
                             "now": iso(now())})
            return

        # ---- POST /v1/test/reset ---------------------------------------
        if self.path == "/v1/test/reset":
            with _lock:
                _state["machines"].clear()
                _state["faults"].clear()
                _state["auditLog"].clear()
                _state["clockOffsetSeconds"] = 0.0
            persist()
            self._send(200, {"reset": True})
            return

        self._send(404, {"error": "not found"})


def main() -> None:
    restore()
    log("INFO", "vast-mock listening", port=PORT,
        note="mock only; holds no credentials and makes no outbound calls")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
