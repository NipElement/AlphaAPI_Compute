#!/usr/bin/env bash
# ============================================================================
# install-tools.sh — kind / kubectl / helm into a USER-LOCAL bin dir.
#
# No sudo. No system path. No package manager. Nothing outside $TOOL_BIN_DIR
# is written. Satisfies plan §7.3 "download checksum, verify, then move into a
# controlled PATH" while honouring WAIVER-2026-08-11-001 C-1 (lab artifacts
# stay removable and out of system paths).
#
# Checksums are fetched from the official endpoint every run and verified.
# No hash is hardcoded — a hand-copied hash proves nothing.
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"
RUN_ID="$(cat "$REPO/.run_id")"
DEPLOY_EV="$REPO/evidence/$RUN_ID/deploy"
mkdir -p "$DEPLOY_EV" "$TOOL_BIN_DIR"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

LOG="$DEPLOY_EV/toolchain-install.log"
: > "$LOG"
log() { echo "$*" | tee -a "$LOG"; }

log "=== toolchain install  run_id=$RUN_ID  at=$(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
log "target bin dir: $TOOL_BIN_DIR"
log ""

# Take a command's first output line WITHOUT a pipe. Under `set -e` +
# `pipefail`, `cmd | head -1` lets head close the pipe, the producer takes
# SIGPIPE (141), pipefail propagates it and the script dies silently. That
# class of bug bit install-docker.sh on 2026-08-11; it is avoided here rather
# than left to timing luck.
first_line() {
  local out
  out="$("$@" 2>&1 || true)"
  printf '%s' "${out%%$'\n'*}"
}

verify() {  # verify <file> <expected_hash> <label>
  local f="$1" expect="$2" label="$3" actual
  actual="$(sha256sum "$f" | cut -d' ' -f1)"
  if [[ "$actual" != "$expect" ]]; then
    log "FAIL $label checksum mismatch"
    log "  expected: $expect"
    log "  actual  : $actual"
    return 1
  fi
  log "OK   $label sha256=$actual"
}

# ------------------------------------------------------------------- kind --
log "--- kind $KIND_VERSION ---"
KIND_URL="https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-amd64"
curl -fsSL -o "$WORK/kind" "$KIND_URL"
KIND_EXPECT="$(curl -fsSL "${KIND_URL}.sha256sum" | awk '{print $1}')"
verify "$WORK/kind" "$KIND_EXPECT" "kind $KIND_VERSION"
install -m 0755 "$WORK/kind" "$TOOL_BIN_DIR/kind"

# ---------------------------------------------------------------- kubectl --
log "--- kubectl $KUBECTL_VERSION ---"
KUBECTL_URL="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
curl -fsSL -o "$WORK/kubectl" "$KUBECTL_URL"
KUBECTL_EXPECT="$(curl -fsSL "${KUBECTL_URL}.sha256" | awk '{print $1}')"
verify "$WORK/kubectl" "$KUBECTL_EXPECT" "kubectl $KUBECTL_VERSION"
install -m 0755 "$WORK/kubectl" "$TOOL_BIN_DIR/kubectl"

# ------------------------------------------------------------------- helm --
log "--- helm $HELM_VERSION ---"
HELM_TGZ="helm-${HELM_VERSION}-linux-amd64.tar.gz"
curl -fsSL -o "$WORK/$HELM_TGZ" "https://get.helm.sh/${HELM_TGZ}"
HELM_EXPECT="$(curl -fsSL "https://get.helm.sh/${HELM_TGZ}.sha256sum" | awk '{print $1}')"
verify "$WORK/$HELM_TGZ" "$HELM_EXPECT" "helm $HELM_VERSION"
tar -xzf "$WORK/$HELM_TGZ" -C "$WORK" linux-amd64/helm
install -m 0755 "$WORK/linux-amd64/helm" "$TOOL_BIN_DIR/helm"

# ------------------------------------------------------- record what landed --
log ""
log "=== installed versions ==="
{
  echo "# versions actually installed — run_id=$RUN_ID"
  echo "# generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  printf 'kind\t%s\t%s\n'    "$(first_line "$TOOL_BIN_DIR/kind" version)"           "$KIND_EXPECT"
  printf 'kubectl\t%s\t%s\n' "$(first_line "$TOOL_BIN_DIR/kubectl" version --client)" "$KUBECTL_EXPECT"
  printf 'helm\t%s\t%s\n'    "$(first_line "$TOOL_BIN_DIR/helm" version --short)"    "$HELM_EXPECT"
  echo
  echo "# locked but NOT installed here (operator-executed, needs sudo):"
  echo "docker_engine_target	$DOCKER_ENGINE_VERSION"
  echo "kind_node_image	$KIND_NODE_IMAGE"
} | tee "$DEPLOY_EV/versions-$RUN_ID.txt" | tee -a "$LOG"

log ""
log "NOTE: ensure $TOOL_BIN_DIR is on PATH."
log "done."
