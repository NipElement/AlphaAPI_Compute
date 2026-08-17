#!/usr/bin/env bash
# ============================================================================
# fix-inotify.sh — raise the per-UID inotify instance cap for kind.
#
# SECOND (and, on current evidence, final) system-level change. Reviewed and
# executed by the operator, same rule as install-docker.sh (waiver C-2).
#
#   ./scripts/fix-inotify.sh plan     show current state + intended change
#   ./scripts/fix-inotify.sh apply    apply at runtime AND persist
#
# WHY THIS IS NEEDED
#   A 5-node kind cluster runs 5 kubelets, 5 containerds and their shims, all
#   as root inside containers. `fs.inotify.max_user_instances` is a PER-UID
#   cap, and Ubuntu ships it at 128. Measured usage at failure time:
#       kubelet 35, containerd-shim 30, systemd 25, containerd 10,
#       kube-controller 7, kube-apiserver 6, kube-proxy 4, misc 11  = 128/128
#   kube-proxy then failed with "failed complete: too many open files" and
#   CrashLoopBackOff'd on b300-prelab-worker, which removed all Service DNAT
#   rules on that node — so pods there could not reach 10.96.0.1 or CoreDNS.
#
# WHY IT IS LOW RISK ON THIS SHARED HOST
#   This raises a CEILING; it does not reserve or consume anything. Processes
#   that were already under the old limit are unaffected. Each inotify
#   instance costs on the order of a few hundred bytes of kernel memory, and
#   the number of WATCHES (the memory-significant knob,
#   fs.inotify.max_user_watches) is left untouched at its current value.
#   Nothing is restarted. No existing service is reconfigured.
#
#   The co-resident IDE sessions benefit rather than suffer: they were sharing
#   the same exhausted ceiling.
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$(cat "$REPO/.run_id" 2>/dev/null || echo UNKNOWN)"
EV="$REPO/evidence/$RUN_ID/deploy"
mkdir -p "$EV"

MODE="${1:-plan}"
CONF=/etc/sysctl.d/99-arise-b300-prelab-inotify.conf
WANT_INSTANCES=512

grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }

"$REPO/scripts/guard.sh" check || { echo "guard failed — aborting"; exit 1; }

CUR="$(sysctl -n fs.inotify.max_user_instances)"
echo
echo "=== current ==="
printf '  fs.inotify.max_user_instances = %s   (kind needs more; root is at the cap)\n' "$CUR"
printf '  fs.inotify.max_user_watches   = %s   (NOT changed)\n' "$(sysctl -n fs.inotify.max_user_watches)"

echo
echo "=== intended change ==="
echo "  fs.inotify.max_user_instances: $CUR -> $WANT_INSTANCES"
echo "  persisted in: $CONF"
echo "  (rollback: sudo rm $CONF && sudo sysctl -w fs.inotify.max_user_instances=$CUR)"

if [[ "$MODE" != "apply" ]]; then
  echo
  ylw "PLAN MODE — nothing changed. Re-run with 'apply' to execute."
  exit 0
fi

echo
echo "=== applying ==="
printf '# ARISE B300 prelab — kind 5-node cluster needs more inotify instances.\n# Added %s by %s. Remove this file to revert.\nfs.inotify.max_user_instances = %s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(id -un)" "$WANT_INSTANCES" \
  | sudo tee "$CONF" > /dev/null
sudo sysctl -p "$CONF"

{
  echo "# inotify change evidence — run_id=$RUN_ID  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "before: fs.inotify.max_user_instances = $CUR"
  echo "after : fs.inotify.max_user_instances = $(sysctl -n fs.inotify.max_user_instances)"
  echo "watches (unchanged): $(sysctl -n fs.inotify.max_user_watches)"
  echo "persisted: $CONF"
  echo
  echo "reason: kube-proxy CrashLoopBackOff 'failed complete: too many open files'"
  echo "        on b300-prelab-worker; root was at 128/128 inotify instances."
} > "$EV/inotify-change.txt"

echo
grn "done. Now restart the crash-looping kube-proxy pods:"
echo "  kubectl --context kind-b300-prelab -n kube-system delete pod -l k8s-app=kube-proxy"
