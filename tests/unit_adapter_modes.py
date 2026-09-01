#!/usr/bin/env python3
"""Unit tests for the capacity-controller adapter modes — no cluster needed.

Wired into scripts/validate.sh as an L0 gate. Covers:
  - mock-v1 constructs; an unknown adapter value refuses (VST-06's
    constructor leg, now testable without a cluster)
  - none constructs, tells the truth (unlisted / zero contracts), refuses
    list_machine, no-ops unlist_machine
  - the production flag refuses BOTH adapter classes
  - reconcile()'s no-marketplace gates: desired=VAST fails fast; recorded
    VAST tells (label / phase / listed / operationId) hold the node WITH
    isolation re-asserted; unstamped in-flight phases inherited from an
    earlier adapter regime are held; none-stamped in-flight phases are not;
    an explicit operator quarantine request bypasses the gates
  - the ownership volume gates: DIRECT handover and ARISE reclaim hold while
    tenant volumes are pinned to the node; tenant_pvs_on_node parses real
    local-path PV shapes

Each test runs against a fresh snapshot of the module's globals (stubs are
restored between tests), so tests are order-independent.
"""
import importlib.util
import time
import sys
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "services" / "capacity-controller" / "capacity_controller.py")

spec = importlib.util.spec_from_file_location("capacity_controller", SRC)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

# Snapshot every module global a test might stub, for restoration.
_STUBBABLE = ("VAST_ADAPTER", "VAST_PRODUCTION_ENABLED", "get_node_by_logical",
              "patch_status", "cordon", "update_taints", "evict_pod",
              "set_owner_label", "emit_event", "tenant_pvs_on_node", "api",
              "run_sanitization", "pods_on_node", "fake_gpu_allocated", "log",
              "TENANT_NAMESPACES", "CUSTOMER_TENANTS")
_ORIG = {k: getattr(cc, k) for k in _STUBBABLE}

FAILS = []


# Captured before any case monkeypatches the module. Most cases in this file
# replace module-level functions, so a case that needs the REAL one must take
# it from here — otherwise it silently tests whatever the previous case left
# behind (this bit twice: 2026-08-31 in unit_metering.py, 2026-09-01 here,
# when the sanitize case stubbed isolation_namespaces and the two union cases
# after it started asserting against the stub).
_REAL = {name: getattr(cc, name) for name in
         ("isolation_namespaces", "pods_on_node", "live_tenant_pods",
          "terminal_tenant_pods")}


def restore_real(*names):
    for n in names:
        setattr(cc, n, _REAL[n])


def check(name, fn):
    for k, v in _ORIG.items():
        setattr(cc, k, v)
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


def quiet():
    cc.log = lambda *a, **k: None
    cc.emit_event = lambda *a, **k: None


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
    try:
        cc.NoMarketplaceAdapter().list_machine("dgx01", "tr-1")
    except RuntimeError:
        return
    raise AssertionError("list_machine must refuse without a marketplace")


def t_none_unlist_noop():
    cc.VAST_PRODUCTION_ENABLED = False
    code, resp = cc.NoMarketplaceAdapter().unlist_machine("dgx01")
    assert code == 200 and resp.get("noop") is True, "unlist must no-op OK"


# ------------------------------------------------- reconcile: none gates --

def _fake_node(owner, cordoned=False, taints=()):
    return {"metadata": {"name": "node-a",
                         "labels": {cc.NODE_ID_LABEL: "dgx01",
                                    cc.OWNER_LABEL: owner}},
            "spec": {"unschedulable": cordoned,
                     "taints": [{"key": k} for k in taints]}}


def _cr(desired, phase="", status_extra=None):
    st = dict(status_extra or {})
    if phase:
        st["phase"] = phase
    return {"metadata": {"name": "dgx01", "generation": 1},
            "spec": {"desiredOwner": desired, "transitionId": "tr-unit-1"},
            "status": st}


def t_gate_desired_vast_fails_fast():
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE")
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = must_not_be_called("cordon")
    cc.update_taints = must_not_be_called("update_taints")
    cc.evict_pod = must_not_be_called("evict_pod")
    cc.set_owner_label = must_not_be_called("set_owner_label")
    cc.reconcile(_cr("VAST"), cc.NoMarketplaceAdapter(), {})
    assert len(patches) == 1, f"expected one status patch, got {len(patches)}"
    conds = patches[0].get("conditions", [])
    assert conds and conds[0]["reason"] == "NoMarketplaceAdapter", \
        f"wrong condition: {conds}"


def _expect_hold(cr, node, reason, want_cordon=True, want_taint=False):
    """Drive reconcile under none; assert a hold with isolation re-assert."""
    quiet()
    patches, cordons, taints = [], [], []
    cc.get_node_by_logical = lambda nid: node
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: cordons.append(v)
    cc.update_taints = lambda n, **k: taints.append(k)
    cc.evict_pod = must_not_be_called("evict_pod")
    cc.set_owner_label = must_not_be_called("set_owner_label")
    cc.run_sanitization = must_not_be_called("run_sanitization")
    cc.reconcile(cr, cc.NoMarketplaceAdapter(), {})
    assert len(patches) == 1, f"expected one status patch, got {len(patches)}"
    conds = patches[0].get("conditions", [])
    assert conds and conds[0]["reason"] == reason, f"wrong condition: {conds}"
    if want_cordon:
        assert cordons == [True], f"hold must re-assert cordon, got {cordons}"
    if want_taint:
        assert any("add" in t for t in taints), "hold must re-add VAST taint"


def t_gate_vast_label_held():
    cc.VAST_PRODUCTION_ENABLED = False
    # Uncordoned, taint stripped: the hold must restore BOTH (OWN-06 applies
    # to held nodes too — sanitizing nothing, isolating everything).
    _expect_hold(_cr("ARISE", phase="VAST_RENTED"), _fake_node("VAST"),
                 "VastStateWithoutMarketplace", want_cordon=True,
                 want_taint=True)


def t_gate_listed_residue_held():
    cc.VAST_PRODUCTION_ENABLED = False
    # QUARANTINED node whose best-effort unlist failed while listed.
    _expect_hold(_cr("ARISE", phase="QUARANTINED",
                     status_extra={"listed": True,
                                   "marketplaceAdapter": "none"}),
                 _fake_node("QUARANTINED"), "VastStateWithoutMarketplace")


def t_gate_operation_id_held():
    cc.VAST_PRODUCTION_ENABLED = False
    # A recorded operationId means a list once succeeded — never assume clean.
    _expect_hold(_cr("ARISE", phase="READY",
                     status_extra={"operationId": "op-123"}),
                 _fake_node("ARISE"), "VastStateWithoutMarketplace")


def t_gate_inherited_unstamped_held():
    cc.VAST_PRODUCTION_ENABLED = False
    # DRAINING written before the none regime (no stamp): the list crash
    # window means the machine may be listed with nothing recorded. Held.
    _expect_hold(_cr("ARISE", phase="DRAINING"),
                 _fake_node("ARISE", cordoned=True),
                 "InheritedTransitionState", want_cordon=False)


