#!/usr/bin/env python3
"""operator-images.py — the NVIDIA GPU Operator / Network Operator image lock.

Two Helm charts, pinned by version in versions.env, pull about twenty container
images between them. Until 2026-09-08 not one of them had a digest anywhere in
this repository and none was in the registry mirror: `helm install --version`
pins the CHART, and every image a chart names is pulled by mutable tag from
nvcr.io whenever a pod (re)starts. This tool makes the image set a lock that
is checked offline like every other pinned dependency.

  resolve      (network: helm, docker) pull the pinned charts, enumerate every
               image they and our values / NicClusterPolicy name, resolve each
               tag to its manifest digest, write infra/dgx/operators/operator-images.lock
  check        (offline; `make validate`) the lock agrees with versions.env;
               every image we run — or may switch on at Day-0 — has a digest;
               every digest-capable field in our values / NicClusterPolicy
               carries EXACTLY the lock's digest; no placeholder in an image field
  mirror-list  (offline) "<image:tag>\\t<digest>" for every image to mirror,
               consumed by scripts/registry-mirror.sh
  selftest     (offline) break copies of the lock / values one way at a time
               and require `check` to refuse each: the gate proving it detects

Digest pinning is supported by both operators where it matters. gpu-operator
(internal/image/image.go) joins a `version` that starts with "sha256:" using
"@", so every ClusterPolicy component is pinned by `version: sha256:...` in the
Helm values; network-operator (pkg/render/render.go, imagePath) does the same
for every NicClusterPolicy component. The two operator Deployments themselves
and the NFD sub-chart compose `repository/image:tag` in Helm templates with no
digest form: those are pinned by the MIRROR (tag copied with digest equality
verified, nodes pull through the mirror) and checked at runtime by
verify-dgx.sh DGX-31/32 against this lock.

Lock format (tab-separated, `#` comments):
  component  state  image:tag  digest|-  pin
  state  enabled     runs with our values                     -> digest required, mirrored
         conditional a ⟪DECIDE-WITH-VENDOR⟫ flip may enable it -> digest required, mirrored
         disabled    off in chart or values                   -> inventory only
  pin    values:<dotted.path>  our gpu-operator values field that must carry the digest
         ncp:<dotted.path>     our NicClusterPolicy field that must carry the digest
         tag-only              no digest form in the chart; mirror + DGX-31/32 cover it
"""
import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REL_LOCK = "infra/dgx/operators/operator-images.lock"
REL_GPU_VALUES = "infra/dgx/operators/gpu-operator-values.yaml"
REL_NET_VALUES = "infra/dgx/operators/network-operator-values.yaml"
REL_NCP = "infra/dgx/operators/nic-cluster-policy.yaml"
REL_VERSIONS = "versions.env"
HELM_REPO = "https://helm.ngc.nvidia.com/nvidia"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# DGX OS 7 is Ubuntu 24.04-based, DGX OS 6 Ubuntu 22.04: the driver image tag
# carries the OS suffix (gpu-operator appends it unless a digest is given), so
# the flip case needs one digest per plausible delivered OS.
DRIVER_OS_TAGS = ("ubuntu24.04", "ubuntu22.04")
# gpu-operator components whose ClusterPolicy `version` accepts a digest.
GPU_DIGEST_CAPABLE = {
    "validator", "toolkit", "devicePlugin", "dcgm", "dcgmExporter", "gfd",
    "migManager", "nodeStatusExporter", "gds", "gdrcopy", "vgpuDeviceManager",
    "vfioManager", "sandboxDevicePlugin", "kataSandboxDevicePlugin", "ccManager",
    "driver", "driver.manager",
}
SANDBOX_FAMILY = {"vgpuManager", "vgpuDeviceManager", "vfioManager", "vfioManager.driverManager",
                  "vgpuManager.driverManager", "sandboxDevicePlugin", "kataSandboxDevicePlugin",
                  "ccManager", "kataManager"}


class Fail(Exception):
    pass


# ----------------------------------------------------------------- helpers --
def read_versions(root):
    out = {}
    for line in (root / REL_VERSIONS).read_text().splitlines():
        m = re.match(r"^([A-Z_]+)=([^#\s]+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    for k in ("GPU_OPERATOR_VERSION_REFERENCE", "NETWORK_OPERATOR_VERSION_REFERENCE"):
        if k not in out:
            raise Fail(f"{REL_VERSIONS}: {k} missing")
    return out


def get_path(d, dotted, default=None):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path):
    return yaml.safe_load(path.read_text()) or {}


