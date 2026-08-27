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
OURS=("$ARISE_WEB_IMAGE" "$DEVBOX_IMAGE")   # built locally, tagged, not yet digest-pinned

declare -A SEEN; ALL=()
for img in "${UPSTREAM[@]}" "${RENDERED[@]}" "${VENDORED[@]}"; do
  [[ -n "${SEEN[$img]:-}" ]] && continue; SEEN[$img]=1; ALL+=("$img")
done

echo "mirroring $(( ${#ALL[@]} + ${#OURS[@]} )) images into $REG"
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
for img in "${OURS[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "SKIP $img (not built locally — run make web-image / make devbox-image)" | tee -a "$OUT"
    continue
  fi
  mirror "$img" "${img%%:*}"           # arise/web:dgx -> $REG/arise/web
done

echo
echo "record: $OUT"
echo "NEXT: pin the printed digests — python stays the same digest (content-addressed);"
echo "      arise/web and arise/devbox get their NEW registry digests in"
echo "      platform/overlays/dgx/kustomization.yaml (images:) and tenant-portal.yaml (DEVBOX_IMAGE)."
