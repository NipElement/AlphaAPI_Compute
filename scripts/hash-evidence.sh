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
#
# TWO MODES (2026-09-01):
#   hash-evidence.sh            seal the current campaign
#   hash-evidence.sh verify     re-check a sealed pack and FAIL on any drift
#
# The verify mode exists because sealing without checking is theatre. Measured
# on 2026-09-01: the pack on disk had been sealed on 2026-08-17 and then
# overwritten by every run for two weeks — 85 of its 190 recorded hashes no
# longer matched and 71 files were present that the manifest never listed. The
# rule this script quotes ("hash 不一致 … 必须为 INVALID") had been violated
# continuously and nothing looked. Re-sealing on top of that would have laundered
# it, so sealing an already-sealed pack now requires --reseal and records what
# it supersedes.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="${RUN_ID:-$(cat "$REPO/.run_id" 2>/dev/null)}"
[[ -n "$RUN_ID" ]] || { echo "no .run_id — nothing has been run yet"; exit 1; }
EV="$REPO/evidence/$RUN_ID"
cd "$EV" || { echo "no evidence dir $EV"; exit 1; }

if [[ "${1:-}" == "verify" ]]; then
  [[ -f hashes.sha256 ]] || { echo "$RUN_ID is NOT SEALED (no hashes.sha256) — \
seal it with 'make evidence-seal' before treating it as evidence"; exit 2; }
  # The exit status, not a subset of diagnostic strings, decides integrity.
  # This catches malformed/empty manifests and unreadable files as well.
  if ! sha256sum -c hashes.sha256 > /dev/null 2>&1; then
    echo "EVIDENCE PACK DOES NOT VERIFY: a hash is invalid, changed, missing or unreadable" >&2
    exit 1
  fi
  LISTED=$(wc -l < hashes.sha256)
  HAVE=$(find . -type f ! -name hashes.sha256 ! -name manifest.json | wc -l)
  if [[ "$HAVE" -ne "$LISTED" ]]; then
    echo "EVIDENCE PACK DOES NOT VERIFY: $LISTED sealed files, $HAVE present (unsealed additions or duplicate entries)" >&2
    exit 1
  fi
  echo "$RUN_ID: all $LISTED artifacts match, no unsealed additions"
  echo "evidence-verify: PASS"
  exit 0
fi

if [[ -f manifest.json && "${1:-}" != "--reseal" ]]; then
  echo "$RUN_ID is already sealed. Re-sealing would bless whatever has been \
written over it since. Verify it (scripts/hash-evidence.sh verify), or start a \
new campaign, or pass --reseal deliberately." >&2
  exit 3
fi
SUPERSEDES=""
if [[ -f manifest.json ]]; then
  SUPERSEDES="$(sha256sum manifest.json | cut -d" " -f1)"
  echo "re-sealing; superseding manifest $SUPERSEDES"
fi

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

# The seal must describe the run it is sealing, not the lab. These were
# hardcoded PRELAB/EC2 literals until 2026-09-01, so sealing a hardware
# acceptance pack would have stamped it with the prelab document id and this
# shared dev box's instance id (2026-09-01).
case "$RUN_ID" in
  RUN-dgx-*) DOC_ID="ARISE-B300-DGX-ACCEPTANCE-001"; HOST_ID="${ARISE_HOST_ID:-dgx-head-node}" ;;
  *)         DOC_ID="ARISE-B300-PRELAB-DEPLOY-TEST-001"; HOST_ID="${ARISE_HOST_ID:-i-REDACTED-PRELAB-HOST}" ;;
esac

cat > manifest.json <<JSON
{
  "run_id": "$RUN_ID",
  "change_id": "$RUN_ID",
  "document_id": "$DOC_ID",
  "document_version": "1.0",
  "supersedes_manifest_sha256": "$SUPERSEDES",
  "git_commit": "$GIT_COMMIT",
  "git_tag": "$GIT_TAG",
  "actor": "$(id -un)@$(hostname)",
  "approval_status": "not_recorded",
  "host": "$HOST_ID",
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