def ncp_doc(root):
    docs = [d for d in yaml.safe_load_all((root / REL_NCP).read_text())
            if isinstance(d, dict) and d.get("kind") == "NicClusterPolicy"]
    if len(docs) != 1:
        raise Fail(f"{REL_NCP}: expected exactly one NicClusterPolicy document, found {len(docs)}")
    return docs[0]


def ncp_components(doc):
    """Every spec key that carries an image triple: {name: {image, repository, version}}."""
    out = {}
    for key, val in (doc.get("spec") or {}).items():
        if isinstance(val, dict) and "image" in val:
            out[key] = val
    return out


def ncp_tag_hint(root, comp):
    """A `# tag: <x>` comment on the component's version line (fallback when the
    release convention `network-operator-<appVersion>` does not apply)."""
    text = (root / REL_NCP).read_text()
    m = re.search(rf"^\s*{re.escape(comp)}:\n(?:^\s+.*\n)*?^\s+version:.*#\s*tag:\s*(\S+)", text, re.M)
    return m.group(1) if m else None


# -------------------------------------------------------------------- lock --
class Entry:
    __slots__ = ("component", "state", "image", "digest", "pin")

    def __init__(self, component, state, image, digest, pin):
        self.component, self.state, self.image, self.digest, self.pin = component, state, image, digest, pin

    def line(self):
        return "\t".join([self.component, self.state, self.image, self.digest or "-", self.pin])


def read_lock(root):
    path = root / REL_LOCK
    if not path.exists():
        raise Fail(f"{REL_LOCK} missing — run `python3 scripts/operator-images.py resolve`")
    header, entries = {}, []
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        if ln.startswith("#"):
            m = re.match(r"#\s*chart:\s*(\S+)\s+(\S+)\s+appVersion\s+(\S+)", ln)
            if m:
                header[m.group(1)] = (m.group(2), m.group(3))
            continue
        parts = ln.split("\t")
        if len(parts) != 5:
            raise Fail(f"{REL_LOCK}: malformed line {ln!r}")
        comp, state, image, digest, pin = parts
        if state not in ("enabled", "conditional", "disabled"):
            raise Fail(f"{REL_LOCK}: {comp}: unknown state {state!r}")
        entries.append(Entry(comp, state, image, None if digest == "-" else digest, pin))
    if "gpu-operator" not in header or "network-operator" not in header:
        raise Fail(f"{REL_LOCK}: header must record both charts (`# chart: <name> <version> appVersion <v>`)")
    return header, entries


def write_lock(root, header, entries):
    lines = [
        "# operator-images.lock — generated by scripts/operator-images.py resolve, "
        + dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "# Do not edit by hand: `check` (make validate) refuses a lock that disagrees with",
        "# versions.env or with the digests written into the values / NicClusterPolicy.",
    ]
    for name in ("gpu-operator", "network-operator"):
        ver, app = header[name]
        lines.append(f"# chart: {name} {ver} appVersion {app}")
    lines.append("# component\tstate\timage:tag\tdigest\tpin")
    lines += [e.line() for e in sorted(entries, key=lambda e: e.component)]
    (root / REL_LOCK).write_text("\n".join(lines) + "\n")


# ----------------------------------------------------------------- resolve --
def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def resolve_digest(ref):
    try:
        out = sh(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{.Manifest.Digest}}"])
    except subprocess.CalledProcessError as exc:
        raise Fail(f"cannot resolve {ref}: {exc.stderr.strip()[:200]}") from exc
    dig = out.strip().splitlines()[-1]
    if not DIGEST_RE.match(dig):
        raise Fail(f"{ref}: unexpected digest {dig!r}")
    return dig


def pull_chart(name, version, workdir):
    subprocess.run(["helm", "repo", "add", "nvidia", HELM_REPO], capture_output=True, text=True)
    subprocess.run(["helm", "repo", "update", "nvidia"], capture_output=True, text=True)
    sh(["helm", "pull", f"nvidia/{name}", "--version", version, "--untar", "-d", str(workdir)])
    chart_dir = workdir / name
    meta = load_yaml(chart_dir / "Chart.yaml")
    return chart_dir, meta.get("version"), meta.get("appVersion")


