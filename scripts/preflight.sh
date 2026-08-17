#!/usr/bin/env bash
# ============================================================================
# preflight.sh — plan §6.2 host inventory + §6.5 evidence pack
#
# STRICTLY READ-ONLY. Writes only into evidence/<run_id>/preflight/.
# Every command here is an observation; none mutates host state.
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$(cat "$REPO/.run_id")"
OUT="$REPO/evidence/$RUN_ID/preflight"
mkdir -p "$OUT"

say() { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------- identity --
TOKEN="$(curl -s -X PUT http://169.254.169.254/latest/api/token \
          -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' --max-time 5)"
imds() { curl -s -H "X-aws-ec2-metadata-token: $TOKEN" --max-time 5 \
         "http://169.254.169.254/latest/$1"; }

imds dynamic/instance-identity/document > "$OUT/aws-instance-identity.json"

{
  echo "{"
  echo "  \"instance_id\": \"$(imds meta-data/instance-id)\","
  echo "  \"instance_type\": \"$(imds meta-data/instance-type)\","
  echo "  \"ami_id\": \"$(imds meta-data/ami-id)\","
  echo "  \"availability_zone\": \"$(imds meta-data/placement/availability-zone)\","
  echo "  \"region\": \"$(imds meta-data/placement/region)\","
  echo "  \"private_ipv4\": \"$(imds meta-data/local-ipv4)\","
  echo "  \"public_ipv4\": \"$(imds meta-data/public-ipv4)\","
  echo "  \"security_groups\": \"$(imds meta-data/security-groups | tr '\n' ',')\","
  echo "  \"iam_instance_profile\": \"$(imds meta-data/iam/info | grep -q 404 && echo NONE || echo PRESENT)\""
  echo "}"
} > "$OUT/aws-metadata-summary.json"

# IMDSv2 enforcement is observable from inside: a token-less v1 call must 401.
{
  echo "imdsv1_status_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        http://169.254.169.254/latest/meta-data/instance-id)"
  echo "# 401 => HttpTokens=required (AWS-01 host-side signal)"
  echo "# NOTE: hop-limit cannot be read from inside; needs describe-instances."
} > "$OUT/aws-metadata-options.txt"

# ------------------------------------------------------------- host facts --
{
  say "uname -m";                   uname -m
  say "os-release";                 cat /etc/os-release
  say "kernel";                     uname -a
  say "lscpu";                      lscpu
  say "free -h";                    free -h
  say "df -hT";                     df -hT
  say "df -ih";                     df -ih
  say "lsblk";                      lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
  say "systemd-detect-virt";        systemd-detect-virt
  say "uptime";                     uptime
  say "timedatectl";                timedatectl
  say "chronyc tracking";           chronyc tracking 2>/dev/null || echo "(chrony absent)"
} > "$OUT/host-inventory.txt" 2>&1

say "ports"   ; ss -lntup                       > "$OUT/ports-before.txt" 2>&1
say "routes"  ; { ip route; echo; ip -6 route; } > "$OUT/routes-before.txt" 2>&1
say "docker"  ; { docker info || echo "docker: NOT INSTALLED"; } \
                                                 > "$OUT/docker-before.txt" 2>&1

# ------------------------------------------------- existing business (P0) --
{
  say "mongod service";     systemctl status mongod --no-pager 2>&1 | head -8
  say "mongod processes";   pgrep -a mongod || echo "(none running)"
  say "mongodb data size";  sudo -n du -sh /var/lib/mongodb 2>/dev/null || echo "(needs sudo)"
  say "user crontab";       crontab -l 2>/dev/null || echo "(none)"
  say "listening 27017";    ss -lnt 2>/dev/null | grep ':27017' || echo "(nothing on 27017)"
  say "home projects";      sudo -n du -sh /home/ubuntu/* 2>/dev/null | sort -rh | head -12
} > "$OUT/existing-business.txt" 2>&1

# ---------------------------------------------------------------- SSM (P0) --
{
  say "ssm snap service"; systemctl is-active snap.amazon-ssm-agent.amazon-ssm-agent.service
  say "ssm agent log tail"
  sudo -n tail -30 /var/log/amazon/ssm/amazon-ssm-agent.log 2>/dev/null \
    | grep -Ei 'error|warn' | tail -10 || echo "(log unavailable)"
  echo
  echo "# SSM registration REQUIRES an IAM instance profile. See waiver §3."
} > "$OUT/ssm-status.txt" 2>&1

# ------------------------------------------------------- CIDR overlap (P0) --
{
  echo "host routes:"; ip route | sed 's/^/  /'
  echo
  echo "planned lab CIDRs:"
  echo "  pod_cidr     10.244.0.0/16"
  echo "  service_cidr 10.96.0.0/12"
  echo "  docker0      172.17.0.0/16 (default)"
  echo
  if ip route | grep -qE '(^|via |dev )10\.'; then
    echo "RESULT: CONFLICT — a 10.0.0.0/8 route exists on this host."
  else
    echo "RESULT: no 10.0.0.0/8 route on host; pod/service CIDRs do not overlap."
  fi
  echo "CAVEAT: VPC peering / TGW / VPN routes are NOT visible from inside the"
  echo "        instance. Full PRE-05 closure needs describe-route-tables."
} > "$OUT/cidr-check.txt" 2>&1

# ------------------------------------------------------------ egress (P1) --
{
  for h in registry-1.docker.io github.com pkgs.k8s.io get.helm.sh \
           download.docker.com raw.githubusercontent.com; do
    printf '%-28s %s\n' "$h" \
      "$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$h")"
  done
} > "$OUT/egress-443.txt" 2>&1

echo "preflight evidence written to $OUT"
ls -1 "$OUT"
