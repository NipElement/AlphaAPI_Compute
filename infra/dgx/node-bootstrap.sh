#!/usr/bin/env bash
# ============================================================================
# node-bootstrap.sh — the per-node host layer, Day-0 steps 2 and 4 (worker half)
#
# Runs ON a delivered node (DGX or head), as root. Idempotent: every step
# checks before it changes, so re-running after a partial failure is safe and
# is the intended recovery path. Each step prints what it verified.
#
# What it does NOT do: install the NVIDIA driver (DGX OS ships it — see
# operators/gpu-operator-values.yaml), touch the RAID layout, or join the
# cluster (kubeadm join is printed by the head node and pasted by a human,
# on purpose: the join token is a credential and this script must never
# embed one).
#
#   sudo ./node-bootstrap.sh gpu      # a DGX B300 worker
#   sudo ./node-bootstrap.sh head     # the control-plane host
# ============================================================================
set -euo pipefail
ROLE="${1:?usage: sudo env KUBE_VERSION=vX.Y.Z [REGISTRY_MIRROR=host:port] node-bootstrap.sh gpu|head}"
[[ "$ROLE" == gpu || "$ROLE" == head ]] || { echo "role must be gpu or head"; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 2; }
# Every precondition that can be checked BEFORE touching the host is checked
# here: sudo strips the environment, and failing at step 4c after sysctl /
# swap / containerd were already changed is the wrong place to learn that.
[[ -n "${KUBE_VERSION:-}" ]] || { echo "KUBE_VERSION is unset — run: sudo env KUBE_VERSION=v1.36.2 REGISTRY_MIRROR=<host:port> $0 $ROLE  (values in versions.env)" >&2; exit 2; }
[[ -n "${REGISTRY_MIRROR:-}" ]] || echo "    ! REGISTRY_MIRROR unset: containerd will pull every image from the public registries (fine for a rehearsal, not for the rack)" >&2

say(){ printf '\033[36m==> %s\033[0m\n' "$*"; }
ok(){  printf '    \033[32m✓ %s\033[0m\n' "$*"; }

# --- 1. inventory into evidence -------------------------------------------
say "inventory"
mkdir -p /var/lib/arise/evidence
{
  echo "hostname=$(hostname)"; echo "date=$(date -u +%FT%TZ)"; echo "role=$ROLE"
  echo "os=$(. /etc/os-release && echo "$PRETTY_NAME")"; echo "kernel=$(uname -r)"
  command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version,serial --format=csv,noheader || echo "nvidia-smi=absent"
  command -v ibstat >/dev/null && ibstat | grep -E 'CA|State|Rate' || echo "ibstat=absent"
  lsblk -o NAME,SIZE,TYPE,MOUNTPOINT | grep -E 'raid|nvme|md' || true
} > "/var/lib/arise/evidence/bootstrap-$(date -u +%Y%m%dT%H%M%SZ).txt"
ok "written to /var/lib/arise/evidence/"

# --- 2. kernel + sysctl for kubernetes ------------------------------------
say "kernel modules + sysctl"
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay; modprobe br_netfilter
cat > /etc/sysctl.d/99-kubernetes.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
# inotify: the platform runs many watchers per node; the kind lab hit this
# limit first (scripts/fix-inotify.sh) — set it before it bites on hardware.
fs.inotify.max_user_instances       = 8192
fs.inotify.max_user_watches         = 1048576
EOF
sysctl --system >/dev/null
ok "br_netfilter + ip_forward + inotify limits"

# --- 3. swap off (kubelet refuses to start with swap) ----------------------
say "swap"
swapoff -a
sed -i.bak '/\sswap\s/s/^/#/' /etc/fstab
ok "swap disabled and commented out of fstab"

# --- 4. containerd with systemd cgroups -----------------------------------
say "containerd"
if ! command -v containerd >/dev/null; then
  echo "containerd is not installed. Install the pinned version from versions.env" >&2
  echo "(DGX OS may ship it; check \`containerd --version\` first)." >&2
  exit 1
fi
mkdir -p /etc/containerd
if ! grep -q 'SystemdCgroup = true' /etc/containerd/config.toml 2>/dev/null; then
  containerd config default > /etc/containerd/config.toml
  sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
  systemctl restart containerd
fi
systemctl enable --now containerd >/dev/null
ok "containerd running, SystemdCgroup=true"

# --- 4b. registry mirror for containerd (optional, REGISTRY_MIRROR=host:port)
# The mirror (scripts/registry-mirror.sh) is only a pull path if containerd is
# told to use it; without this every upstream image still pulls from the
# public registries (review 2026-08-27 P1-3). Written per upstream host so
# `image: registry.k8s.io/etcd@sha256:…` resolves to the mirror transparently
# and the digest-pinned references in the manifests stay unchanged.
if [[ -n "${REGISTRY_MIRROR:-}" ]]; then
  say "containerd registry mirror -> $REGISTRY_MIRROR"
  for host in docker.io registry.k8s.io quay.io ghcr.io nvcr.io; do
    install -d "/etc/containerd/certs.d/$host"
    # docker.io's real endpoint is registry-1.docker.io (the fallback when the mirror lacks an image)
    up="$host"; [[ "$host" == docker.io ]] && up=registry-1.docker.io
    cat > "/etc/containerd/certs.d/$host/hosts.toml" <<EOF
server = "https://$up"
[host."http://$REGISTRY_MIRROR/v2/$host"]
  capabilities = ["pull", "resolve"]
  override_path = true
