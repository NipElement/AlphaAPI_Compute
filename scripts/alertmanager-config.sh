#!/usr/bin/env bash
# ============================================================================
# alertmanager-config.sh — generate + apply the dgx Alertmanager config Secret.
#
#   WEBHOOK_URL unset  -> local-sink config (bring-up grade; pages NOBODY, and
#                         verify-dgx WARNs about exactly that)
#   WEBHOOK_URL set    -> P0 fast-path + everything else to that webhook
#                         (PagerDuty Events v2 / Opsgenie / Slack — anything
#                         with an inbound webhook)
#
# The URL arrives via ENV, never argv: argv is world-readable in `ps`, and a
# pager webhook URL is effectively a send-pages credential.
#
# Why a Secret and not the overlay ConfigMap: `make dgx-platform` re-applies
# the overlay, and an overlay-owned config would revert to the null sink on
# every deploy — silently un-paging the fleet. make owns this Secret the same
# way it owns the code ConfigMaps.
#
# Usage:
#   ./scripts/alertmanager-config.sh <kube-context>
#   WEBHOOK_URL=https://... ./scripts/alertmanager-config.sh <kube-context>
# ============================================================================
set -euo pipefail
CTX="${1:?usage: alertmanager-config.sh <kube-context> (WEBHOOK_URL via env)}"
K="kubectl --context $CTX"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

if [[ -n "${WEBHOOK_URL:-}" ]]; then
  case "$WEBHOOK_URL" in
    https://*) ;;
    *) echo "WEBHOOK_URL must be https:// — alerts carry node names and" >&2
       echo "contract state, and a pager URL is a send-pages credential;" >&2
       echo "neither belongs on plaintext HTTP." >&2
       exit 1 ;;
  esac
  RECEIVER=ops-webhook
  cat > "$TMP/alertmanager.yml" <<EOF
global:
  resolve_timeout: 5m
route:
  receiver: ops-webhook
  group_by: ['alertname', 'node']
  group_wait: 10s
  group_interval: 1m
  repeat_interval: 1h
  routes:
    - matchers: [ 'severity="P0"' ]
      receiver: ops-webhook
      group_wait: 0s        # P0 must not wait for grouping
      repeat_interval: 5m
receivers:
  - name: ops-webhook
    webhook_configs:
      - url: '${WEBHOOK_URL}'
        send_resolved: true
EOF
else
  RECEIVER=local-sink
  cat > "$TMP/alertmanager.yml" <<'EOF'
global:
  resolve_timeout: 5m
route:
  receiver: local-sink
  group_by: ['alertname', 'node']
  group_wait: 10s
  group_interval: 1m
  repeat_interval: 1h
  routes:
    - matchers: [ 'severity="P0"' ]
      receiver: local-sink
      group_wait: 0s        # P0 must not wait for grouping
      repeat_interval: 5m
receivers:
  # Pages NOBODY. verify-dgx.sh WARNs while this is the live receiver; wire
  # the real one with:  make dgx-alert-receiver WEBHOOK_URL=https://...
  - name: local-sink
EOF
fi

$K -n monitoring create secret generic alertmanager-config \
  --from-file=alertmanager.yml="$TMP/alertmanager.yml" \
  --dry-run=client -o yaml | $K apply -f - >/dev/null
# Only restart if the deployment exists (dgx-code may run before dgx-platform
# finished rolling monitoring out; the pod then starts with the new Secret).
if $K -n monitoring get deploy alertmanager >/dev/null 2>&1; then
  $K -n monitoring rollout restart deploy/alertmanager >/dev/null
fi
echo "alertmanager-config applied (receiver: $RECEIVER)"
[[ "$RECEIVER" == "local-sink" ]] && \
  echo "NOTE: this receiver pages nobody — set the real one before customers arrive."
exit 0