def gpu_entries(chart_dir, app_version, ours):
    defaults = load_yaml(chart_dir / "values.yaml")
    merged = deep_merge(defaults, ours)
    sandbox_on = bool(get_path(merged, "sandboxWorkloads.enabled", False))
    entries = []

    def state_for(comp):
        if comp in ("operator", "validator"):
            return "enabled"
        if comp == "nfd":
            return "enabled" if get_path(merged, "nfd.enabled", True) else "disabled"
        if comp in ("driver", "driver.manager"):
            # ⟪DECIDE-WITH-VENDOR⟫: DGX OS may or may not ship the driver. Mirror it
            # either way so the flip at Day-0 does not need nvcr.io reachable.
            return "enabled" if get_path(merged, "driver.enabled", False) else "conditional"
        if comp in SANDBOX_FAMILY:
            base = comp.split(".")[0]
            return "enabled" if sandbox_on and get_path(merged, f"{base}.enabled", False) else "disabled"
        base = comp.split(".")[0]
        return "enabled" if get_path(merged, f"{base}.enabled", False) else "disabled"

    def add(comp, repo, image, version, pin, state=None):
        if not repo or not image:
            return                      # vgpu-manager: customer-built, no public image
        st = state or state_for(comp)
        entries.append(Entry(f"gpu-operator/{comp}", st, f"{repo}/{image}:{version}", None, pin))

    op = merged["operator"]
    add("operator", op.get("repository"), op.get("image"), op.get("version") or app_version, "tag-only")
    val = merged.get("validator") or {}
    add("validator", val.get("repository") or op.get("repository"), val.get("image") or op.get("image"),
        val.get("version") or app_version, "values:validator.version")

    def triple(node):
        return node.get("repository"), node.get("image"), node.get("version")

    for comp in ("toolkit", "devicePlugin", "dcgm", "dcgmExporter", "gfd", "migManager", "gds", "gdrcopy",
                 "vgpuDeviceManager", "vfioManager", "sandboxDevicePlugin", "kataSandboxDevicePlugin",
                 "ccManager"):
        node = merged.get(comp) or {}
        repo, image, version = triple(node)
        if version and DIGEST_RE.match(str(version)):
            version = str(defaults.get(comp, {}).get("version") or version)   # tag lives in chart defaults
        add(comp, repo, image, version, f"values:{comp}.version")
    nse = merged.get("nodeStatusExporter") or {}
    add("nodeStatusExporter", nse.get("repository") or op.get("repository"), nse.get("image") or op.get("image"),
        nse.get("version") or app_version, "values:nodeStatusExporter.version")
    # driver: OS-suffixed tags, one entry per plausible DGX OS
    drv = merged.get("driver") or {}
    repo, image, version = triple(drv)
    if version and DIGEST_RE.match(str(version)):
        version = str(defaults.get("driver", {}).get("version") or version)
    for os_tag in DRIVER_OS_TAGS:
        add(f"driver[{os_tag}]", repo, image, f"{version}-{os_tag}", "values:driver.version",
            state=state_for("driver"))
    mgr = drv.get("manager") or {}
    add("driver.manager", mgr.get("repository"), mgr.get("image"), mgr.get("version"), "values:driver.manager.version",
        state=state_for("driver.manager"))
    # NFD sub-chart (tag-only in its templates; `image.repository` is the full name)
    nfd_img = get_path(merged, "node-feature-discovery.image", {}) or {}
    nfd_meta = load_yaml(chart_dir / "charts" / "node-feature-discovery" / "Chart.yaml")
    if nfd_img.get("repository"):
        entries.append(Entry("gpu-operator/nfd", state_for("nfd"),
                             f"{nfd_img['repository']}:{nfd_img.get('tag') or nfd_meta.get('appVersion')}",
                             None, "tag-only"))
    return entries


