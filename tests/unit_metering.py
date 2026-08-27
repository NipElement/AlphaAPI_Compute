#!/usr/bin/env python3
"""Unit tests for the metering ledger and the invoice generator — no cluster.

Wired into scripts/validate.sh. Covers:
  - ledger: append/verify round-trip; a tampered or deleted line breaks the
    chain at the right position; the open-set rebuild after a restart
  - meter: open exactly once per pod UID across ticks; close when the pod
    stops or vanishes; close timestamp source honesty
  - invoice: per-minute round-UP, window clipping, price versioning by
    effective_from, internal tenants at $0, unpriced intervals refuse to
    invent a rate, determinism (same inputs => identical bytes)
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FAILS = []


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mt = load(REPO / "services/metering/metering.py", "metering_under_test")
mt.log = lambda *a, **k: None


def check(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except AssertionError as exc:
        FAILS.append(name)
        print(f"  FAIL {name}: {exc}")
    except Exception as exc:                                  # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL {name}: unexpected {type(exc).__name__}: {exc}")


def tmp_ledger():
    d = tempfile.mkdtemp(prefix="mtr-")
    return os.path.join(d, "allocations.jsonl")


# ------------------------------------------------------------- ledger ------

def t_ledger_roundtrip():
    path = tmp_ledger()
    led = mt.Ledger(path)
    led.append({"event": "open", "pod_uid": "u1", "tenant": "tenant-arise",
                "pod": "p", "kind": "devmachine", "node": "n", "gpu": 2,
                "vcpu": 64, "mem_gi": 512, "at": "2026-09-01T00:00:00Z",
                "at_source": "status.startTime", "ts": "2026-09-01T00:00:05Z"})
    led2 = mt.Ledger(path)                     # reload from disk
    ok, broken = led2.verify()
    assert ok and broken is None, f"fresh ledger must verify: {broken}"
    assert led2.head == led.head, "head hash must survive reload"
    assert led2.records[0]["prev"] == mt.GENESIS


def t_ledger_tamper_detected():
    path = tmp_ledger()
    led = mt.Ledger(path)
    for i in range(3):
        led.append({"event": "open", "pod_uid": f"u{i}", "tenant": "t", "pod": "p",
                    "kind": "k", "node": "n", "gpu": 1, "vcpu": 1, "mem_gi": 1,
                    "at": "2026-09-01T00:00:00Z", "at_source": "x", "ts": "x"})
    lines = Path(path).read_text().splitlines()
    rec = json.loads(lines[1]); rec["gpu"] = 8          # someone edits line 2
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    Path(path).write_text("\n".join(lines) + "\n")
    led2 = mt.Ledger(path)
    assert led2.chain_ok is False, "edited record not detected"
    assert led2.broken_at == 2, f"break reported at {led2.broken_at}, want 2"


def t_ledger_deleted_line_detected():
    path = tmp_ledger()
    led = mt.Ledger(path)
    for i in range(3):
        led.append({"event": "open", "pod_uid": f"u{i}", "tenant": "t", "pod": "p",
                    "kind": "k", "node": "n", "gpu": 1, "vcpu": 1, "mem_gi": 1,
                    "at": "x", "at_source": "x", "ts": "x"})
    lines = Path(path).read_text().splitlines()
    del lines[1]                                        # someone deletes line 2
    Path(path).write_text("\n".join(lines) + "\n")
    led2 = mt.Ledger(path)
    assert led2.chain_ok is False, "deleted record not detected"
    assert led2.broken_at == 2, f"break at {led2.broken_at}, want 2"


def t_open_set_rebuild():
    path = tmp_ledger()
    led = mt.Ledger(path)
    base = {"tenant": "t", "pod": "p", "kind": "k", "node": "n", "gpu": 1,
            "vcpu": 1, "mem_gi": 1, "at": "x", "at_source": "x", "ts": "x"}
    led.append({"event": "open", "pod_uid": "a", **base})
    led.append({"event": "open", "pod_uid": "b", **base})
    led.append({"event": "close", "pod_uid": "a", "opened_at": "x", **base})
    assert set(mt.Ledger(path).open_set()) == {"b"}, "open set must be exactly {b}"


# -------------------------------------------------------------- meter ------

def _pod(uid, phase="Running", running=True, gpu=1, start="2026-09-01T00:00:00Z",
         finished=None, deleted=None):
    st = {"phase": phase, "startTime": start,
          "containerStatuses": [{"state": {"running": {}} if running
                                 else {"terminated": {"finishedAt": finished or start}}}]}
    md = {"uid": uid, "name": f"pod-{uid}", "namespace": "tenant-arise",
          "labels": {"arise.ai/kind": "devmachine"}}
    if deleted:
        md["deletionTimestamp"] = deleted
    return {"metadata": md, "status": st,
            "spec": {"nodeName": "node-a", "containers": [{"resources": {
                "requests": {mt.GPU_RESOURCE: str(gpu), mt.VCPU_RESOURCE: "32"}}}]}}


def t_meter_opens_once_and_closes():
    led = mt.Ledger(tmp_ledger())
    m = mt.Meter(led)
    pods = [_pod("u1")]
    mt.list_tenant_pods = lambda: pods
    m.tick(); m.tick(); m.tick()                       # three polls, same pod
    opens = [r for r in led.records if r["event"] == "open"]
    assert len(opens) == 1, f"pod opened {len(opens)} times, want exactly 1"
    assert opens[0]["at"] == "2026-09-01T00:00:00Z", "open.at must be startTime"
    assert opens[0]["at_source"] == "status.startTime"
    # pod terminates
    pods[:] = [_pod("u1", phase="Succeeded", running=False,
                    finished="2026-09-01T00:10:00Z")]
    m.tick(); m.tick()
    closes = [r for r in led.records if r["event"] == "close"]
    assert len(closes) == 1, f"closed {len(closes)} times, want 1"
    assert closes[0]["at"] == "2026-09-01T00:10:00Z"
    assert closes[0]["at_source"] == "containerStatuses.terminated.finishedAt"
    assert closes[0]["opened_at"] == "2026-09-01T00:00:00Z"


def t_meter_closes_on_disappearance():
    led = mt.Ledger(tmp_ledger())
    m = mt.Meter(led)
    pods = [_pod("u2")]
    mt.list_tenant_pods = lambda: pods
    m.tick()
    pods[:] = []                                       # deleted between polls
    m.tick()
    closes = [r for r in led.records if r["event"] == "close"]
    assert len(closes) == 1 and closes[0]["at_source"] == "observed(pod-absent)", closes


def t_meter_ignores_non_rentable_pods():
    led = mt.Ledger(tmp_ledger())
    m = mt.Meter(led)
    p = _pod("u3", gpu=0)
    p["spec"]["containers"][0]["resources"]["requests"] = {"cpu": "500m", "memory": "512Mi"}
    mt.list_tenant_pods = lambda: [p]
    m.tick()
    assert led.records == [], "a pod with no rentable request must not be metered"


def t_meter_restart_continues():
    path = tmp_ledger()
    led = mt.Ledger(path)
    m = mt.Meter(led)
    pods = [_pod("u4")]
    mt.list_tenant_pods = lambda: pods
    m.tick()
    # "restart": new Meter on the same file, pod is now gone
    m2 = mt.Meter(mt.Ledger(path))
    pods[:] = []
    m2.tick()
    events = [r["event"] for r in mt.Ledger(path).records]
    assert events == ["open", "close"], f"expected open,close across restart, got {events}"


# ------------------------------------------------------------ invoice ------

def run_invoice(ledger_path, tenant, start, end, kind="customer", extra=()):
    cmd = [sys.executable, str(REPO / "billing/invoice.py"), "--ledger", ledger_path,
           "--pricebook", str(REPO / "billing/pricebook.yaml"), "--tenant", tenant,
           "--from", start, "--to", end, "--tenant-kind", kind, *extra]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout


def _closed(path, uid, tenant, opened, closed, gpu=1):
    led = mt.Ledger(path)
    base = {"tenant": tenant, "pod": f"pod-{uid}", "kind": "devmachine", "node": "n",
            "gpu": gpu, "vcpu": 32, "mem_gi": 256, "at_source": "x", "ts": "x"}
    led.append({"event": "open", "pod_uid": uid, "at": opened, **base})
    led.append({"event": "close", "pod_uid": uid, "at": closed, "opened_at": opened, **base})


def t_invoice_rounds_up_to_minute():
    path = tmp_ledger()
    # 61 seconds on 1 GPU -> billed as 2 minutes -> 120/3600 * 9.57 = 0.319 -> $0.32
    _closed(path, "a", "tenant-direct", "2026-09-10T00:00:00Z", "2026-09-10T00:01:01Z")
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    assert rc == 0, out
    line = [l for l in out.splitlines() if ",pod-a," in l][0].split(",")
    assert line[6] == "120", f"billed_seconds {line[6]}, want 120 (round UP)"
    assert line[9] == "0.32", f"amount {line[9]}, want 0.32"


def t_invoice_clips_to_window():
    path = tmp_ledger()
    # 2 GPUs, 3 hours spanning the window start: only the 1h inside counts
    _closed(path, "b", "tenant-direct", "2026-08-31T22:00:00Z", "2026-09-01T01:00:00Z", gpu=2)
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    line = [l for l in out.splitlines() if ",pod-b," in l][0].split(",")
    assert line[6] == "3600", f"billed {line[6]}, want 3600 (clipped)"
    assert line[9] == "19.14", f"amount {line[9]}, want 19.14 (2 GPU x 1h x 9.57)"


def t_invoice_internal_is_zero():
    path = tmp_ledger()
    _closed(path, "c", "tenant-arise", "2026-09-10T00:00:00Z", "2026-09-10T02:00:00Z", gpu=8)
    rc, out = run_invoice(path, "tenant-arise", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", kind="internal")
    assert rc == 0, out
    line = [l for l in out.splitlines() if ",pod-c," in l][0].split(",")
    assert line[9] == "0.00", f"internal amount {line[9]}, want 0.00"


def t_invoice_refuses_to_invent_a_rate():
    path = tmp_ledger()
    # before any price is effective (book starts 2026-09-01)
    _closed(path, "d", "tenant-direct", "2026-08-01T00:00:00Z", "2026-08-01T01:00:00Z")
    rc, out = run_invoice(path, "tenant-direct", "2026-07-01T00:00:00Z", "2026-09-01T00:00:00Z")
    assert rc == 2, f"unpriced interval must exit 2, got {rc}"
    assert "NOT PRICED" in out, out


def t_invoice_dedicated_prorated():
    path = tmp_ledger()
    mt.Ledger(path)                                        # empty ledger
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z",
                          extra=["--dedicated-days", "15"])
    line = [l for l in out.splitlines() if "node-month" in l][0].split(",")
    assert line[9] == "26375.00", f"15/30 days of 52750 = 26375, got {line[9]}"


def t_invoice_is_deterministic():
    path = tmp_ledger()
    _closed(path, "e", "tenant-direct", "2026-09-10T00:00:00Z", "2026-09-10T01:00:00Z")
    _, a = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    _, b = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    assert a == b, "same inputs must produce byte-identical statements"
    assert "TOTAL" in a and "9.57" in a


checks = [
    ("ledger: append/verify round-trip, head survives reload", t_ledger_roundtrip),
    ("ledger: edited record breaks the chain at its line", t_ledger_tamper_detected),
    ("ledger: deleted record breaks the chain at its line", t_ledger_deleted_line_detected),
    ("ledger: open-set rebuild", t_open_set_rebuild),
    ("meter: opens exactly once, closes with honest timestamps", t_meter_opens_once_and_closes),
    ("meter: closes when the pod vanishes", t_meter_closes_on_disappearance),
    ("meter: ignores pods with no rentable request", t_meter_ignores_non_rentable_pods),
    ("meter: restart continues the same interval", t_meter_restart_continues),
    ("invoice: rounds UP to the minute", t_invoice_rounds_up_to_minute),
    ("invoice: clips intervals to the window", t_invoice_clips_to_window),
    ("invoice: internal tenant at $0", t_invoice_internal_is_zero),
    ("invoice: refuses to invent a rate", t_invoice_refuses_to_invent_a_rate),
    ("invoice: dedicated node pro-rated", t_invoice_dedicated_prorated),
    ("invoice: deterministic bytes", t_invoice_is_deterministic),
]
print(f"metering unit tests ({len(checks)}):")
for name, fn in checks:
    check(name, fn)
if FAILS:
    print(f"FAIL: {len(FAILS)}/{len(checks)}"); sys.exit(1)
print(f"PASS: {len(checks)}/{len(checks)}")
