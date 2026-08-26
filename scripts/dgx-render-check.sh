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

# 2. Required admission policies AND their bindings (a policy without its
#    binding validates nothing — the object exists, the gate does not).
for want in ("arise-deny-simulated-gpu", "arise-tenant-owner-gate",
             "arise-tenant-host-isolation", "arise-queue-binding",
             "arise-priority-binding", "arise-flavor-quantization",
             "arise-storage-quantization"):
    if not any(d["metadata"].get("name") == want
               and d["kind"] == "ValidatingAdmissionPolicy" for d in docs):
        fails.append(f"ValidatingAdmissionPolicy {want} missing")
    if not any(d["metadata"].get("name") == want
               and d["kind"] == "ValidatingAdmissionPolicyBinding" for d in docs):
        fails.append(f"ValidatingAdmissionPolicyBinding {want} missing")

# 2b. The owner gate must carry the lab's DIRECT branch: tenant-direct pods
#     target the node their customer pays for. A dgx copy that hardcodes
#     ARISE denies the day-0 product (caught in review, 2026-08-26).
og = next((d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"
           and d["metadata"]["name"] == "arise-tenant-owner-gate"), None)
if og and "allowedOwner" not in yaml.dump(og):
    fails.append("owner gate lacks the allowedOwner DIRECT branch "
                 "(tenant-direct would be denied its own reserved nodes)")

# 3. No hostPath VOLUMES in any workload: the docker-cp/hostPath SPA delivery
#    is a kind-ism and no dgx workload may depend on one node's filesystem.
#    (Inspects real pod specs — the host-isolation POLICY legitimately
#    mentions the word "hostPath" in its denial message.)
for d in docs:
    tmpl = None
    if d["kind"] in ("Deployment", "DaemonSet", "StatefulSet", "Job"):
        tmpl = d["spec"]["template"]["spec"]
    elif d["kind"] == "Pod":
        tmpl = d["spec"]
    if tmpl and any("hostPath" in v for v in (tmpl.get("volumes") or [])):
        fails.append(f"hostPath volume: {d['kind']} "
                     f"{d['metadata'].get('namespace','')}/{d['metadata']['name']}")

# 4. Image hygiene: no unresolved placeholders, no :latest, and EVERY image
#    digest-pinned — with exactly one sanctioned exception: arise/web resolves
#    to the day0-registry.invalid sentinel (an IETF-reserved TLD that cannot
#    pull) until Day-0 pushes it and pins the registry digest. Anything else
#    unpinned is a mutable-tag supply-chain hole.
images = set()
for d in docs:
    for line in yaml.dump(d).splitlines():
        if "image:" in line:
            img = line.split("image:", 1)[1].strip().strip("'\"")
            images.add((d["metadata"].get("name", "?"), img))
for owner, img in sorted(images):
    if "PLACEHOLDER" in img:
        fails.append(f"unresolved image placeholder in {owner}")
    elif img.endswith(":latest"):
        fails.append(f":latest tag in {owner}")
    elif "@sha256:" not in img and not img.startswith("day0-registry.invalid/"):
        fails.append(f"image not digest-pinned in {owner}: {img}")

# 4b. Cross-check against versions.env so the two declarations cannot
#     silently diverge: the rendered python digest must equal
#     PYTHON_BASE_IMAGE's, and the sentinel web image must carry
#     ARISE_WEB_IMAGE's tag.
env = {}
for line in open("versions.env"):
    line = line.split("#", 1)[0].strip()
    if "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
py_digest = env.get("PYTHON_BASE_IMAGE", "").split("@")[-1]
web_tag = env.get("ARISE_WEB_IMAGE", "").rsplit(":", 1)[-1]
rendered_py = {img for _, img in images if img.startswith("python@")}
if rendered_py and not all(i.endswith(py_digest) for i in rendered_py):
    fails.append(f"rendered python digest disagrees with versions.env: {rendered_py}")
rendered_web = {img for _, img in images
                if img.startswith("day0-registry.invalid/arise/web")}
if rendered_web and not all(i.endswith(":" + web_tag) for i in rendered_web):
    fails.append(f"rendered web tag disagrees with versions.env "
                 f"ARISE_WEB_IMAGE ({web_tag}): {rendered_web}")

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

# 9. Identity: every rendered object carries project=arise-b300 (the overlay
#    label transformer overrides base's -prelab), so fleet-wide selectors on
#    hardware see the whole platform, not just overlay-authored objects.
for d in docs:
    lbl = (d["metadata"].get("labels") or {}).get("project")
    if lbl != "arise-b300":
        fails.append(f"project label {lbl!r} on {d['kind']} "
                     f"{d['metadata'].get('namespace','')}/{d['metadata']['name']}")

print(f"  rendered objects: {len(docs)}")
if fails:
    for f in fails:
        print(f"  FAIL {f}")
    sys.exit(1)
print("  ok  all dgx render assertions hold")
PY

echo "dgx-render: PASS"