EOF
  done
  # Our OWN images (arise/web, arise/devbox) are addressed directly as
  # $REGISTRY_MIRROR/arise/...: plain HTTP, native path (no /v2/<host> prefix).
  # Review 2026-08-27: the mirror host used to be in the loop above, which sent
  # those pulls to /v2/<mirror>/arise/web (404) and then HTTPS (refused).
  install -d "/etc/containerd/certs.d/$REGISTRY_MIRROR"
  cat > "/etc/containerd/certs.d/$REGISTRY_MIRROR/hosts.toml" <<EOF
server = "http://$REGISTRY_MIRROR"
[host."http://$REGISTRY_MIRROR"]
  capabilities = ["pull", "resolve", "push"]
EOF
  # containerd 1.x: [plugins."io.containerd.grpc.v1.cri".registry] with no
  # config_path; containerd 2.x (version = 3): [plugins.'io.containerd.cri.v1.images'.registry]
  # with config_path = '' already present. Handle both, then PROVE it took —
  # a silent non-match used to print ok with nothing wired (review 2026-08-27).
  if ! grep -qE "config_path = ['\"]/etc/containerd/certs.d['\"]" /etc/containerd/config.toml; then
    sed -i -E "s|(config_path = )''|\1'/etc/containerd/certs.d'|; s|(config_path = )\"\"|\1\"/etc/containerd/certs.d\"|" /etc/containerd/config.toml
    grep -qE "config_path = ['\"]/etc/containerd/certs.d['\"]" /etc/containerd/config.toml || \
      sed -i 's|\[plugins."io.containerd.grpc.v1.cri".registry\]|&\n      config_path = "/etc/containerd/certs.d"|' /etc/containerd/config.toml
  fi
  systemctl restart containerd
  containerd config dump 2>/dev/null | grep -q "/etc/containerd/certs.d" || {
    echo "containerd did not pick up certs.d (config_path); check /etc/containerd/config.toml schema" >&2; exit 1; }
  ok "certs.d/hosts.toml written for docker.io registry.k8s.io quay.io ghcr.io nvcr.io + $REGISTRY_MIRROR itself (plain-HTTP mirror; put TLS in front for anything beyond the rack)"
fi

# --- 4c. kubeadm / kubelet / kubectl at the PINNED version -----------------
# Nothing else in the sequence installs them (review 2026-08-27 P1-6). The
# version comes from versions.env KUBE_VERSION (the minor picks the apt repo);
# needs the pkgs.k8s.io repo reachable — or a local apt mirror at Day-0.
say "kubeadm/kubelet $KUBE_VERSION"
KMINOR="${KUBE_VERSION%.*}"                     # v1.36.2 -> v1.36
if ! command -v kubeadm >/dev/null || [[ "$(kubeadm version -o short 2>/dev/null)" != "$KUBE_VERSION" ]]; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL "https://pkgs.k8s.io/core:/stable:/$KMINOR/deb/Release.key" | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
  echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/$KMINOR/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
  apt-get update -qq
  PKGVER="${KUBE_VERSION#v}-*"
  apt-get install -y -qq "kubelet=$PKGVER" "kubeadm=$PKGVER" "kubectl=$PKGVER"
  apt-mark hold kubelet kubeadm kubectl
fi
systemctl enable kubelet >/dev/null
ok "kubeadm $(kubeadm version -o short) held; kubelet enabled (kubeadm init/join starts it)"

# --- 5. storage sentinel (EVERY node) --------------------------------------
# local-path refuses to provision under a directory without this sentinel
# (platform/overlays/dgx/storage.yaml). GPU nodes: the NVMe RAID at /raid.
# The HEAD node needs it too (review 2026-08-27 P0-1): metering-ledger,
# prometheus-data and alertmanager-data are pinned to the control plane and
# stayed Pending forever without a data path there. Decision D1 owns what the
# head's data disk IS; whatever it is, it must be MOUNTED at /raid (a bind
# mount of a dedicated partition is fine — `mount --bind /data /raid` is a
# mountpoint). The OS disk is refused on both roles.
say "data-path sentinel (/raid)"
if ! mountpoint -q /raid; then
  echo "/raid is NOT a mountpoint. Provisioning would land on the OS disk." >&2
  if [[ "$ROLE" == gpu ]]; then
    echo "Fix the RAID first (DGX OS mounts it at /raid); refusing to plant the sentinel." >&2
  else
    echo "Head node: mount the D1 data disk (or bind-mount its partition) at /raid;" >&2
    echo "without it metering/prometheus/alertmanager PVCs never bind (DGX-09 fails)." >&2
  fi
  exit 1
fi
install -d -m 0755 /raid/arise/volumes
touch /raid/arise/volumes/.raid-verified
ok "/raid is a mountpoint; .raid-verified planted (local-path fails closed without it)"

# --- 6. head node: audit dirs the API server flags point at ---------------
if [[ "$ROLE" == head ]]; then
  say "head-node audit + backup paths"
  install -d -m 0700 /var/log/kubernetes/audit
  install -d -m 0700 /var/lib/arise/etcd-backups
  [[ -f /etc/kubernetes/audit-policy.yaml ]] \
    && ok "audit-policy.yaml present" \
    || echo "    ! copy infra/dgx/audit-policy.yaml to /etc/kubernetes/audit-policy.yaml BEFORE kubeadm init"
fi

# --- 7. sshd hardening (both roles) ---------------------------------------
say "sshd"
install -d /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-arise.conf <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
MaxAuthTries 4
LoginGraceTime 30
EOF
sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
ok "key-only, no root password login"

say "done ($ROLE). Next: kubeadm $( [[ $ROLE == head ]] && echo 'init --config kubeadm-cluster-config.yaml, then: kubectl apply -f platform/vendor/calico-*.yaml; scripts/approve-kubelet-csrs.sh' || echo 'join <printed by the head node>; then on the head: scripts/approve-kubelet-csrs.sh')"
