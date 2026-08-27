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
    mt.SEEN_PATH = os.path.join(d, "last_seen.json")   # per-test side file
    mt.list_tenant_pvcs = lambda: []                    # volumes opt in per test
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
         finished=None, deleted=None, init_gpu=0):
    st = {"phase": phase, "startTime": start,
          "containerStatuses": [{"state": {"running": {}} if running
                                 else {"terminated": {"finishedAt": finished or start}}}]}
    md = {"uid": uid, "name": f"pod-{uid}", "namespace": "tenant-arise",
          "labels": {"arise.ai/kind": "devmachine"}}
    if deleted:
        md["deletionTimestamp"] = deleted
    spec = {"nodeName": "node-a", "containers": [{"resources": {
                "requests": {mt.GPU_RESOURCE: str(gpu), mt.VCPU_RESOURCE: "32"}}}]}
    if init_gpu:
        spec["initContainers"] = [{"resources": {"requests": {mt.GPU_RESOURCE: str(init_gpu)}}}]
    return {"metadata": md, "status": st, "spec": spec}


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
    assert len(closes) == 1 and closes[0]["at_source"] == "last-seen-holding", closes


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



def t_meter_crashloop_does_not_reopen():
    """P0 from review: a container restart is NOT a new interval. The pod
    keeps its GPUs through CrashLoopBackOff; the first cut closed and re-opened
    from the ORIGINAL startTime and billed the hour twice."""
    led = mt.Ledger(tmp_ledger())
    m = mt.Meter(led)
    pod = _pod("u5")
    mt.list_tenant_pods = lambda: [pod]
    m.tick()
    pod["status"]["containerStatuses"] = [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]
    m.tick(); m.tick()
    events = [r["event"] for r in led.records]
    assert events == ["open"], f"crash loop must not close/re-open: {events}"


def t_meter_reopen_floors_at_previous_close():
    led = mt.Ledger(tmp_ledger())
    m = mt.Meter(led)
    pod = _pod("u6", start="2026-09-01T00:00:00Z")
    mt.list_tenant_pods = lambda: [pod]
    m.tick()
    pod["status"]["phase"] = "Failed"
    pod["status"]["containerStatuses"] = [{"state": {"terminated": {"finishedAt": "2026-09-01T01:00:00Z"}}}]
    m.tick()                                   # closed at 01:00
    pod["status"]["phase"] = "Running"
    pod["status"]["containerStatuses"] = [{"state": {"running": {}}}]
    m.done.discard("u6")                       # force the re-open path
    m.tick()
    opens = [r for r in led.records if r["event"] == "open"]
    assert opens[-1]["at"] == "2026-09-01T01:00:00Z", opens[-1]
    assert "re-open floor" in opens[-1]["at_source"], opens[-1]


def t_meter_init_container_counts():
    led = mt.Ledger(tmp_ledger())
    m = mt.Meter(led)
    p = _pod("u7", gpu=0, init_gpu=8)
    p["status"]["phase"] = "Pending"
    mt.list_tenant_pods = lambda: [p]
    m.tick()
    opens = [r for r in led.records if r["event"] == "open"]
    assert opens and opens[0]["gpu"] == 8, f"init-container GPUs unmetered: {opens}"


def t_meter_subpoll_pod_recorded():
    led = mt.Ledger(tmp_ledger())
    m = mt.Meter(led)
    p = _pod("u8", phase="Succeeded", running=False,
             start="2026-09-01T00:00:00Z", finished="2026-09-01T00:00:10Z")
    mt.list_tenant_pods = lambda: [p]
    m.tick(); m.tick()
    events = [r["event"] for r in led.records]
    assert events == ["open", "close"], events
    assert led.records[1]["at"] == "2026-09-01T00:00:10Z", led.records[1]


def t_meter_absent_pod_closes_at_last_seen():
    path = tmp_ledger()
    led = mt.Ledger(path); m = mt.Meter(led)
    pod = _pod("u9")
    mt.list_tenant_pods = lambda: [pod]
    m.tick()
    seen_at = m.seen["u9"]
    m2 = mt.Meter(mt.Ledger(path))
    mt.list_tenant_pods = lambda: []
    m2.tick()
    close = [r for r in mt.Ledger(path).records if r["event"] == "close"][0]
    assert close["at"] == seen_at and close["at_source"] == "last-seen-holding", close


def t_ledger_torn_tail_tolerated():
    path = tmp_ledger()
    led = mt.Ledger(path)
    led.append({"event": "open", "pod_uid": "x", "tenant": "t", "pod": "p", "kind": "k",
                "node": "n", "gpu": 1, "vcpu": 1, "mem_gi": 1, "at": "x", "at_source": "x", "ts": "x"})
    with open(path, "a") as fh:
        fh.write('{"event":"close","pod_uid":"x","tena')
    led2 = mt.Ledger(path)
    assert led2.chain_ok and len(led2.records) == 1, "torn tail must be dropped, chain intact"
    assert open(path, "rb").read().endswith(b"\n"), "file must be truncated to the last good line"


