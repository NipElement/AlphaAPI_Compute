#!/usr/bin/env bash
# ============================================================================
# registry-mirror.sh — Day-0 step 5: put every pinned image into OUR registry.
#
# A DGX cluster in a datacenter must not depend on Docker Hub / quay / GHCR
# being reachable and unchanged at the moment a pod restarts. This script
# mirrors the COMPLETE pinned image set into the private registry and prints,
# for each image, the digest as stored THERE — which is what the dgx overlay
# pins at Day-0 (kustomization `images:` + the DEVBOX_IMAGE / web sentinels).
#
# Inputs: the digest-pinned references in versions.env, every image the dgx
# overlay renders, and every image in the VENDORED manifests (Volcano,
# Calico). Upstream images are copied with `docker buildx imagetools create`,
# which moves the manifest LIST byte-for-byte, so the mirrored digest EQUALS
# the source digest and the pinned references in Git resolve unchanged
# through the mirror (node-bootstrap.sh 4b points containerd at it). A plain
# pull/tag/push would re-push a single-platform manifest under a DIFFERENT
# digest — the review of 2026-08-27 caught the header promising equality the
# code did not keep; the script now VERIFIES it and fails on mismatch. Our own
# built images (arise/web, arise/devbox) are pushed from the local engine and
# get new digests, printed for the Day-0 retag.
#
#   REGISTRY=registry.internal:5000 ./scripts/registry-mirror.sh
#   REGISTRY=127.0.0.1:5001 ./scripts/registry-mirror.sh   # rehearsal
#
# A plain-HTTP registry that is not on localhost must be listed in the docker
# daemon's "insecure-registries" (docker refuses HTTP otherwise). NOTE that
# `docker buildx imagetools create` (the upstream copy path) talks to the
# registry ITSELF and does not read insecure-registries: for a non-localhost
# plain-HTTP registry either put TLS in front of it (recommended — the rack
# will hold customer images) or run this script on the registry host against
# 127.0.0.1:<port>, which the rehearsal did. The nodes' containerd side is
# configured by node-bootstrap.sh 4b (REGISTRY_MIRROR).
#
# Uses docker (already the admin-box tool of record); no new binaries.
# ============================================================================
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
REG="${REGISTRY:?set REGISTRY=<host:port> (the private registry)}"
# shellcheck disable=SC1091
source versions.env

# 1. the set: pinned upstream images from versions.env + rendered dgx images.
#    LAB-ONLY images are excluded by name: the kind node image, the Go builder
#    (builds the lab's fake-gpu plugin) and the plugin itself must never sit
#    in the production registry — they are the simulation, and a mirror is
#    also an inventory someone will trust.
LAB_ONLY='^(KIND_NODE_IMAGE|GO_BUILDER_IMAGE|FAKE_GPU_PLUGIN_IMAGE)='
mapfile -t UPSTREAM < <(grep -oE '^[A-Z_]+_IMAGE=.*@sha256:[0-9a-f]{64}' versions.env \
                        | grep -vE "$LAB_ONLY" \
                        | cut -d= -f2- | sed -E 's/:[^@]+@/@/')    # tag@digest -> name@digest
mapfile -t RENDERED < <(kubectl kustomize platform/overlays/dgx \
                        | grep -oE 'image: \S+' | cut -d' ' -f2 | sort -u \
                        | grep -v 'day0-registry.invalid' | grep '@sha256:')
