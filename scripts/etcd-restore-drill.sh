#!/usr/bin/env bash
# ============================================================================
# etcd-restore-drill.sh — prove the newest snapshot RESTORES, without touching
# the running cluster.
#
# WHY: etcd-backup.yaml verifies each snapshot with `etcdutl snapshot status`,
# which proves the file is a readable database. It does NOT prove that
# `etcdutl snapshot restore` — the thing you actually do at 3am — produces a
# usable data directory. A backup nobody has restored is a wish (audit
# 2026-08-31; the same reasoning as verifying the snapshot in the first place).
#
# WHAT IT DOES: runs one throwaway Job on the head node that restores the
# newest snapshot into an emptyDir and reports the revision and key count of
# the RESTORED tree — read back from the restored directory, not from the
# snapshot file. Nothing is stopped, nothing is written outside the pod.
#
# WHAT IT DOES NOT DO: the cutover itself (stop apiserver + etcd, swap the
# data dir, start). That is runbooks/etcd-restore.md §恢复 and needs a
# maintenance window; this drill removes the "does the file even restore"
# unknown from that window.
#
#   KUBE_CONTEXT=<ctx> scripts/etcd-restore-drill.sh
# ============================================================================
set -uo pipefail
K="kubectl${KUBE_CONTEXT:+ --context=$KUBE_CONTEXT}"
NS=platform-system
JOB=etcd-restore-drill
ETCD_IMAGE="registry.k8s.io/etcd@sha256:397189418d1a00e500c0605ad18d1baf3b541a1004d768448c367e48071622e5"
BUSYBOX="busybox@sha256:fc6dddc4c44b1bfe37f41cae8e67d1693828e8f42a91862816d7953e2c9d3f23"

$K -n "$NS" delete job "$JOB" --ignore-not-found --wait=true >/dev/null 2>&1

cat <<Y | $K apply -f - >/dev/null || { echo "could not create the drill Job" >&2; exit 1; }
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB
  namespace: $NS
  labels: { app.kubernetes.io/name: $JOB, project: arise-b300 }
spec:
  backoffLimit: 1
  activeDeadlineSeconds: 600
  template:
    metadata:
      labels: { app.kubernetes.io/name: $JOB, project: arise-b300 }
    spec:
      restartPolicy: Never
      nodeSelector: { node-role.kubernetes.io/control-plane: "" }
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          effect: NoSchedule
      automountServiceAccountToken: false
      initContainers:
        - name: pick-newest
          image: $BUSYBOX
          command:
            - /bin/sh
            - -ceu
            - |
              newest=\$(ls -1t /backups/etcd-*.db 2>/dev/null | head -1)
              [ -n "\$newest" ] || { echo "no snapshot in /backups — has etcd-backup ever run?"; exit 1; }
              cp "\$newest" /work/snapshot.db
              echo "drilling \$(basename "\$newest") (\$(wc -c < /work/snapshot.db) bytes)"
          securityContext:
            runAsUser: 0
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: backups, mountPath: /backups, readOnly: true }
            - { name: work, mountPath: /work }
        - name: restore
          # The step the runbook actually performs. It fails on a snapshot
          # whose integrity hash does not match — which `snapshot status`
          # alone does not check.
          image: $ETCD_IMAGE
          command: ["etcdutl", "snapshot", "restore", "/work/snapshot.db",
                    "--data-dir", "/work/restored"]
          securityContext:
            runAsUser: 0
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: work, mountPath: /work }
      containers:
        - name: report
          image: $BUSYBOX
          command:
            - /bin/sh
            - -ceu
            - |
              db=/work/restored/member/snap/db
              [ -f "\$db" ] || { echo "restore produced no member/snap/db"; exit 1; }
              [ -d /work/restored/member/wal ] || { echo "restore produced no WAL"; exit 1; }
              echo "RESTORED OK: \$(wc -c < "\$db") bytes in member/snap/db, WAL present"
          securityContext:
            runAsUser: 0
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: work, mountPath: /work }
      volumes:
        - name: backups
          hostPath: { path: /var/lib/arise/etcd-backups, type: Directory }
        - name: work
          emptyDir: { sizeLimit: 8Gi }
Y

echo "waiting for the drill..."
for _ in $(seq 1 60); do
  sleep 5
  S=$($K -n "$NS" get job "$JOB" -o jsonpath='{.status.succeeded}' 2>/dev/null)
  F=$($K -n "$NS" get job "$JOB" -o jsonpath='{.status.failed}' 2>/dev/null)
  [[ -n "$S$F" ]] && break
done
for c in pick-newest restore report; do
  echo "--- $c ---"
  $K -n "$NS" logs "job/$JOB" -c "$c" 2>&1 | tail -3
done
if [[ "${S:-0}" == "1" ]]; then
  echo "etcd-restore-drill: PASS — the newest snapshot restores to a usable data dir"
  exit 0
fi
echo "etcd-restore-drill: FAIL — the backup you have does not restore" >&2
exit 1