def t_epoch_is_utc_regardless_of_tz():
    import time as _t
    os.environ["TZ"] = "Europe/Berlin"; _t.tzset()
    try:
        assert mt._iso_to_epoch("2026-10-25T00:00:00Z") == 1792886400.0
    finally:
        os.environ["TZ"] = "UTC"; _t.tzset()


def t_mem_gi_parses_units():
    assert mt._mem_gi("64Gi") == 64 and mt._mem_gi("512Mi") == 0, (mt._mem_gi("64Gi"), mt._mem_gi("512Mi"))


def _pvc(uid, name="data", gib=20, phase="Bound", cls="arise-longterm"):
    return {"metadata": {"uid": uid, "name": name, "namespace": "tenant-direct"},
            "spec": {"storageClassName": cls, "resources": {"requests": {"storage": f"{gib}Gi"}}},
            "status": {"phase": phase, "capacity": {"storage": f"{gib}Gi"}}}


def t_meter_bound_pvc_is_a_volume_interval():
    led = mt.Ledger(tmp_ledger()); m = mt.Meter(led)
    pvcs = [_pvc("v1", gib=30), _pvc("v2", gib=10, phase="Pending")]
    mt.list_tenant_pods = lambda: []
    mt.list_tenant_pvcs = lambda: pvcs
    m.tick(); m.tick()
    opens = [r for r in led.records if r["event"] == "open"]
    assert len(opens) == 1 and opens[0]["kind"] == "volume" and opens[0]["storage_gib"] == 30, opens
    assert opens[0]["at_source"] == "observed(bound)", opens[0]
    assert 'arise_metering_storage_gib_allocated{tenant="tenant-direct"} 30' in mt.render_metrics(m), "gauge while bound"
    pvcs[:] = []                                        # claim deleted
    m.tick()
    closes = [r for r in led.records if r["event"] == "close"]
    assert len(closes) == 1 and closes[0]["storage_gib"] == 30 and closes[0]["at_source"] == "last-seen-holding", closes
    assert 'storage_gib_allocated{tenant="tenant-direct"} 30' not in mt.render_metrics(m), "gauge must drop after close"


def t_invoice_volume_line_is_included_at_zero():
    path = tmp_ledger(); led = mt.Ledger(path)
    led.append({"event": "open", "pod_uid": "v1", "tenant": "tenant-direct", "pod": "data", "kind": "volume",
                "node": "", "gpu": 0, "vcpu": 0, "mem_gi": 0, "storage_gib": 300, "storage_class": "arise-longterm",
                "at": "2026-09-01T00:00:00Z", "at_source": "observed(bound)", "ts": "x"})
    led.append({"event": "close", "pod_uid": "v1", "tenant": "tenant-direct", "pod": "data", "kind": "volume",
                "node": "", "gpu": 0, "vcpu": 0, "mem_gi": 0, "storage_gib": 300, "storage_class": "arise-longterm",
                "at": "2026-09-16T00:00:00Z", "at_source": "x", "ts": "x", "opened_at": "2026-09-01T00:00:00Z"})
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    assert rc == 0, out
    line = [l for l in out.splitlines() if "storage-gib-month" in l][0].split(",")
    assert line[9] == "0.00" and "150.00 GiB-month" in line[10] and "included" in line[10], line


# ------------------------------------------------------------ invoice ------

def run_invoice(ledger_path, tenant, start, end, kind=None, extra=(), pricebook=None):
    cmd = [sys.executable, str(REPO / "billing/invoice.py"), "--ledger", ledger_path,
           "--pricebook", pricebook or str(REPO / "billing/pricebook.yaml"),
           "--tenants", str(REPO / "platform/tenants.yaml"), "--tenant", tenant,
           "--from", start, "--to", end, *(["--tenant-kind", kind] if kind else []), *extra]
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
    rc, out = run_invoice(path, "tenant-arise", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
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


def t_invoice_dedicated_prorated_by_month():
    path = tmp_ledger()
    mt.Ledger(path)
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z",
                          extra=["--dedicated-nodes", "node-a",
                                 "--dedicated-from", "2026-09-16T00:00:00Z"])
    assert rc == 0, out
    line = [l for l in out.splitlines() if "node-month" in l][0].split(",")
    assert line[9] == "26375.00", f"15/30 days of 52750 = 26375, got {line}"
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z",
                          extra=["--dedicated-nodes", "node-a"])
    line = [l for l in out.splitlines() if "node-month" in l][0].split(",")
    assert line[9] == "12308.33", f"7/30 of 52750, got {line}"