def net_entries(root, chart_dir, app_version, ours, ncp):
    defaults = load_yaml(chart_dir / "values.yaml")
    merged = deep_merge(defaults, ours)
    entries = []
    op = merged["operator"]
    entries.append(Entry("network-operator/operator", "enabled",
                         f"{op['repository']}/{op['image']}:{op.get('tag') or app_version}", None, "tag-only"))
    init = get_path(merged, "operator.ofedDriver.initContainer", {}) or {}
    if init.get("repository"):
        st = "enabled" if "ofedDriver" in (ncp.get("spec") or {}) else "disabled"
        entries.append(Entry("network-operator/ofedDriver.initContainer", st,
                             f"{init['repository']}/{init['image']}:{init['version']}", None, "tag-only"))
    nfd_img = get_path(merged, "node-feature-discovery.image", {}) or {}
    if nfd_img.get("repository"):
        st = "enabled" if get_path(merged, "nfd.enabled", True) else "disabled"
        entries.append(Entry("network-operator/nfd", st, f"{nfd_img['repository']}:{nfd_img.get('tag')}", None, "tag-only"))
    for comp, node in ncp_components(ncp).items():
        version = str(node.get("version") or "")
        if DIGEST_RE.match(version):
            hint = ncp_tag_hint(root, comp)
            if hint:
                tag = hint
            elif str(node.get("repository", "")).startswith("nvcr.io/nvidia/mellanox"):
                tag = f"network-operator-{app_version}"      # the release's convention (hack/release.yaml)
            else:
                raise Fail(f"{REL_NCP}: {comp}.version is a digest and no tag can be inferred; "
                           f"add `# tag: <tag>` on that line")
        elif not version or "REPLACE_WITH" in version:
            raise Fail(f"{REL_NCP}: {comp}.version is {version!r}; the release's component tags are in "
                       f"hack/release.yaml of the network-operator tag (v26.4.0: network-operator-v26.4.0)")
        else:
            tag = version
        entries.append(Entry(f"network-operator/ncp.{comp}", "enabled",
                             f"{node['repository']}/{node['image']}:{tag}", None, f"ncp:{comp}.version"))
    return entries


def cmd_resolve(root):
    versions = read_versions(root)
    gpu_ver = versions["GPU_OPERATOR_VERSION_REFERENCE"]
    net_ver = versions["NETWORK_OPERATOR_VERSION_REFERENCE"]
    for tool in ("helm", "docker"):
        if not shutil.which(tool):
            raise Fail(f"{tool} not installed; resolve needs helm (charts) and docker buildx (digests)")
    with tempfile.TemporaryDirectory(prefix="operator-images-") as tmp:
        work = Path(tmp)
        gpu_dir, gpu_chart_ver, gpu_app = pull_chart("gpu-operator", gpu_ver, work)
        net_dir, net_chart_ver, net_app = pull_chart("network-operator", net_ver, work)
        header = {"gpu-operator": (gpu_ver, gpu_app), "network-operator": (net_ver, net_app)}
        entries = gpu_entries(gpu_dir, gpu_app, load_yaml(root / REL_GPU_VALUES))
        entries += net_entries(root, net_dir, net_app, load_yaml(root / REL_NET_VALUES), ncp_doc(root))
    seen = set()
    for e in entries:
        if e.component in seen:
            raise Fail(f"duplicate component {e.component}")
        seen.add(e.component)
        if e.state in ("enabled", "conditional"):
            e.digest = resolve_digest(e.image)
            print(f"  {e.state:11s} {e.image:88s} {e.digest[:19]}…")
        else:
            print(f"  {e.state:11s} {e.image:88s} (not resolved: not in use)")
    write_lock(root, header, entries)
    print(f"wrote {REL_LOCK}: {len(entries)} images, "
          f"{sum(1 for e in entries if e.digest)} digests")
    print("NEXT: python3 scripts/operator-images.py check  — it names every values field that must carry a digest")


