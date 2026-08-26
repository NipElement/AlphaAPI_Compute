#!/usr/bin/env bash
# ============================================================================
# DGX overlay static render gate — no cluster, no docker (kubectl binary only,
# used purely as a kustomize renderer).
#
# This is the promotion-side twin of validate.sh's lab checks: it renders
# platform/overlays/dgx and asserts the properties that make the overlay safe
# to apply on hardware. Run via `make dgx-render`. Every FAIL here is a real
# "do not apply this on the machine" condition.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

command -v kubectl >/dev/null 2>&1 || {
  echo "kubectl missing — run 'make tools' first"; exit 1; }

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
kubectl kustomize platform/overlays/dgx > "$TMP" || {
  echo "FAIL: dgx overlay does not render"; exit 1; }

python3 - "$TMP" <<'PY'
import sys, yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
fails = []


def every(pred, what):
    bad = [f'{d["kind"]} {d["metadata"].get("namespace","")}/{d["metadata"]["name"]}'
           for d in docs if not pred(d)]
    if bad:
        fails.append(f"{what}: " + "; ".join(bad))


# 1. No simulation residue: nothing named for, and no references to, the
#    lab's fake resources — except the policy that DENIES them.
for d in docs:
    nm = d["metadata"].get("name", "")
    ns = d["metadata"].get("namespace", "")
    if ns == "vast-mock" or nm.startswith("vast-mock") or "fake-gpu" in nm:
        fails.append(f"lab residue object: {d['kind']} {ns}/{nm}")
    if "arise.dev/" in yaml.dump(d) and nm != "arise-deny-simulated-gpu":
        fails.append(f"simulation-resource reference: {d['kind']} {ns}/{nm}")

# 2. The deny policy itself must be present (two-mechanism rule, plan §8.2).
if not any(d["metadata"].get("name") == "arise-deny-simulated-gpu"
           and d["kind"] == "ValidatingAdmissionPolicy" for d in docs):
    fails.append("arise-deny-simulated-gpu policy missing")

# 3. No hostPath anywhere: the docker-cp/hostPath SPA delivery is a kind-ism
#    and no dgx workload may depend on one node's filesystem.
for d in docs:
    if "hostPath" in yaml.dump(d):
        fails.append(f"hostPath volume: {d['kind']} "
                     f"{d['metadata'].get('namespace','')}/{d['metadata']['name']}")

# 4. Image hygiene: no unresolved placeholders, no :latest.
for d in docs:
    for line in yaml.dump(d).splitlines():
        if "image:" in line:
            img = line.split("image:", 1)[1].strip()
            if "PLACEHOLDER" in img:
                fails.append(f"unresolved image placeholder in {d['metadata']['name']}")
            if img.endswith(":latest"):
                fails.append(f":latest tag in {d['metadata']['name']}")

# 5. Tenant quotas must bound the REAL GPU resource.
for ns in ("tenant-arise", "tenant-direct"):
    q = next((d for d in docs if d["kind"] == "ResourceQuota"
              and d["metadata"].get("namespace") == ns), None)
    if not q:
        fails.append(f"no ResourceQuota in {ns}")
    elif "requests.nvidia.com/gpu" not in q["spec"]["hard"]:
        fails.append(f"{ns} quota does not bound requests.nvidia.com/gpu")

# 6. Platform infra placement: every Deployment pins to the control-plane so
#    nothing platform-owned can squat on (or survive a drain of) a sellable
#    GPU node.
every(lambda d: d["kind"] != "Deployment"
      or d["spec"]["template"]["spec"].get("nodeSelector", {})
             .get("node-role.kubernetes.io/control-plane") is not None,
      "Deployment not pinned to control-plane")

# 7. Both product StorageClasses exist (the storage admission policy names
#    them; missing classes would strand every tenant PVC).
have_sc = {d["metadata"]["name"] for d in docs if d["kind"] == "StorageClass"}
for want in ("arise-shared", "arise-longterm"):
    if want not in have_sc:
        fails.append(f"StorageClass {want} missing")

# 8. The capacity-controller must run the no-marketplace adapter here — the
#    mock does not exist on this overlay and the production adapter does not
#    exist at all (VST-06).
cc = next((d for d in docs if d["kind"] == "Deployment"
           and d["metadata"]["name"] == "capacity-controller"), None)
if cc is None:
    fails.append("capacity-controller Deployment missing")
else:
    env = {e["name"]: e.get("value") for c in cc["spec"]["template"]["spec"]["containers"]
           for e in c.get("env", [])}
    if env.get("VAST_ADAPTER") != "none":
        fails.append(f"capacity-controller VAST_ADAPTER={env.get('VAST_ADAPTER')!r}, expected 'none'")
    if env.get("VAST_PRODUCTION_ADAPTER_ENABLED") != "false":
        fails.append("VAST_PRODUCTION_ADAPTER_ENABLED must be 'false'")
    if env.get("FAKE_GPU_RESOURCE") != "nvidia.com/gpu":
        fails.append("controller drain gate must watch nvidia.com/gpu")

print(f"  rendered objects: {len(docs)}")
if fails:
    for f in fails:
        print(f"  FAIL {f}")
    sys.exit(1)
print("  ok  all dgx render assertions hold")
PY

echo "dgx-render: PASS"
