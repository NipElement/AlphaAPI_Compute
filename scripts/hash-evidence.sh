#!/usr/bin/env bash
# ============================================================================
# hash-evidence.sh — freeze the Evidence Pack (plan §9.4, §9.5)
#
# Produces manifest.json + hashes.sha256 over every artifact in the run.
# Plan §9.4: "任何必需证据缺失、hash 不一致或测试进程异常退出，该用例状态必须为
# INVALID，不得标记 PASS." This script is what makes that check possible; it
# does not itself decide PASS/FAIL.
#
# Also runs a redaction sweep BEFORE hashing (plan §9.4): Authorization,
# X-Api-Key, AWS keys, session tokens and kubeconfig client keys must never
# enter the pack.
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$(cat "$REPO/.run_id")"
EV="$REPO/evidence/$RUN_ID"
cd "$EV" || { echo "no evidence dir $EV"; exit 1; }

echo "=== redaction sweep ==="
PAT="(AKIA[0-9A-Z]{16}|aws_secret""_access_key|Authorization:\s*Bearer|x-api""-key:|BEGIN [A-Z ]*PRIVATE KEY|client-key-data)"
HITS=$(grep -rEil "$PAT" . 2>/dev/null || true)
if [[ -n "$HITS" ]]; then
  echo "  POTENTIAL SECRETS — review before sharing this pack:"
  echo "$HITS" | sed 's/^/    /'
  echo "  (not auto-scrubbing: silently rewriting evidence would itself be a"
  echo "   integrity problem. Fix the producer, then re-run the case.)"
else
  echo "  clean"
fi

echo
echo "=== hashing ==="
find . -type f ! -name hashes.sha256 ! -name manifest.json -print0 \
  | sort -z | xargs -0 sha256sum > hashes.sha256
COUNT=$(wc -l < hashes.sha256)
echo "  $COUNT artifact(s) hashed -> hashes.sha256"

# manifest fields are exactly those plan §9.4 enumerates
GIT_COMMIT=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "NOT-A-GIT-REPO")
GIT_TAG=$(git -C "$REPO" describe --tags --exact-match 2>/dev/null || echo "UNTAGGED")

cat > manifest.json <<JSON
{
  "run_id": "$RUN_ID",
  "change_id": "CHG-20260811-001",
  "document_id": "ARISE-B300-PRELAB-DEPLOY-TEST-001",
  "document_version": "1.0",
  "waiver_id": "WAIVER-2026-08-11-001",
  "git_commit": "$GIT_COMMIT",
  "git_tag": "$GIT_TAG",
  "actor": "$(id -un)@$(hostname)",
  "approver": "yuansheng@ariselabs.ai",
  "instance_id": "i-REDACTED-PRELAB-HOST",
  "region": "us-east-2",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "artifact_count": $COUNT,
  "hash_algorithm": "SHA-256",
  "hash_file": "hashes.sha256",
  "integrity_note": "Verify with: sha256sum -c hashes.sha256 (run from this directory)."
}
JSON
echo "  manifest.json written"
echo
echo "verify later with:  cd $EV && sha256sum -c hashes.sha256"