def t_invoice_dedicated_node_not_double_billed():
    path = tmp_ledger()
    _closed(path, "d1", "tenant-direct", "2026-09-10T00:00:00Z", "2026-09-11T00:00:00Z", gpu=8)
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z",
                          extra=["--dedicated-nodes", "n"])
    assert rc == 0, out
    line = [l for l in out.splitlines() if ",pod-d1," in l][0].split(",")
    assert line[9] == "0.00" and "covered by" in line[10], line
    total = [l for l in out.splitlines() if l.startswith("TOTAL")][0].split(",")
    assert total[9] == "52750.00", f"node-month only, got {total}"


def t_invoice_bills_open_intervals():
    path = tmp_ledger()
    led = mt.Ledger(path)
    led.append({"event": "open", "pod_uid": "o1", "tenant": "tenant-direct", "pod": "pod-o1",
                "kind": "devmachine", "node": "x", "gpu": 1, "vcpu": 32, "mem_gi": 256,
                "at": "2026-09-30T23:00:00Z", "at_source": "x", "ts": "x"})
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    assert rc == 0, out
    line = [l for l in out.splitlines() if ",pod-o1," in l][0].split(",")
    assert line[6] == "3600" and line[9] == "9.57" and "OPEN" in line[10], line


def t_invoice_splits_at_price_change():
    import shutil
    book = tempfile.mkdtemp() + "/book.yaml"
    shutil.copy(REPO / "billing/pricebook.yaml", book)
    with open(book, "a") as fh:
        fh.write("\n  - sku: gpu-hour.b300.on-demand\n    unit: gpu-hour\n"
                 "    unit_price_micros: 20000000\n    effective_from: \"2026-09-10T12:00:00Z\"\n"
                 "    bill_granularity_seconds: 60\n    tenant_kinds: [customer]\n")
    path = tmp_ledger()
    _closed(path, "s1", "tenant-direct", "2026-09-10T11:00:00Z", "2026-09-10T13:00:00Z")
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z",
                          pricebook=book)
    assert rc == 0, out
    segs = [l.split(",") for l in out.splitlines() if ",pod-s1," in l]
    assert len(segs) == 2 and segs[0][9] == "9.57" and segs[1][9] == "20.00", segs


def t_invoice_refuses_broken_chain():
    path = tmp_ledger()
    _closed(path, "b1", "tenant-direct", "2026-09-10T00:00:00Z", "2026-09-10T01:00:00Z")
    lines = Path(path).read_text().splitlines()
    rec = json.loads(lines[0]); rec["gpu"] = 8
    lines[0] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    Path(path).write_text("\n".join(lines) + "\n")
    rc, out = run_invoice(path, "tenant-direct", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    assert rc == 3, f"broken chain must exit 3, got {rc}: {out}"


def t_invoice_unknown_tenant_refuses_to_guess_kind():
    path = tmp_ledger(); mt.Ledger(path)
    rc, out = run_invoice(path, "tenant-ghost", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
    assert rc != 0, "unknown tenant kind must not default to a rate card"


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
    ("meter: crash loop does not re-open (no double billing)", t_meter_crashloop_does_not_reopen),
    ("meter: re-open floors at previous close", t_meter_reopen_floors_at_previous_close),
    ("meter: init-container GPUs count (effective request)", t_meter_init_container_counts),
    ("meter: sub-poll pod recorded from API timestamps", t_meter_subpoll_pod_recorded),
    ("meter: absent pod closes at last-seen, not restart time", t_meter_absent_pod_closes_at_last_seen),
    ("ledger: torn trailing line tolerated", t_ledger_torn_tail_tolerated),
    ("epoch parsing is UTC regardless of TZ", t_epoch_is_utc_regardless_of_tz),
    ("memory quantities parse to GiB", t_mem_gi_parses_units),
    ("invoice: dedicated pro-rated by calendar month", t_invoice_dedicated_prorated_by_month),
    ("invoice: dedicated node's own pods not double-billed", t_invoice_dedicated_node_not_double_billed),
    ("invoice: open intervals billed this period", t_invoice_bills_open_intervals),
    ("invoice: splits at a price change", t_invoice_splits_at_price_change),
    ("invoice: refuses a broken chain", t_invoice_refuses_broken_chain),
    ("invoice: unknown tenant kind refuses to guess", t_invoice_unknown_tenant_refuses_to_guess_kind),
    ("meter: a Bound PVC is a volume interval (open/close/metric)", t_meter_bound_pvc_is_a_volume_interval),
    ("invoice: volume line = GiB-months at $0 'included'", t_invoice_volume_line_is_included_at_zero),
    ("invoice: deterministic bytes", t_invoice_is_deterministic),
]
print(f"metering unit tests ({len(checks)}):")
for name, fn in checks:
    check(name, fn)
if FAILS:
    print(f"FAIL: {len(FAILS)}/{len(checks)}"); sys.exit(1)
print(f"PASS: {len(checks)}/{len(checks)}")