# ------------------------------------------------------------------- check --
def check(root):
    """Returns a list of failures (empty = consistent)."""
    fails = []
    versions = read_versions(root)
    header, entries = read_lock(root)
    for name, key in (("gpu-operator", "GPU_OPERATOR_VERSION_REFERENCE"),
                      ("network-operator", "NETWORK_OPERATOR_VERSION_REFERENCE")):
        if header[name][0] != versions[key]:
            fails.append(f"{REL_LOCK} was resolved for {name} {header[name][0]} but versions.env pins "
                         f"{versions[key]} — run resolve after a version bump")
    by_comp = {e.component: e for e in entries}
    gpu_vals = load_yaml(root / REL_GPU_VALUES)
    ncp = ncp_doc(root)

    def explicit_enabled(dotted):
        v = get_path(gpu_vals, dotted + ".enabled")
        return None if v is None else bool(v)

    sandbox_on = bool(get_path(gpu_vals, "sandboxWorkloads.enabled", False))

    def gpu_in_use(comp, lock_state):
        """Is this gpu-operator component going to run, given OUR values (the
        lock's state carries the chart default for anything we do not say)?"""
        base = comp.split("[")[0].split(".")[0]
        if base in ("validator", "operator"):
            return True
        if base == "driver":
            return bool(get_path(gpu_vals, "driver.enabled", False))
        if base in SANDBOX_FAMILY:
            return sandbox_on and bool(get_path(gpu_vals, f"{base}.enabled", lock_state == "enabled"))
        exp = explicit_enabled(base)
        return (lock_state == "enabled") if exp is None else exp

    for e in entries:
        if e.state in ("enabled", "conditional") and not e.digest:
            fails.append(f"{e.component} ({e.image}) is {e.state} but has no digest")
        if e.digest and not DIGEST_RE.match(e.digest):
            fails.append(f"{e.component}: malformed digest {e.digest!r}")
        if e.image.count(":") < 1 or "REPLACE_WITH" in e.image:
            fails.append(f"{e.component}: image reference {e.image!r} has no tag or is a placeholder")

    # gpu-operator: a component that will run must not sit in the lock as
    # `disabled` — mirror-list skips disabled entries, so the fleet would pull
    # it from nvcr.io unpinned (the self-test's "in-use component marked
    # disabled" mutation is exactly this).
    for e in entries:
        if not e.component.startswith("gpu-operator/"):
            continue
        comp = e.component.split("/", 1)[1]
        if e.state == "disabled" and gpu_in_use(comp, e.state):
            fails.append(f"{e.component} is in use per {REL_GPU_VALUES} but the lock marks it disabled "
                         f"(it would not be mirrored) — run resolve")
    # ...and every digest-capable component that is in use must be pinned to
    # the lock's digest in our values.
    for e in entries:
        if not e.component.startswith("gpu-operator/") or not e.pin.startswith("values:"):
            continue
        comp = e.component.split("/", 1)[1]
        if not gpu_in_use(comp, e.state):
            continue
        if not e.digest:
            fails.append(f"{comp} is enabled in {REL_GPU_VALUES} but the lock has no digest for it "
                         f"(state {e.state}) — run resolve")
            continue
        field = e.pin.split(":", 1)[1]
        got = get_path(gpu_vals, field)
        if comp.startswith("driver["):
            drivers = [x.digest for x in entries if x.component.startswith("gpu-operator/driver[") and x.digest]
            if got not in drivers:
                fails.append(f"{REL_GPU_VALUES}: driver.enabled is true, so driver.version must be one of the "
                             f"lock's driver digests {drivers}; got {got!r}")
        elif got != e.digest:
            fails.append(f"{REL_GPU_VALUES}: {field} must be {e.digest} ({e.image}); got {got!r}")
    # ...and nothing digest-capable is in use without a lock entry at all
    for comp in sorted(GPU_DIGEST_CAPABLE):
        key = "gpu-operator/" + (f"driver[{DRIVER_OS_TAGS[0]}]" if comp == "driver" else comp)
        if key not in by_comp and gpu_in_use(comp, "disabled"):
            fails.append(f"{comp} is in use but has no lock entry — run resolve")

    # network-operator: every NicClusterPolicy component pinned to the lock digest
    for comp, node in ncp_components(ncp).items():
        e = by_comp.get(f"network-operator/ncp.{comp}")
        for k in ("image", "repository", "version"):
            if "REPLACE_WITH" in str(node.get(k, "")):
                fails.append(f"{REL_NCP}: {comp}.{k} is still a placeholder ({node.get(k)})")
        if e is None:
            fails.append(f"{REL_NCP}: {comp} has no lock entry — run resolve")
            continue
        if not e.digest:
            fails.append(f"{REL_NCP}: {comp}: lock entry has no digest")
            continue
        if str(node.get("version")) != e.digest:
            fails.append(f"{REL_NCP}: {comp}.version must be {e.digest} ({e.image}); got {node.get('version')!r}")
        want_repo = e.image.rsplit(":", 1)[0].rsplit("/", 1)[0]
        if str(node.get("repository")) != want_repo or str(node.get("image")) != e.image.rsplit(":", 1)[0].rsplit("/", 1)[1]:
            fails.append(f"{REL_NCP}: {comp} names {node.get('repository')}/{node.get('image')} but the lock "
                         f"has {e.image}")
    for e in entries:
        if e.pin.startswith("ncp:"):
            comp = e.component.split("ncp.", 1)[1]
            if comp not in ncp_components(ncp):
                fails.append(f"{REL_LOCK}: {e.component} is locked but absent from {REL_NCP} — run resolve")
    return fails


