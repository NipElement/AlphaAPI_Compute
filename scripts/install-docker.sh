#!/usr/bin/env bash
# ============================================================================
# install-docker.sh — THE ONLY STEP THAT REQUIRES sudo.
#
# Intended to be reviewed and executed BY THE OPERATOR, not by an automation
# agent (WAIVER-2026-08-11-001, control C-2).
#
#   ./scripts/install-docker.sh plan    # simulate only, changes NOTHING
#   ./scripts/install-docker.sh apply   # perform the install
#
# Safety properties (each is asserted, not assumed):
#   1. Refuses to run if ANY conflicting package is installed. It will report
#      them and stop. It NEVER removes a package. (plan §5.3, waiver C-7)
#   2. Pins the exact apt version for DOCKER_ENGINE_VERSION. If that version is
#      not offered by the repo it STOPS rather than installing something else.
#   3. Constrains Docker's address pools away from 172.31.0.0/16. Docker's
#      built-in default pool spans 172.17.0.0/16..172.31.0.0/16, which OVERLAPS
#      this VPC subnet 172.31.16.0/20. SSH is the only management channel on
#      this host (SSM cannot register) and there is no EBS snapshot, so a
#      network collision there is unrecoverable. This is a hard requirement.
#   4. Enables json-file log rotation (10m x 5) BEFORE the daemon first starts.
#   5. Publishes no ports and touches no firewall rule beyond what the docker
#      package installs itself.
#   6. Brackets the whole operation with scripts/guard.sh.
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"
RUN_ID="$(cat "$REPO/.run_id")"
EV="$REPO/evidence/$RUN_ID/deploy"
mkdir -p "$EV"

MODE="${1:-plan}"
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
ARCH="$(dpkg --print-architecture)"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

CONFLICTS=(docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc)

# ---------------------------------------------------------------- step 0 ---
echo "=== step 0: guard ==="
"$REPO/scripts/guard.sh" check || { red "guard failed — aborting"; exit 1; }

# ---------------------------------------------------------------- step 1 ---
echo
echo "=== step 1: conflicting packages (never auto-removed) ==="
found=0
for p in "${CONFLICTS[@]}"; do
  if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
    red "  CONFLICT INSTALLED: $p"; found=1
  fi
done
if (( found )); then
  red "Conflicting packages are present. This script will NOT remove them."
  red "Resolve manually with the package owner, then re-run. (plan §5.3)"
  exit 1
fi
grn "  none installed — install is a pure addition"

# ---------------------------------------------------------------- step 2 ---
echo
echo "=== step 2: official docker apt repository ==="
KEYRING=/etc/apt/keyrings/docker.asc
LIST=/etc/apt/sources.list.d/docker.list
REPO_LINE="deb [arch=${ARCH} signed-by=${KEYRING}] https://download.docker.com/linux/ubuntu ${CODENAME} stable"

if [[ "$MODE" == "plan" ]]; then
  echo "  would create : $KEYRING  (from https://download.docker.com/linux/ubuntu/gpg)"
  echo "  would create : $LIST"
  echo "  content      : $REPO_LINE"
else
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$KEYRING"
  sudo chmod a+r "$KEYRING"
  echo "$REPO_LINE" | sudo tee "$LIST" > /dev/null
  sudo apt-get update -qq
  grn "  repository configured"
fi

# ---------------------------------------------------------------- step 3 ---
echo
echo "=== step 3: resolve exact pinned version for ${DOCKER_ENGINE_VERSION} ==="
if [[ "$MODE" == "plan" && ! -f "$LIST" ]]; then
  ylw "  repository not configured yet; cannot resolve until 'apply'."
  ylw "  Re-run 'plan' after step 2, or inspect with:"
  echo "      apt-cache madison docker-ce | grep ${DOCKER_ENGINE_VERSION}"
  PKG_VER="<resolved-at-apply-time>"