def t_gate_stamped_inflight_proceeds():
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    # Same DRAINING phase but stamped none: written under this regime, no
    # marketplace effect was possible — the legitimate day-0 DIRECT
    # reservation must keep flowing (here: drain completes, node handed over).
    patches, labels = [], []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE", cordoned=True)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: None
    cc.update_taints = lambda n, **k: None
    cc.set_owner_label = lambda n, o: labels.append(o)
    cc.pods_on_node = lambda n, ns=None: []
    cc.fake_gpu_allocated = lambda n: 0
    cc.tenant_pvs_on_node = lambda n, ns=None: []
    cc.reconcile(_cr("DIRECT", phase="DRAINING",
                     status_extra={"marketplaceAdapter": "none",
                                   "lastTransitionId": "tr-unit-1"}),
                 cc.NoMarketplaceAdapter(), {"dgx01:tr-unit-1": {"startedAt": 0}})
    assert labels == ["DIRECT"], f"expected DIRECT handover, got {labels}"
    assert patches and patches[-1].get("phase") == "DIRECT_ASSIGNED", \
        f"expected DIRECT_ASSIGNED, got {patches}"


def t_gate_quarantine_bypasses():
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    # desired=QUARANTINED is the operator's emergency isolation and must WIN
    # over the hold — it reads no marketplace fact and only isolates.
    patches, labels = [], []
    cc.get_node_by_logical = lambda nid: _fake_node("VAST")
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: None
    cc.update_taints = lambda n, **k: None
    cc.set_owner_label = lambda n, o: labels.append(o)
    cc.reconcile(_cr("QUARANTINED", phase="VAST_RENTED"),
                 cc.NoMarketplaceAdapter(), {})
    assert labels == ["QUARANTINED"], \
        f"operator quarantine must execute under none, got {labels}"
    assert patches and patches[-1].get("phase") == "QUARANTINED", \
        f"expected QUARANTINED phase, got {patches}"


def t_mock_reconcile_path_unaffected():
    # With the mock adapter the gates must NOT trigger: an ARISE steady-state
    # node reconciles to READY exactly as before this change.
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE")
    cc.patch_status = lambda name, status: patches.append(status)
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("ARISE"), a, {})
    assert patches and patches[-1].get("phase") == "READY", \
        f"steady ARISE node should reach READY, got {patches}"
    assert patches[-1].get("marketplaceAdapter") == "mock-v1", \
        "status must be stamped with the adapter mode"


# ------------------------------------------- DIRECT tenant resolution ------

def t_direct_tenant_explicit():
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-acme", "tenant-direct")
    cc.CUSTOMER_TENANTS = ("tenant-acme", "tenant-direct")
    who, why = cc.resolve_direct_tenant({"tenant": "tenant-acme"})
    assert who == "tenant-acme", (who, why)


def t_direct_tenant_inferred_when_single_customer():
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")
    cc.CUSTOMER_TENANTS = ("tenant-direct",)
    who, why = cc.resolve_direct_tenant({})
    assert who == "tenant-direct", (who, why)
    assert "inferred" in why, why


def t_direct_tenant_refuses_to_guess():
    """Two customers and no spec.tenant: the reservation names nobody.

    Guessing would drain one paying customer's pods off a node reserved for
    another — or leave them running on hardware someone else pays for.
    """
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-acme", "tenant-direct")
    cc.CUSTOMER_TENANTS = ("tenant-acme", "tenant-direct")
    who, why = cc.resolve_direct_tenant({})
    assert who is None, f"controller guessed {who!r} instead of holding"
    assert "names nobody" in why, why


def t_direct_tenant_rejects_unknown():
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")
    cc.CUSTOMER_TENANTS = ("tenant-direct",)
    who, why = cc.resolve_direct_tenant({"tenant": "tenant-ghost"})
    assert who is None and "not a registered CUSTOMER tenant" in why, (who, why)


def t_direct_drain_holds_when_ambiguous():
    """End to end: an ambiguous reservation must mutate NOTHING."""
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-acme", "tenant-direct")
    cc.CUSTOMER_TENANTS = ("tenant-acme", "tenant-direct")
    quiet()
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE", cordoned=True)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: None
    cc.update_taints = must_not_be_called("update_taints (ambiguous)")
    cc.set_owner_label = must_not_be_called("set_owner_label (ambiguous)")
    cc.evict_pod = must_not_be_called("evict_pod (ambiguous)")
    cc.pods_on_node = must_not_be_called("pods_on_node (ambiguous)")
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("DIRECT", phase="DRAINING",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 a, {"dgx01:tr-unit-1": {"startedAt": 0}})
    conds = patches[-1].get("conditions", [])
    assert conds and conds[0]["reason"] == "AmbiguousReservation", \
        f"expected an ambiguity hold, got {patches}"


# --------------------------------------------------- MAINTENANCE state -----

def t_maintenance_drains_all_tenants_and_settles():
    """Enter maintenance: drain EVERY tenant, keep cordon, swap to the
    maintenance taint, settle in the MAINTENANCE steady state. No volume gate:
    ownership does not change and the node comes back."""
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    patches, labels, taint_calls, drained_ns = [], [], [], []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE", cordoned=True)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: None
    cc.update_taints = lambda n, **k: taint_calls.append(k)
    cc.set_owner_label = lambda n, o: labels.append(o)
    cc.pods_on_node = lambda n, ns=None: drained_ns.append(ns) or []
    cc.tenant_pvs_on_node = must_not_be_called("tenant_pvs_on_node (maintenance)")
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("MAINTENANCE", phase="DRAINING",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 a, {"dgx01:tr-unit-1": {"startedAt": 0}})
    assert labels == ["MAINTENANCE"], labels
    assert patches and patches[-1].get("phase") == "MAINTENANCE", patches[-1]
    assert drained_ns and drained_ns[0] == cc.TENANT_NAMESPACES, \
        f"maintenance must drain EVERY tenant, got {drained_ns}"
    assert any("add" in c for c in taint_calls), "maintenance taint not added"


def t_maintenance_blocked_by_active_contract():
    """A rented machine finishes its contract before maintenance."""
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("VAST", cordoned=True)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = must_not_be_called("cordon")
    cc.update_taints = must_not_be_called("update_taints")
    cc.set_owner_label = must_not_be_called("set_owner_label")
    cc.evict_pod = must_not_be_called("evict_pod")
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 1,
                               "rentalEndAt": "2026-09-01T00:00:00Z"})
    cc.reconcile(_cr("MAINTENANCE", phase="VAST_RENTED",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 a, {})
    conds = patches[-1].get("conditions", [])
    assert conds and conds[0]["reason"] == "ActiveContracts", patches[-1]


def t_maintenance_steady_reasserts_isolation():
    """OWN-06 for maintenance: a stripped taint / lost cordon is restored."""
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    patches, cordons, taint_calls = [], [], []
    cc.get_node_by_logical = \
        lambda nid: _fake_node("MAINTENANCE", cordoned=False)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: cordons.append(v)
    cc.update_taints = lambda n, **k: taint_calls.append(k)
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("MAINTENANCE", phase="MAINTENANCE",
                     status_extra={"lastTransitionId": "tr-unit-1"}), a, {})
    assert cordons == [True], "lost cordon not re-asserted"
    assert any("add" in c for c in taint_calls), "stripped taint not restored"
    assert patches[-1].get("phase") == "MAINTENANCE", patches[-1]


