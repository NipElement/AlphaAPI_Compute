#!/usr/bin/env python3
"""Unit tests for the capacity-controller adapter modes — no cluster needed.

Wired into scripts/validate.sh as an L0 gate. Covers:
  - mock-v1 constructs; an unknown adapter value refuses (VST-06's
    constructor leg, now testable without a cluster)
  - none constructs, tells the truth (unlisted / zero contracts), refuses
    list_machine, no-ops unlist_machine
  - the production flag refuses BOTH adapter classes
  - reconcile()'s no-marketplace gates: desired=VAST fails fast before any
    node mutation; a node with VAST state is held, never assumed
    contract-free

The module is imported directly (its main() is __main__-guarded and import
has no side effects); k8s helpers are stubbed per test.
"""
import importlib.util
import sys
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "services" / "capacity-controller" / "capacity_controller.py")

spec = importlib.util.spec_from_file_location("capacity_controller", SRC)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except AssertionError as exc:
        FAILS.append(name)
        print(f"  FAIL {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL {name}: unexpected {type(exc).__name__}: {exc}")


def must_not_be_called(what):
    def _stub(*a, **k):
        raise AssertionError(f"{what} must not be called on this path")
    return _stub


# ---------------------------------------------------------- constructors --

def t_mock_constructs():
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    a = cc.VastAdapter("http://example.invalid")
    assert a.base == "http://example.invalid", "base not stored"


def t_unknown_refuses():
    cc.VAST_ADAPTER = "production-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    try:
        cc.VastAdapter("http://example.invalid")
    except RuntimeError:
        return
    raise AssertionError("unknown adapter value must refuse to construct")


def t_production_flag_refuses_mock():
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = True
    try:
        cc.VastAdapter("http://example.invalid")
    except RuntimeError:
        return
    raise AssertionError("production flag must refuse VastAdapter")


def t_production_flag_refuses_none():
    cc.VAST_PRODUCTION_ENABLED = True
    try:
        cc.NoMarketplaceAdapter()
    except RuntimeError:
        return
    raise AssertionError("production flag must refuse NoMarketplaceAdapter")


# ------------------------------------------------------ none-adapter API --

def t_none_tells_truth():
    cc.VAST_PRODUCTION_ENABLED = False
    a = cc.NoMarketplaceAdapter()
    code, m = a.get("dgx01")
    assert code == 200, f"get status {code}"
    assert m["listed"] is False, "a nonexistent marketplace lists nothing"
    assert m["activeContracts"] == 0, "a nonexistent marketplace has no contracts"


def t_none_refuses_list():
    cc.VAST_PRODUCTION_ENABLED = False
    a = cc.NoMarketplaceAdapter()
    try:
        a.list_machine("dgx01", "tr-1")
    except RuntimeError:
        return
    raise AssertionError("list_machine must refuse without a marketplace")


def t_none_unlist_noop():
    cc.VAST_PRODUCTION_ENABLED = False
    a = cc.NoMarketplaceAdapter()
    code, resp = a.unlist_machine("dgx01")
    assert code == 200 and resp.get("noop") is True, "unlist must no-op OK"


# ------------------------------------------------- reconcile gate: VAST ---

def _fake_node(owner):
    return {"metadata": {"name": "node-a",
                         "labels": {cc.NODE_ID_LABEL: "dgx01",
                                    cc.OWNER_LABEL: owner}},
            "spec": {}}


def _cr(desired, phase=""):
    return {"metadata": {"name": "dgx01", "generation": 1},
            "spec": {"desiredOwner": desired, "transitionId": "tr-unit-1"},
            "status": {"phase": phase} if phase else {}}


def t_gate_desired_vast_fails_fast():
    cc.VAST_PRODUCTION_ENABLED = False
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE")
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = must_not_be_called("cordon")
    cc.update_taints = must_not_be_called("update_taints")
    cc.evict_pod = must_not_be_called("evict_pod")
    cc.set_owner_label = must_not_be_called("set_owner_label")
    cc.emit_event = lambda *a, **k: None
    cc.reconcile(_cr("VAST"), cc.NoMarketplaceAdapter(), {})
    assert len(patches) == 1, f"expected exactly one status patch, got {len(patches)}"
    conds = patches[0].get("conditions", [])
    assert conds and conds[0]["reason"] == "NoMarketplaceAdapter", \
        f"wrong condition: {conds}"


def t_gate_vast_state_held():
    cc.VAST_PRODUCTION_ENABLED = False
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("VAST")
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = must_not_be_called("cordon")
    cc.update_taints = must_not_be_called("update_taints")
    cc.set_owner_label = must_not_be_called("set_owner_label")
    cc.emit_event = lambda *a, **k: None
    # desired=ARISE: without the gate this would sail into the reclaim path
    # believing activeContracts=0 and sanitize a possibly-rented node.
    cc.reconcile(_cr("ARISE", phase="VAST_RENTED"),
                 cc.NoMarketplaceAdapter(), {})
    assert len(patches) == 1, f"expected exactly one status patch, got {len(patches)}"
    conds = patches[0].get("conditions", [])
    assert conds and conds[0]["reason"] == "VastStateWithoutMarketplace", \
        f"wrong condition: {conds}"


def t_mock_reconcile_path_unaffected():
    # With the mock adapter the gates must NOT trigger: an ARISE steady-state
    # node reconciles to READY exactly as before this change.
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE")
    cc.patch_status = lambda name, status: patches.append(status)
    cc.emit_event = lambda *a, **k: None
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("ARISE"), a, {})
    assert patches and patches[-1].get("phase") == "READY", \
        f"steady ARISE node should reach READY, got {patches}"


checks = [
    ("mock-v1 constructs", t_mock_constructs),
    ("unknown adapter refuses", t_unknown_refuses),
    ("production flag refuses VastAdapter", t_production_flag_refuses_mock),
    ("production flag refuses NoMarketplaceAdapter", t_production_flag_refuses_none),
    ("none: get() is truthful (unlisted, zero contracts)", t_none_tells_truth),
    ("none: list_machine refuses", t_none_refuses_list),
    ("none: unlist_machine no-ops", t_none_unlist_noop),
    ("none: desired=VAST fails fast, no mutation", t_gate_desired_vast_fails_fast),
    ("none: VAST-state node held, no mutation", t_gate_vast_state_held),
    ("mock: reconcile path unaffected by the gates", t_mock_reconcile_path_unaffected),
]

print(f"adapter-mode unit tests ({len(checks)}):")
for name, fn in checks:
    check(name, fn)

if FAILS:
    print(f"FAIL: {len(FAILS)}/{len(checks)}")
    sys.exit(1)
print(f"PASS: {len(checks)}/{len(checks)}")