else
  # NOTE: do NOT pipe apt-cache into an awk that exits early. awk's `exit`
  # closes the pipe, apt-cache takes SIGPIPE (141), `pipefail` propagates it
  # and `set -e` kills this script with no diagnostic at all. That is exactly
  # what happened on the first apply attempt on 2026-08-11. Capture first,
  # then match against a here-string — no pipe, no signal.
  MADISON="$(apt-cache madison docker-ce 2>/dev/null || true)"
  # `index($3, v "-")` is a literal substring match anchored on the trailing
  # dash, so "29.6.1" cannot accidentally match a future "29.6.10".
  PKG_VER="$(awk -v v="$DOCKER_ENGINE_VERSION" \
               'index($3, v "-") > 0 {print $3; exit}' <<<"$MADISON")"
  if [[ -z "$PKG_VER" ]]; then
    red "  Docker Engine ${DOCKER_ENGINE_VERSION} is NOT offered by the repo."
    red "  Refusing to install a different version (plan §4.1 version lock)."
    red "  Available:"; printf '%s\n' "$MADISON" | head -10
    exit 1
  fi
  grn "  resolved: $PKG_VER"
fi

PKGS=(
  "docker-ce=${PKG_VER}"
  "docker-ce-cli=${PKG_VER}"
  "containerd.io"
  "docker-buildx-plugin"
  "docker-compose-plugin"
)

# ---------------------------------------------------------------- step 4 ---
echo
echo "=== step 4: daemon.json (written BEFORE first daemon start) ==="
read -r -d '' DAEMON_JSON <<'JSON' || true
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": false,
  "bip": "172.20.0.1/16",
  "default-address-pools": [
    { "base": "172.21.0.0/16", "size": 24 }
  ]
}
JSON
echo "$DAEMON_JSON"
echo
echo "  rationale for bip/default-address-pools:"
echo "    Docker's built-in pool spans 172.17.0.0/16..172.31.0.0/16."
echo "    This VPC subnet is 172.31.16.0/20 -> OVERLAP."
echo "    SSH is the only management channel and there is no EBS snapshot."
echo "    Pools are therefore pinned to 172.20/16 (docker0) + 172.21/16 (networks)."

# ---------------------------------------------------------------- step 5 ---
echo
echo "=== step 5: install ==="
if [[ "$MODE" == "plan" ]]; then
  echo "  simulation (apt-get -s install), nothing will change:"
  if [[ -f "$LIST" ]]; then
    sudo apt-get -s install -y "${PKGS[@]}" 2>&1 | sed 's/^/    /'
  else
    echo "    (repo not configured; simulation unavailable in plan mode)"
    printf '    would install: %s\n' "${PKGS[@]}"
  fi
  echo
  ylw "PLAN MODE — no changes were made. Re-run with 'apply' to execute."
  exit 0
fi

sudo install -m 0755 -d /etc/docker
echo "$DAEMON_JSON" | sudo tee /etc/docker/daemon.json > /dev/null

sudo apt-get install -y "${PKGS[@]}" 2>&1 | tee "$EV/docker-apt-install.log"

# hold the pinned versions so unattended-upgrades cannot drift them
sudo apt-mark hold docker-ce docker-ce-cli | tee -a "$EV/docker-apt-install.log"

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

# ---------------------------------------------------------------- step 6 ---
echo
echo "=== step 6: verify ==="
{
  echo "# docker install evidence — run_id=$RUN_ID  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "## docker version";        sudo docker version
  echo; echo "## containerd";      containerd --version 2>/dev/null || true
  echo; echo "## daemon.json";     sudo cat /etc/docker/daemon.json
  echo; echo "## docker info";     sudo docker info
  echo; echo "## networks";        sudo docker network ls
  echo; echo "## docker0 address"; ip -4 addr show docker0 2>/dev/null || echo "(docker0 not up yet)"
  echo; echo "## host routes after install"; ip route
} > "$EV/docker-after.txt" 2>&1

echo
echo "--- CIDR non-overlap assertion ---"
if ip route | grep -E 'docker|br-' | grep -qE '172\.31\.'; then
  red "FAIL: a docker network landed inside 172.31.0.0/16 (VPC range)."
  red "      Stop now and remove that network before proceeding."
  exit 1
fi
grn "OK: no docker network overlaps 172.31.0.0/16"

echo
"$REPO/scripts/guard.sh" check

grn "docker installed. Log out and back in (or: newgrp docker) for group membership."
grn "evidence: $EV/docker-after.txt"