def t_reclaim_ignores_internal_volumes():
    """Maintenance round-trip: internal (non-customer) volumes must NOT block
    the return to ARISE — the pool serves that tenant, and blocking made every
    maintenance a one-way door."""
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    cc.CUSTOMER_TENANTS = ("tenant-direct",)
    quiet()
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("MAINTENANCE", cordoned=True)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: None
    cc.update_taints = lambda n, **k: None
    # Real helper filtered by namespaces: internal volume only on this node.
    cc.tenant_pvs_on_node = \
        lambda n, ns=None: ["tenant-arise/scratch"] if (ns is None or "tenant-arise" in ns) else []
    cc.run_sanitization = lambda n, nid: [{"check": "x", "passed": True}]
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("ARISE", phase="MAINTENANCE",
                     status_extra={"lastTransitionId": "tr-unit-1"}), a, {})
    assert patches and patches[-1].get("phase") in ("SANITIZING", "HEALTH_CHECK"), \
        f"internal volume blocked a maintenance exit: {patches}"


# ------------------------------------------------- ownership volume gates --

def _lp_pv(name, node, sc, ns, claim):
    return {"metadata": {"name": name},
            "spec": {"storageClassName": sc,
                     "claimRef": {"namespace": ns, "name": claim},
                     "nodeAffinity": {"required": {"nodeSelectorTerms": [
                         {"matchExpressions": [
                             {"key": "kubernetes.io/hostname",
                              "operator": "In", "values": [node]}]}]}}}}


def t_pvs_helper_parses_local_path_shape():
    cc.api = lambda m, p, **k: {"items": [
        _lp_pv("pv-1", "node-a", "arise-shared", "tenant-arise", "scratch"),
        _lp_pv("pv-2", "node-a", "arise-longterm", "tenant-direct", "models"),
        _lp_pv("pv-3", "node-b", "arise-shared", "tenant-arise", "elsewhere"),
        _lp_pv("pv-4", "node-a", "some-other-class", "tenant-arise", "alien"),
    ]}
    got = cc.tenant_pvs_on_node("node-a")
    assert got == ["tenant-arise/scratch", "tenant-direct/models"], got
    got = cc.tenant_pvs_on_node("node-a", ("tenant-arise",))
    assert got == ["tenant-arise/scratch"], got


def t_direct_handover_blocked_by_stranded_volumes():
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE", cordoned=True)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: None
    cc.update_taints = must_not_be_called("update_taints (handover)")
    cc.set_owner_label = must_not_be_called("set_owner_label (handover)")
    cc.pods_on_node = lambda n, ns=None: []
    cc.fake_gpu_allocated = lambda n: 0
    cc.tenant_pvs_on_node = \
        lambda n, ns=None: ["tenant-arise/scratch"] if "tenant-arise" in (ns or ()) else []
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("DIRECT", phase="DRAINING",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 a, {"dgx01:tr-unit-1": {"startedAt": 0}})
    conds = patches[-1].get("conditions", [])
    assert conds and conds[0]["reason"] == "StrandedVolumes", \
        f"expected StrandedVolumes hold, got {patches}"
    assert patches[-1].get("phase") == "DRAINING", "must hold in DRAINING"


def t_reclaim_blocked_by_stranded_volumes():
    cc.VAST_ADAPTER = "mock-v1"
    cc.VAST_PRODUCTION_ENABLED = False
    quiet()
    patches = []
    cc.get_node_by_logical = lambda nid: _fake_node("DIRECT", cordoned=False)
    cc.patch_status = lambda name, status: patches.append(status)
    cc.cordon = lambda n, v: None
    cc.update_taints = lambda n, **k: None
    cc.run_sanitization = must_not_be_called("run_sanitization")
    cc.set_owner_label = must_not_be_called("set_owner_label")
    cc.tenant_pvs_on_node = lambda n, ns=None: ["tenant-direct/models"]
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": False, "activeContracts": 0,
                               "rentalEndAt": None})
    cc.reconcile(_cr("ARISE"), a, {})
    conds = patches[-1].get("conditions", [])
    assert conds and conds[0]["reason"] == "StrandedVolumes", \
        f"expected StrandedVolumes hold, got {patches}"


# ------------------------------------------------ review fixes 2026-08-27 --
def _mock_adapter(listed=False, active=0):
    cc.VAST_ADAPTER = "mock-v1"; cc.VAST_PRODUCTION_ENABLED = False
    a = cc.VastAdapter("http://example.invalid")
    a.get = lambda mid: (200, {"listed": listed, "activeContracts": active})
    a.unlist_machine = lambda mid: (200, {})
    return a


def _wire(owner, cordoned=False, taints=()):
    quiet()
    calls = {"patches": [], "labels": [], "cordon": [], "taints": []}
    cc.get_node_by_logical = lambda nid: _fake_node(owner, cordoned, taints)
    cc.patch_status = lambda name, status: calls["patches"].append(status)
    cc.cordon = lambda n, v: calls["cordon"].append(v)
    cc.update_taints = lambda n, **k: calls["taints"].append(k)
    cc.set_owner_label = lambda n, o: calls["labels"].append(o)
    cc.pods_on_node = lambda n, ns=None: []
    cc.fake_gpu_allocated = lambda n: 0
    cc.tenant_pvs_on_node = lambda n, ns=None: []
    return calls


def t_quarantine_label_drift_is_corrected():
    """P1-1: a hand-edited owner label must not launder a quarantine."""
    calls = _wire("ARISE")
    cc.reconcile(_cr("QUARANTINED", phase="QUARANTINED"), _mock_adapter(), {})
    assert calls["labels"] == ["QUARANTINED"], f"drift not corrected: {calls}"