mapfile -t VENDORED < <(cat platform/vendor/*.yaml \
                        | grep -oE 'image: \S+' | cut -d' ' -f2 | sort -u | grep '@sha256:')
# The TLS edge has its own explicit-cutover overlay. Caddy includes ACME;
# cert-manager is no longer a dependency of the platform's public endpoint.
mapfile -t EDGE < <(kubectl kustomize platform/overlays/dgx/edge \
  | grep -oE 'image: \S+' | cut -d' ' -f2 | sort -u | grep '@sha256:')
# kubeadm's own control-plane set is pulled BY TAG by kubeadm init/join, so it
# is mirrored tag-to-tag (digest equality still verified). Needs kubeadm on
# this box at the pinned version; without it the list is printed and the run
# FAILS rather than pretending the mirror is complete.
if command -v kubeadm >/dev/null; then
  mapfile -t KUBEADM_IMGS < <(kubeadm config images list --kubernetes-version "$KUBE_VERSION" 2>/dev/null)
  (( ${#KUBEADM_IMGS[@]} >= 7 )) || { echo "kubeadm listed ${#KUBEADM_IMGS[@]} control-plane images for $KUBE_VERSION (expected >= 7: apiserver, controller-manager, scheduler, proxy, coredns, pause, etcd) — version skew or offline; refusing to call the mirror complete" >&2; exit 1; }
else
  echo "kubeadm not installed here: cannot enumerate the control-plane image set for $KUBE_VERSION" >&2
  echo "  (registry.k8s.io/kube-{apiserver,controller-manager,scheduler,proxy}:$KUBE_VERSION, coredns, pause, etcd)" >&2
  echo "  install kubeadm=$KUBE_VERSION on the admin box (node-bootstrap.sh 4c) and re-run" >&2
  exit 1
fi
OURS=("$ARISE_WEB_IMAGE" "$DEVBOX_IMAGE")   # built locally, tagged, not yet digest-pinned

declare -A SEEN; ALL=()
for img in "${UPSTREAM[@]}" "${RENDERED[@]}" "${VENDORED[@]}" "${EDGE[@]}"; do
  [[ -n "${SEEN[$img]:-}" ]] && continue; SEEN[$img]=1; ALL+=("$img")
done

echo "mirroring $(( ${#ALL[@]} + ${#OURS[@]} + ${#KUBEADM_IMGS[@]} )) images into $REG"
# Under evidence/RUN-dgx/ so .gitignore covers it: a mirror record names a
# specific registry host and is a run artifact, not source.
OUT="$REPO/evidence/RUN-dgx/registry-mirror-$(date -u +%Y%m%dT%H%M%SZ).txt"
mkdir -p "$(dirname "$OUT")"
{
  echo "# registry mirror $(date -u +%FT%TZ) -> $REG"
  echo "# source => mirrored (digest as stored in $REG)"
} > "$OUT"

mirror() {  # mirror <source-ref> <target-repo-path>
  local src="$1" path="$2" target="$REG/$path" dig
  if [[ "$src" == *@sha256:* ]]; then
    # Upstream, digest-pinned: registry-to-registry copy of the whole index.
    # The mirrored digest MUST equal the source digest; anything else means
    # the manifest was rewritten and every pinned reference would 404.
    docker buildx imagetools create --tag "$target" "$src" >/dev/null
    dig=$(docker buildx imagetools inspect "$target" --format '{{.Manifest.Digest}}')
    [[ "$dig" == "${src##*@}" ]] || { echo "DIGEST MISMATCH $src -> $target@$dig" >&2; exit 1; }
  else
    # Our own build: exists only in the local engine; push gives it a digest.
    docker image inspect "$src" >/dev/null 2>&1 || { echo "local image missing: $src (make web-image / devbox-image)" >&2; exit 1; }
    docker tag "$src" "$target"
    docker push -q "$target" >/dev/null
    dig=$(docker inspect "$target" --format '{{range .RepoDigests}}{{.}}{{"\n"}}{{end}}' | grep "^$REG/" | head -1 | sed 's/.*@//')
  fi
  printf '%-100s => %s@%s\n' "$src" "$target" "$dig" | tee -a "$OUT"
}

for img in "${ALL[@]}"; do
  # keep the upstream path under our registry: registry.k8s.io/etcd@sha256:x
  # -> $REG/registry.k8s.io/etcd  (a mirror is easier to audit than a rename)
  name="${img%%@*}"
  case "$name" in
    */*) path="$name" ;;              # already has a namespace (quay.io/…, registry.k8s.io/…)
    *)   path="docker.io/library/$name" ;;   # bare docker hub names (python, busybox)
  esac
  mirror "$img" "$path"
done
for ref in "${KUBEADM_IMGS[@]}"; do   # registry.k8s.io/kube-apiserver:v1.36.2 -> $REG/registry.k8s.io/kube-apiserver:v1.36.2
  name="${ref%%:*}"; tag="${ref##*:}"
  src_dig=$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}')
  docker buildx imagetools create --tag "$REG/$name:$tag" "$ref" >/dev/null
  dst_dig=$(docker buildx imagetools inspect "$REG/$name:$tag" --format '{{.Manifest.Digest}}')
  [[ "$src_dig" == "$dst_dig" ]] || { echo "DIGEST MISMATCH $ref -> $REG/$name:$tag" >&2; exit 1; }
  printf '%-100s => %s@%s\n' "$ref" "$REG/$name:$tag" "$dst_dig" | tee -a "$OUT"
done
MISSING_OWN=0
for img in "${OURS[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "MISSING $img (not built locally — run make web-image / make devbox-image)" | tee -a "$OUT" >&2
    MISSING_OWN=1; continue
  fi
  mirror "$img" "${img%%:*}"           # arise/web:dgx -> $REG/arise/web
done
# A mirror without our own images cannot satisfy dgx-sentinel-check: say so
# with a non-zero exit instead of a line someone scrolls past.
(( MISSING_OWN == 0 )) || { echo "own images missing — mirror INCOMPLETE (see above)" >&2; exit 1; }

echo
echo "record: $OUT"
echo "NEXT: pin the printed digests — python stays the same digest (content-addressed);"
echo "      arise/web and arise/devbox get their NEW registry digests in"
echo "      platform/overlays/dgx/kustomization.yaml (images:) and tenant-portal.yaml (DEVBOX_IMAGE)."