def cmd_check(root):
    fails = check(root)
    if fails:
        for f in fails:
            print(f"  FAIL {f}")
        return 1
    header, entries = read_lock(root)
    n_mirror = sum(1 for e in entries if e.state in ("enabled", "conditional"))
    print(f"  ok   operator image lock consistent: {len(entries)} images "
          f"({n_mirror} mirrored, {sum(1 for e in entries if e.pin != 'tag-only' and e.state == 'enabled')} digest-pinned "
          f"in values/NicClusterPolicy); gpu-operator {header['gpu-operator'][0]}, "
          f"network-operator {header['network-operator'][0]}")
    return 0


def cmd_mirror_list(root):
    fails = check(root)
    if fails:
        for f in fails:
            print(f"FAIL {f}", file=sys.stderr)
        return 1
    _, entries = read_lock(root)
    for e in sorted(entries, key=lambda e: e.component):
        if e.state in ("enabled", "conditional"):
            print(f"{e.image}\t{e.digest}")
    return 0


# ---------------------------------------------------------------- selftest --
def cmd_selftest(root):
    """Copy the four inputs, mutate one thing at a time, require check() to fail."""
    files = [REL_LOCK, REL_GPU_VALUES, REL_NET_VALUES, REL_NCP, REL_VERSIONS]
    if check(root):
        print("SELF-TEST SETUP FAILED: the unmodified inputs do not pass check")
        return 1
    _, entries = read_lock(root)
    pinned = next(e for e in entries if e.state == "enabled" and e.pin.startswith("values:") and e.digest)
    ncp_e = next(e for e in entries if e.pin.startswith("ncp:"))
    mutations = [
        ("lock digest altered", REL_LOCK, pinned.digest, "sha256:" + "0" * 64),
        ("values digest altered", REL_GPU_VALUES, pinned.digest, "sha256:" + "1" * 64),
        ("values pin replaced by a tag", REL_GPU_VALUES, pinned.digest, pinned.image.rsplit(":", 1)[1]),
        ("NicClusterPolicy pin replaced by placeholder", REL_NCP, ncp_e.digest, "REPLACE_WITH_PINNED_TAG"),
        ("NicClusterPolicy pin replaced by the tag", REL_NCP, ncp_e.digest, ncp_e.image.rsplit(":", 1)[1]),
        ("chart version bumped without resolve", REL_VERSIONS,
         "GPU_OPERATOR_VERSION_REFERENCE=", "GPU_OPERATOR_VERSION_REFERENCE=v99.0.0 #"),
        ("lock entry removed", REL_LOCK, pinned.line() + "\n", ""),
        ("in-use component marked disabled in the lock", REL_LOCK, pinned.line(),
         pinned.line().replace("\tenabled\t", "\tdisabled\t")),
        ("digest dropped from an enabled entry", REL_LOCK, pinned.line(),
         pinned.line().replace(pinned.digest, "-")),
    ]
    holes = 0
    with tempfile.TemporaryDirectory(prefix="operator-images-selftest-") as tmp:
        work = Path(tmp)
        for rel in files:
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(root / rel, work / rel)
        for name, rel, old, new in mutations:
            path = work / rel
            orig = path.read_text()
            if old not in orig:
                print(f"  STALE {name}: {old[:40]!r} not in {rel}")
                holes += 1
                continue
            path.write_text(orig.replace(old, new, 1))
            try:
                fails = check(work)
            except Fail as exc:
                fails = [str(exc)]
            path.write_text(orig)
            if fails:
                print(f"  OK    {name:48s} check refused it")
            else:
                print(f"  HOLE  {name:48s} check ACCEPTED it")
                holes += 1
    if holes:
        print(f"SELF-TEST FAILED: {holes} mutation(s) not caught")
        return 1
    print(f"SELF-TEST PASSED: all {len(mutations)} mutations refused")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["resolve", "check", "mirror-list", "selftest"])
    ap.add_argument("--root", default=str(ROOT), help="repository root (selftest uses copies)")
    args = ap.parse_args()
    root = Path(args.root)
    try:
        if args.command == "resolve":
            cmd_resolve(root)
            return 0
        if args.command == "check":
            return cmd_check(root)
        if args.command == "mirror-list":
            return cmd_mirror_list(root)
        return cmd_selftest(root)
    except Fail as exc:
        print(f"  FAIL {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