def t_direct_converges_after_contract_ends():
    """P1-2: blocked-on-contract (VAST_RENTED) drains once the contract is over."""
    calls = _wire("VAST", cordoned=True)
    cc.reconcile(_cr("DIRECT", phase="VAST_RENTED",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 _mock_adapter(listed=False, active=0), {})
    assert calls["patches"] and calls["patches"][-1].get("phase") == "DRAINING", calls["patches"]


def t_maintenance_converges_after_contract_ends():
    calls = _wire("VAST", cordoned=True)
    cc.reconcile(_cr("MAINTENANCE", phase="VAST_RENTED",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 _mock_adapter(listed=False, active=0), {})
    assert calls["patches"] and calls["patches"][-1].get("phase") == "DRAINING", calls["patches"]


def t_quarantine_works_when_marketplace_unreachable():
    """P1-3: the emergency lever must not wait on adapter.get()."""
    calls = _wire("VAST")
    a = _mock_adapter()
    a.get = must_not_be_called("adapter.get")
    a.unlist_machine = lambda mid: (503, {})
    cc.reconcile(_cr("QUARANTINED", phase="VAST_RENTED"), a, {})
    assert calls["labels"] == ["QUARANTINED"], calls


def t_contract_on_non_vast_node_quarantines():
    """P2-7: an active contract on an ARISE-labelled node is dual ownership."""
    calls = _wire("ARISE")
    cc.reconcile(_cr("ARISE", phase="READY"), _mock_adapter(listed=True, active=1), {})
    assert calls["labels"] == ["QUARANTINED"], f"expected quarantine, got {calls}"


def t_ready_steady_state_opens_the_node():
    """P2-1: READY means schedulable with no arise.ai taint, every cycle."""
    calls = _wire("ARISE", cordoned=True, taints=(cc.TRANSITION_TAINT,))
    cc.reconcile(_cr("ARISE", phase="READY"), _mock_adapter(), {})
    assert calls["cordon"] == [False], f"expected uncordon, got {calls['cordon']}"
    assert any(cc.TRANSITION_TAINT in (k.get("remove") or []) for k in calls["taints"]), calls["taints"]


def t_direct_ambiguity_holds_before_cordon():
    """P2-3: a typo in spec.tenant must not cost the pool a cordoned node."""
    cc.CUSTOMER_TENANTS = ("tenant-direct", "tenant-acme")
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct", "tenant-acme")
    calls = _wire("ARISE")
    cc.cordon = must_not_be_called("cordon")
    cc.reconcile(_cr("DIRECT"), _mock_adapter(), {})
    conds = calls["patches"][-1].get("conditions", [])
    assert conds and conds[0]["reason"] == "AmbiguousReservation", calls["patches"]


def t_spec_tenant_must_be_customer():
    """P2-4: an internal namespace cannot be the target of a DIRECT sale."""
    who, why = cc.resolve_direct_tenant({"tenant": "tenant-arise"})
    assert who is None and "CUSTOMER" in why, (who, why)


def t_unbound_pv_counts_as_stranded():
    """P2-5: a Retain PV with claimRef cleared belongs to SOMEONE."""
    cc.api = lambda m, p, **k: {"items": [
        {"metadata": {"name": "pv-orphan"},
         "spec": {"storageClassName": "arise-longterm", "nodeAffinity": {"required": {"nodeSelectorTerms": [
             {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-a"]}]}]}}}}]}
    found = cc.tenant_pvs_on_node("node-a", ("tenant-arise",))
    assert found and "<unbound>" in found[0], found


def t_not_before_does_not_pause_steady_state():
    """P2-6: notBefore delays a START; a READY node keeps its drift enforcement."""
    calls = _wire("ARISE", cordoned=True)
    cr = _cr("ARISE", phase="READY", status_extra={"lastTransitionId": "tr-unit-1"})
    cr["spec"]["notBefore"] = "2999-01-01T00:00:00Z"
    cc.reconcile(cr, _mock_adapter(), {})       # CURRENT transition: enforcement stays on
    assert calls["cordon"] == [False], f"steady-state enforcement skipped: {calls}"
    # A NEW transition against a node resting in READY from its last one must
    # wait for notBefore (review 2026-08-27 P1-3: the phase-only test let a
    # scheduled maintenance cordon immediately).
    calls = _wire("ARISE")
    cc.cordon = must_not_be_called("cordon")
    cr = _cr("MAINTENANCE", phase="READY", status_extra={"lastTransitionId": "tr-old"})
    cr["spec"]["notBefore"] = "2999-01-01T00:00:00Z"
    cc.reconcile(cr, _mock_adapter(), {})
    # ...and the wait is not a drift holiday: a tampered label on the held
    # READY node is still corrected, and a hand-cordon still reopened.
    calls = _wire("VAST", cordoned=False)          # label tampered to VAST
    cr = _cr("MAINTENANCE", phase="READY", status_extra={"lastTransitionId": "tr-old"})
    cr["spec"]["notBefore"] = "2999-01-01T00:00:00Z"
    cc.reconcile(cr, _mock_adapter(), {})
    assert calls["labels"] == ["ARISE"], f"drift not corrected while gated: {calls}"
    calls = _wire("ARISE", cordoned=True)
    cr = _cr("MAINTENANCE", phase="READY", status_extra={"lastTransitionId": "tr-old"})
    cr["spec"]["notBefore"] = "2999-01-01T00:00:00Z"
    cc.reconcile(cr, _mock_adapter(), {})
    assert calls["cordon"] == [False] and not calls["patches"], f"gated READY must reopen and write nothing: {calls}"
    calls = _wire("ARISE")
    cc.cordon = must_not_be_called("cordon")
    cr = _cr("MAINTENANCE"); cr["spec"]["notBefore"] = "2999-01-01T00:00:00Z"
    cc.reconcile(cr, _mock_adapter(), {})


def t_vast_handover_clears_every_owner_taint():
    """P2-8: a DIRECT/MAINT node listed on VAST must not keep the old taint."""
    # The label flips to VAST at the END of the drain (list + readback), so
    # the handover shape is DRAINING with an empty node and a listing that
    # takes: that is where the old DIRECT taint used to survive.
    calls = _wire("DIRECT", cordoned=True, taints=(cc.DIRECT_TAINT, cc.TRANSITION_TAINT))
    def _ready_node(nid):                      # pre-list gate needs Ready=True
        n = _fake_node("DIRECT", True, (cc.DIRECT_TAINT, cc.TRANSITION_TAINT))
        n["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
        return n
    cc.get_node_by_logical = _ready_node
    a = _mock_adapter(listed=True, active=0)
    a.list_machine = lambda mid, **k: (200, {"operationId": "op-unit-1"})
    cc.reconcile(_cr("VAST", phase="DRAINING", status_extra={"lastTransitionId": "tr-unit-1"}),
                 a, {"dgx01:tr-unit-1": {"startedAt": 0}})
    assert calls["labels"] == ["VAST"], f"expected VAST handover, got {calls}"
    removed = [k.get("remove") or [] for k in calls["taints"]]
    assert any(cc.DIRECT_TAINT in r for r in removed), f"DIRECT taint kept: {calls['taints']}"


def t_metrics_know_maintenance():
    with cc._metrics_lock:
        cc._metrics["owner"]["dgx09"] = "MAINTENANCE"
    out = cc.render_metrics()
    assert 'owner="MAINTENANCE"' in out and 'node="dgx09"' in out, out[:400]


# --------------- gaps the falsification audit found (2026-08-30) -----------
# Three mechanisms with real code and no detector at all.

def t_mutating_calls_are_never_blind_retried():
    """A GET may be repeated; a POST whose outcome is UNKNOWN may not — that is
    how a marketplace listing becomes two listings. The retry budget must apply
    to safe methods only."""
    import urllib.error, urllib.request
    calls = {"GET": 0, "POST": 0}

    class FakeResp:
        pass

    def fake_urlopen(req, context=None, timeout=None):
        calls[req.get_method()] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        get = urllib.request.Request("http://example.invalid/x", method="GET")
        try:
            cc._open_with_retry(get)
        except urllib.error.HTTPError:
            pass
        post = urllib.request.Request("http://example.invalid/x", method="POST", data=b"{}")
        try:
            cc._open_with_retry(post)
        except urllib.error.HTTPError:
            pass
    finally:
        urllib.request.urlopen = real
    assert calls["GET"] == cc.RETRY_BUDGET, f"safe method must use the whole budget, got {calls['GET']}"
    assert calls["POST"] == 1, f"a mutating call must be attempted exactly once, got {calls['POST']}"


def t_unknown_contract_state_holds_position():
    """An adapter that cannot answer is not an adapter answering "zero": the
    node must be left exactly as it is, with the condition recorded."""
    calls = _wire("VAST", cordoned=True)
    cc.cordon = must_not_be_called("cordon")
    cc.update_taints = must_not_be_called("update_taints")
    cc.set_owner_label = must_not_be_called("set_owner_label")
    cc.evict_pod = must_not_be_called("evict_pod")
    a = _mock_adapter()

    def boom(mid):
        raise TimeoutError("marketplace unreachable")

    a.get = boom
    cc.reconcile(_cr("ARISE", phase="VAST_RENTED", status_extra={"lastTransitionId": "tr-unit-1"}), a, {})
    conds = (calls["patches"][-1] if calls["patches"] else {}).get("conditions", [])
    assert conds and conds[0]["type"] == "ContractStateKnown" and conds[0]["status"] == "False", calls["patches"]
    assert "activeContracts" not in (calls["patches"][-1] or {}), \
        "unknown contract state must not be written as a fact"


def t_direct_node_stays_uncordoned():
    """A cordoned DIRECT node denies the customer the capacity they are paying
    for — as much a breach as letting someone else onto it."""
    calls = _wire("DIRECT", cordoned=True, taints=(cc.DIRECT_TAINT,))
    cc.reconcile(_cr("DIRECT", phase="DIRECT_ASSIGNED",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 _mock_adapter(), {})
    assert calls["cordon"] == [False], f"a cordoned DIRECT node must be reopened, got {calls['cordon']}"
    assert calls["patches"] and calls["patches"][-1].get("phase") == "DIRECT_ASSIGNED", calls["patches"]
    # ...and the taint it needs is (re)asserted in the same pass
    assert any(cc.DIRECT_TAINT in [x.get("key") for x in (k.get("add") or [])] for k in calls["taints"]), \
        f"the DIRECT taint must be re-asserted, got {calls['taints']}"


def t_cordon_and_taint_precede_eviction():
    """Ordering, not just occurrence: if a pod is evicted BEFORE the node is
    cordoned and tainted, the scheduler can put another pod straight back onto
    a machine that is being handed to someone else. Nothing asserted the
    order (audit 2026-08-30)."""
    quiet()
    order = []
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE")
    cc.patch_status = lambda name, status: order.append(f"status:{status.get('phase','')}")
    cc.cordon = lambda n, v: order.append(f"cordon:{v}")
    cc.update_taints = lambda n, **k: order.append("taint:" + ",".join(
        [x.get("key", "") for x in (k.get("add") or [])] or ["-"]))
    cc.set_owner_label = lambda n, o: order.append(f"label:{o}")
    cc.evict_pod = lambda ns, pod: order.append(f"evict:{pod}")
    cc.pods_on_node = lambda n, ns=None: [{"metadata": {"name": "victim", "namespace": "tenant-arise"}}]
    cc.fake_gpu_allocated = lambda n: 0
    cc.tenant_pvs_on_node = lambda n, ns=None: []
    cc.reconcile(_cr("VAST", status_extra={}), _mock_adapter(), {})
    # first pass: cordon + taint, no eviction yet
    assert any(o.startswith("cordon:True") for o in order), f"no cordon on the first pass: {order}"
    assert any(o.startswith("taint:") for o in order), f"no taint on the first pass: {order}"
    assert not any(o.startswith("evict:") for o in order), \
        f"a pod was evicted before the node was fenced: {order}"
    ci = next(i for i, o in enumerate(order) if o.startswith("cordon:True"))
    ti = next(i for i, o in enumerate(order) if o.startswith("taint:"))
    si = next(i for i, o in enumerate(order) if o.startswith("status:DRAINING"))
    assert ci < si and ti < si, f"the node must be fenced before DRAINING is recorded: {order}"


def t_sanitization_failure_quarantines():
    """The cleanup gate has to BLOCK, not just leave a record. Every existing
    detector asserted that sanitizationResults exists and names a check —
    nothing ever made a check FAIL and watched what happens (audit
    2026-08-30). A node whose tenant pods are still running must be
    quarantined, never uncordoned back into the pool."""
    calls = _wire("VAST", cordoned=True, taints=(cc.VAST_TAINT,))
    # one tenant pod refuses to die: customer_workloads_stopped -> passed False
    cc.pods_on_node = lambda n, ns=None: [{"metadata": {"name": "stuck", "namespace": "tenant-arise"}}]
    cc.fake_gpu_allocated = lambda n: 0
    cc.tenant_pvs_on_node = lambda n, ns=None: []
    cc.evict_pod = lambda ns, pod: None
    cc.reconcile(_cr("ARISE", phase="SANITIZING",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 _mock_adapter(listed=False, active=0), {"dgx01:tr-unit-1": {"startedAt": 0}})
    assert calls["labels"] == ["QUARANTINED"], \
        f"a failed sanitization must quarantine, got {calls}"
    assert not any(v is False for v in calls["cordon"]), \
        f"a node that failed sanitization must NOT be uncordoned: {calls['cordon']}"
    last = calls["patches"][-1] if calls["patches"] else {}
    assert last.get("phase") != "READY", f"it must never reach READY: {last}"


def t_sanitization_records_what_is_simulated():
    """Two of these checks are hardcoded True in Phase A (real NVMe erase is
    HW-12). That is a legitimate state, but it must be VISIBLE in the record
    an operator signs off — a check that always passes and does not say so is
    indistinguishable from one that verified something.

    terminal_pods_left_behind is a third always-true check, and deliberately
    so: it REPORTS finished pod objects rather than blocking on them (a
    customer job that succeeded must not quarantine the node — 2026-09-01).
    It is CONTROL-PLANE, not SIMULATED: it really did look, and it names what
    it found."""
    restore_real("isolation_namespaces", "live_tenant_pods", "terminal_tenant_pods")
    cc.pods_on_node = lambda n, ns=None: []
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")
    cc.api = lambda m, p, **k: {"items": []}
    cc._ns_cache.update(at=0.0, names=())
    cc.fake_gpu_allocated = lambda n: 0
    results = cc.run_sanitization("node-a", "dgx01")
    by = {r["check"]: r for r in results}
    assert set(by) == {"customer_workloads_stopped", "terminal_pods_left_behind",
                       "fake_gpu_released", "data_erasure", "health_score"}, sorted(by)
    for name in ("data_erasure", "health_score"):
        assert by[name]["kind"] == "SIMULATED", f"{name} must be marked SIMULATED: {by[name]}"
        assert "SIMULATED" in by[name]["detail"].upper(), by[name]
    for name in ("customer_workloads_stopped", "terminal_pods_left_behind",
                 "fake_gpu_released"):
        assert by[name]["kind"] == "CONTROL-PLANE", by[name]
    # the reporting check must never be the thing that blocks a handover
    assert by["terminal_pods_left_behind"]["passed"] is True


def t_taint_update_is_compare_and_set():
    """A taint list written from a stale read erases whatever another writer
    added in between (kubelet condition taints, the GPU Operator's
    nvidia.com/gpu during driver install). The write must carry the
    resourceVersion it read, and a 409 must send it back for a fresh read."""
    import urllib.error
    reads, writes = [], []
    state = {"rv": "100", "taints": [{"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"}]}

    def fake_api(method, path, body=None, content_type="application/json"):
        if method == "GET":
            reads.append(state["rv"])
            return {"metadata": {"resourceVersion": state["rv"]},
                    "spec": {"taints": list(state["taints"])}}
        writes.append(body)
        sent_rv = (body.get("metadata") or {}).get("resourceVersion")
        assert sent_rv is not None, "the write must carry a resourceVersion precondition"
        if sent_rv != state["rv"]:
            raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
        # somebody else wins the first race
        if len(writes) == 1:
            state["rv"] = "101"
            raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
        state["taints"] = body["spec"]["taints"]
        return {}

    cc.api = fake_api
    cc.update_taints("node-a", add=[cc.taint(cc.VAST_TAINT, "true")])
    assert len(reads) == 2, f"a 409 must trigger a fresh read, reads={reads}"
    keys = [t["key"] for t in state["taints"]]
    assert cc.VAST_TAINT in keys, f"the taint we asked for is missing: {keys}"
    assert "nvidia.com/gpu" in keys, \
        f"a taint written by someone else was erased: {keys}"


def _draining(evict_result, phase="DRAINING", desired="VAST", age=100000):
    """A node stuck in DRAINING with one live tenant pod, well past the
    deadline. `evict_result` decides WHY it is stuck."""
    quiet()
    calls = {"labels": [], "patches": [], "cordon": [], "events": []}
    cc.get_node_by_logical = lambda nid: _fake_node("ARISE", cordoned=True,
                                                    taints=(cc.TRANSITION_TAINT,))
    cc.patch_status = lambda n, s: calls["patches"].append(s)
    cc.set_owner_label = lambda n, o: calls["labels"].append(o)
    cc.cordon = lambda n, v: calls["cordon"].append(v)
    cc.update_taints = lambda n, **k: None
    cc.emit_event = lambda cr, reason, msg, etype="Normal": \
        calls["events"].append((reason, msg))
    cc.evict_pod = lambda pod: evict_result
    cc.pods_on_node = lambda n, ns=None: [
        {"metadata": {"name": "sigterm-ignorer", "namespace": "tenant-arise"},
         "status": {"phase": "Running"}}]
    cc.fake_gpu_allocated = lambda n: 0
    cc.tenant_pvs_on_node = lambda n, ns=None: []
    cr = {"metadata": {"name": "dgx01", "generation": 1},
          "spec": {"desiredOwner": desired, "transitionId": "tr-hang"},
          "status": {"phase": phase, "lastTransitionId": "tr-hang"}}
    cc.reconcile(cr, _mock_adapter(), {"dgx01:tr-hang": {"startedAt": time.time() - age}})
    return calls


def t_drain_deadline_is_on_the_outcome():
    """An eviction the API server ACCEPTS but which never completes must hit
    the same deadline as one it refuses.

    Until 2026-08-31 all three drain sites read
    `if blocked and elapsed > DRAIN_TIMEOUT`, so the deadline only existed for
    REFUSED evictions. A pod with a long terminationGracePeriodSeconds and a
    container that ignores SIGTERM — or a finalizer, or a stuck CSI unmount —
    returned (True, "evicted") on every pass and left the node in DRAINING
    forever: cordoned and tainted out of the sellable fleet, no event, no
    quarantine, no alert. Reproduced at 833x the deadline: five reconciles,
    five identical "1 tenant pod(s) remain" statuses, nothing else."""
    calls = _draining((True, "evicted"))
    assert calls["labels"] == ["QUARANTINED"], \
        f"an accepted-but-ineffective eviction must still hit the deadline: {calls}"
    last = calls["patches"][-1] if calls["patches"] else {}
    assert last.get("phase") == "QUARANTINED", f"still {last.get('phase')}"
    reason, msg = calls["events"][-1]
    assert reason == "DrainBlocked", reason
    # the message has to name the pod and say WHICH failure this is, because
    # "blocked by a PDB" and "accepted but never died" need different fixes
    assert "tenant-arise/sigterm-ignorer" in msg, msg
    assert "ACCEPTED" in msg and "terminationGracePeriodSeconds" in msg, msg


def t_drain_deadline_still_reports_a_refusal():
    """The other direction: a PDB rejection must keep its own diagnosis, not
    be flattened into the new message."""
    calls = _draining((False, "429:disruption budget"))
    assert calls["labels"] == ["QUARANTINED"], calls
    _reason, msg = calls["events"][-1]
    assert "eviction blocked past timeout" in msg and "429" in msg, msg


def t_drain_inside_the_deadline_keeps_waiting():
    """The deadline must not become a hair trigger: a pod still inside its
    grace period is a normal drain, not a quarantine."""
    calls = _draining((True, "evicted"), age=5)
    assert calls["labels"] == [], f"quarantined a healthy drain: {calls}"
    assert calls["patches"][-1].get("phase") == "DRAINING", calls["patches"][-1]


def t_transition_age_is_published():
    """A hang has to be visible from OUTSIDE the controller. Every other
    series reads healthy while a node sits mid-transition: owner sums to
    exactly 1, contracts are 0. Without this gauge NodeTransitionStuck has
    nothing to fire on (audit 2026-08-31)."""
    with cc._metrics_lock:
        cc._metrics["transition"].clear()
    _draining((True, "evicted"), age=4000)
    # QUARANTINED is a settled phase: the series must be GONE, not frozen at
    # its last value, or the alert would keep firing for a node nobody is
    # transitioning any more.
    assert "dgx01" not in cc._metrics["transition"], \
        f"a settled node still publishes a transition age: {cc._metrics['transition']}"
    _draining((True, "evicted"), age=4000, phase="SANITIZING")
    text = cc.render_metrics()
    line = next((l for l in text.splitlines()
                 if l.startswith("arise_node_transition_seconds{")), "")
    assert 'node="dgx01"' in line and 'phase="SANITIZING"' in line, repr(line)
    assert float(line.rsplit(" ", 1)[1]) > 3900, line
    with cc._metrics_lock:
        cc._metrics["transition"].clear()


def t_isolation_set_is_a_union():
    """A tenant onboarded after this process started must still be drained.

    TENANT_NAMESPACES is a hardcoded fallback that an unreadable register
    leaves in place, so before 2026-08-31 a third tenant's pods could stay on
    a node being handed to the marketplace — dual tenancy on sold hardware.
    The set is now the UNION of the register and the namespaces LABELLED
    arise.ai/tier=tenant, because the two directions are not symmetric:
    missing one is catastrophic, an extra one just evicts pods from a node
    already leaving service."""
    quiet()
    restore_real("isolation_namespaces")
    cc._ns_cache.update(at=0.0, names=())
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")
    cc.api = lambda m, p, **k: {"items": [
        {"metadata": {"name": "tenant-acme"}},
        {"metadata": {"name": "tenant-arise"}}]}
    got = cc.isolation_namespaces()
    assert got == ("tenant-acme", "tenant-arise", "tenant-direct"), got

    # the API failing must NARROW nothing: the register still counts
    cc._ns_cache.update(at=0.0, names=())

    def boom(*a, **k):
        raise RuntimeError("apiserver unreachable")

    cc.api = boom
    got = cc.isolation_namespaces()
    assert got == ("tenant-arise", "tenant-direct"), got

    # ...and with NEITHER source it must RAISE, not return empty:
    # pods_on_node(node, ()) filters against an empty set and reports no pods,
    # so an empty answer would tell the drain a node holding a customer's work
    # is clean. Fail closed — the reconcile aborts with the node still
    # cordoned.
    cc._ns_cache.update(at=0.0, names=())
    cc.TENANT_NAMESPACES = ()
    cc.api = lambda m, p, **k: {"items": []}
    try:
        got = cc.isolation_namespaces()
        raise AssertionError(f"returned {got!r} instead of refusing")
    except RuntimeError as exc:
        assert "cannot enumerate tenant namespaces" in str(exc), exc
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")
    cc._ns_cache.update(at=0.0, names=())


def t_drain_covers_a_tenant_the_process_never_heard_of():
    """The property, at the call site: a pod in a namespace that exists only
    as a cluster label must block the handover."""
    calls = _wire("ARISE")
    restore_real("isolation_namespaces", "live_tenant_pods")
    cc._ns_cache.update(at=0.0, names=())
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")
    asked = []

    def fake_api(m, p, **k):
        if "/namespaces?" in p:
            return {"items": [{"metadata": {"name": "tenant-acme"}}]}
        return {"items": []}

    cc.api = fake_api
    cc.pods_on_node = lambda n, ns=None: (
        asked.append(tuple(ns or ())) or
        [{"metadata": {"name": "acme-pod", "namespace": "tenant-acme"},
          "status": {"phase": "Running"}}])
    cc.evict_pod = lambda pod: (True, "evicted")
    cc.reconcile(_cr("VAST", phase="DRAINING",
                     status_extra={"lastTransitionId": "tr-unit-1"}),
                 _mock_adapter(), {"dgx01:tr-unit-1": {"startedAt": time.time()}})
    assert asked and "tenant-acme" in asked[0], \
        f"the drain never asked about the onboarded tenant: {asked}"
    last = calls["patches"][-1] if calls["patches"] else {}
    assert last.get("phase") == "DRAINING", \
        f"a pod in the new tenant did not hold the handover: {last}"
    cc._ns_cache.update(at=0.0, names=())


def _reclaim(pods, phase="DRAINING"):
    """Drive the VAST -> ARISE reclaim path with a given set of pods on the node."""
    quiet()
    calls = {"labels": [], "patches": [], "events": []}
    cc.get_node_by_logical = lambda nid: {
        "metadata": {"name": "kn", "labels": {"arise.ai/owner": "VAST"}},
        "spec": {"unschedulable": True, "taints": [{"key": cc.VAST_TAINT}]}}
    cc.patch_status = lambda n, s: calls["patches"].append(s)
    cc.set_owner_label = lambda n, o: calls["labels"].append(o)
    cc.cordon = lambda n, v: None
    cc.update_taints = lambda n, **k: None
    cc.emit_event = lambda cr, r, m, etype="Normal": calls["events"].append((r, m))
    cc.isolation_namespaces = lambda: ("tenant-arise", "tenant-direct")
    cc.pods_on_node = lambda n, ns=None: list(pods)
    cc.fake_gpu_allocated = lambda n: 0
    cc.tenant_pvs_on_node = lambda n, ns=None: []
    cc.reconcile(_cr("ARISE", phase=phase, status_extra={"lastTransitionId": "tr-unit-1"}),
                 _mock_adapter(), {"dgx01:tr-unit-1": {"startedAt": time.time() - 1}})
    return calls


def _pod(name, phase, ns="tenant-direct"):
    return {"metadata": {"name": name, "namespace": ns}, "status": {"phase": phase}}


def t_finished_job_does_not_quarantine():
    """A customer job that SUCCEEDS must not take the node out of the fleet.

    Three places used to disagree about "is a customer workload still here":
    the drain excluded Succeeded/Failed pods, while run_sanitization() and
    pre_list_checks() counted every pod object. So a training run that
    finished normally left a Succeeded pod, the drain declared the node clean,
    and the very next gate quarantined it for customer_workloads_stopped —
    the HAPPY PATH ending in a state only a human could clear (reproduced
    2026-09-01). They now share live_tenant_pods()."""
    calls = _reclaim([_pod("training-run-1", "Succeeded")])
    assert calls["labels"] == [], f"a finished job quarantined the node: {calls}"
    assert [p.get("phase") for p in calls["patches"]][-1] == "HEALTH_CHECK", \
        f"reclaim did not proceed: {[p.get('phase') for p in calls['patches']]}"
    # ...and the leftover is REPORTED, not silently dropped
    res = [p for p in calls["patches"] if "sanitizationResults" in p][-1]["sanitizationResults"]
    left = next(c for c in res if c["check"] == "terminal_pods_left_behind")
    assert left["passed"] and "training-run-1" in left["detail"], left


def t_running_pod_still_blocks_the_reclaim():
    """The other direction, or the fix above would just be a hole: a pod that
    is actually RUNNING must still stop the node going back to the pool."""
    calls = _reclaim([_pod("still-going", "Running")])
    assert calls["labels"] == ["QUARANTINED"], \
        f"a running customer pod did not block the reclaim: {calls}"
    reason = calls["events"][-1][0] if calls["events"] else ""
    assert reason == "SanitizationFailed", reason


def t_failed_job_also_does_not_quarantine():
    """Failed is terminal too — a crashed customer job holds no resources."""
    calls = _reclaim([_pod("crashed-run", "Failed")])
    assert calls["labels"] == [], f"a failed job quarantined the node: {calls}"


def t_pre_list_gate_uses_the_same_definition():
    """The gate before LISTING a node on the marketplace must agree with the
    drain and the sanitize gate about what "a tenant workload is still here"
    means. It counted every pod object too, so a node whose customer job had
    SUCCEEDED could not be listed — the same happy-path break, on the selling
    side (2026-09-01). Three gates, one definition: live_tenant_pods()."""
    restore_real("isolation_namespaces", "live_tenant_pods", "terminal_tenant_pods")
    cc.TENANT_NAMESPACES = ("tenant-arise", "tenant-direct")
    cc._ns_cache.update(at=0.0, names=())
    cc.api = lambda m, p, **k: {"items": []}
    cc.fake_gpu_allocated = lambda n: 0
    cc.get_node_by_logical = lambda nid: {
        "metadata": {"name": "kn"},
        "status": {"conditions": [{"type": "Ready", "status": "True"},
                                  {"type": "DiskPressure", "status": "False"}]}}

    cc.pods_on_node = lambda n, ns=None: [_pod("finished-run", "Succeeded")]
    by = {c["check"]: c for c in cc.run_pre_list_checks("kn", "dgx01")}
    assert by["no_tenant_pods"]["passed"], \
        f"a finished job blocked the listing: {by['no_tenant_pods']}"

    cc.pods_on_node = lambda n, ns=None: [_pod("still-going", "Running")]
    by = {c["check"]: c for c in cc.run_pre_list_checks("kn", "dgx01")}
    assert not by["no_tenant_pods"]["passed"], \
        "a RUNNING customer pod must still block listing the node for sale"
    cc._ns_cache.update(at=0.0, names=())


checks = [
    ("mock-v1 constructs", t_mock_constructs),
    ("unknown adapter refuses", t_unknown_refuses),
    ("production flag refuses VastAdapter", t_production_flag_refuses_mock),
    ("production flag refuses NoMarketplaceAdapter", t_production_flag_refuses_none),
    ("none: get() is truthful (unlisted, zero contracts)", t_none_tells_truth),
    ("none: list_machine refuses", t_none_refuses_list),
    ("none: unlist_machine no-ops", t_none_unlist_noop),
    ("none: desired=VAST fails fast, no mutation", t_gate_desired_vast_fails_fast),
    ("none: VAST-label node held + isolation re-asserted", t_gate_vast_label_held),
    ("none: listed-residue (quarantine) node held", t_gate_listed_residue_held),
    ("none: recorded operationId held", t_gate_operation_id_held),
    ("none: unstamped in-flight phase held (inherited)", t_gate_inherited_unstamped_held),
    ("none: none-stamped in-flight DIRECT drain proceeds", t_gate_stamped_inflight_proceeds),
    ("none: operator quarantine bypasses the hold", t_gate_quarantine_bypasses),
    ("mock: reconcile path unaffected + status stamped", t_mock_reconcile_path_unaffected),
    ("DIRECT tenant: explicit spec.tenant wins", t_direct_tenant_explicit),
    ("DIRECT tenant: inferred with one customer", t_direct_tenant_inferred_when_single_customer),
    ("DIRECT tenant: refuses to guess with two", t_direct_tenant_refuses_to_guess),
    ("DIRECT tenant: unknown namespace rejected", t_direct_tenant_rejects_unknown),
    ("DIRECT drain holds (no mutation) when ambiguous", t_direct_drain_holds_when_ambiguous),
    ("maintenance: drains all tenants, settles, no volume gate", t_maintenance_drains_all_tenants_and_settles),
    ("maintenance: blocked by an active contract", t_maintenance_blocked_by_active_contract),
    ("maintenance: steady state re-asserts isolation", t_maintenance_steady_reasserts_isolation),
    ("reclaim: internal volumes do NOT block (round-trip)", t_reclaim_ignores_internal_volumes),
    ("volume gate: helper parses local-path PV shape", t_pvs_helper_parses_local_path_shape),
    ("volume gate: DIRECT handover holds on tenant-arise PV", t_direct_handover_blocked_by_stranded_volumes),
    ("volume gate: ARISE reclaim holds on tenant PV", t_reclaim_blocked_by_stranded_volumes),
    ("review: quarantine label drift corrected", t_quarantine_label_drift_is_corrected),
    ("review: DIRECT converges after contract ends", t_direct_converges_after_contract_ends),
    ("review: MAINTENANCE converges after contract ends", t_maintenance_converges_after_contract_ends),
    ("review: quarantine works with marketplace unreachable", t_quarantine_works_when_marketplace_unreachable),
    ("review: contract on non-VAST node quarantines", t_contract_on_non_vast_node_quarantines),
    ("review: READY steady state opens the node", t_ready_steady_state_opens_the_node),
    ("review: DIRECT ambiguity holds BEFORE cordon", t_direct_ambiguity_holds_before_cordon),
    ("review: spec.tenant must be a customer", t_spec_tenant_must_be_customer),
    ("review: unbound PV counts as stranded", t_unbound_pv_counts_as_stranded),
    ("review: notBefore only gates the start", t_not_before_does_not_pause_steady_state),
    ("review: VAST handover clears DIRECT/MAINT taints", t_vast_handover_clears_every_owner_taint),
    ("review: metrics know MAINTENANCE", t_metrics_know_maintenance),
    ("audit: mutating HTTP calls are never blind-retried", t_mutating_calls_are_never_blind_retried),
    ("audit: unknown contract state holds position", t_unknown_contract_state_holds_position),
    ("audit: a DIRECT node is never left cordoned", t_direct_node_stays_uncordoned),
    ("audit: cordon+taint happen BEFORE any eviction", t_cordon_and_taint_precede_eviction),
    ("audit: a FAILED sanitization quarantines (the gate blocks)", t_sanitization_failure_quarantines),
    ("audit: simulated sanitize checks are marked SIMULATED", t_sanitization_records_what_is_simulated),
    ("audit: taint updates are compare-and-set (no clobbering)", t_taint_update_is_compare_and_set),
    ("audit: drain deadline fires on pods remaining, not on refusals", t_drain_deadline_is_on_the_outcome),
    ("audit: a refused eviction keeps its own diagnosis", t_drain_deadline_still_reports_a_refusal),
    ("audit: a drain inside the deadline is not quarantined", t_drain_inside_the_deadline_keeps_waiting),
    ("audit: transition age is published and cleared", t_transition_age_is_published),
    ("audit: the isolation namespace set is a union, never a replacement", t_isolation_set_is_a_union),
    ("audit: a drain covers a tenant onboarded after startup", t_drain_covers_a_tenant_the_process_never_heard_of),
    ("audit: a job that FINISHED does not quarantine the node", t_finished_job_does_not_quarantine),
    ("audit: a RUNNING pod still blocks the reclaim", t_running_pod_still_blocks_the_reclaim),
    ("audit: a FAILED job does not quarantine the node", t_failed_job_also_does_not_quarantine),
    ("audit: the pre-list gate shares the live-pod definition", t_pre_list_gate_uses_the_same_definition),
]

print(f"adapter-mode unit tests ({len(checks)}):")
for name, fn in checks:
    check(name, fn)

if FAILS:
    print(f"FAIL: {len(FAILS)}/{len(checks)}")
    sys.exit(1)
print(f"PASS: {len(checks)}/{len(checks)}")
