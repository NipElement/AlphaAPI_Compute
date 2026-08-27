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
    assert who is None and "not a registered tenant" in why, (who, why)


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
]

print(f"adapter-mode unit tests ({len(checks)}):")
for name, fn in checks:
    check(name, fn)

if FAILS:
    print(f"FAIL: {len(FAILS)}/{len(checks)}")
    sys.exit(1)
print(f"PASS: {len(checks)}/{len(checks)}")
