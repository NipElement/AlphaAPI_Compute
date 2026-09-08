#!/usr/bin/env bash
# ============================================================================
# verify-dgx.sh — the DGX completion gate (Day-0 step 11).
#
# verify.sh is the LAB gate: it asserts simulated GPUs exist and that real ones
# do NOT. This is its mirror image. Every assertion here is the inverse or the
# hardware equivalent, and the two must never be confused — running the lab
# gate against hardware would "pass" a cluster with no GPUs at all.
#
# What this gate does NOT do: it does not prove NCCL bandwidth, NVLink
# topology, IB link rate or storage throughput. Those are the HW-* acceptance
# matrix, run after this gate, with their own thresholds. This answers the
# narrower question the Day-0 operator needs answered before letting anything
# else run: "is the control plane on this hardware the one we rehearsed?"
#
# USAGE:  KUBE_CONTEXT=arise-dgx ./scripts/verify-dgx.sh
#         GPU_NODES=4 GPU_PER_NODE=8 KUBE_CONTEXT=... ./scripts/verify-dgx.sh
#
# Writes a machine-parsable result next to the lab gate's, so a Day-0 run
# leaves the same kind of evidence a rehearsal does.
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/versions.env"

# On hardware there is no kind context to fall back to: refusing to guess is
# the point, since the default would silently target the prelab cluster.
if [[ -z "${KUBE_CONTEXT:-}" ]]; then
  echo "KUBE_CONTEXT is unset. Refusing to guess — the fallback would be the" >&2
  echo "kind prelab cluster, and 'the hardware gate passed' would be a lie." >&2
  echo "Usage: KUBE_CONTEXT=<dgx context> $0" >&2
  exit 2
fi
CTX="$KUBE_CONTEXT"
K="kubectl --context $CTX"

# The fleet the gate is checking AGAINST comes from versions.env, not from a
# default in this file: with `GPU_NODES="${GPU_NODES:-4}"` anyone (or any
# script) could pass the completion gate on a smaller fleet by exporting a
# smaller number — the gate would still print PASS (falsification audit
# 2026-08-30). An override is still allowed for a pilot, but it is now LOUD
# and it lands in the evidence JSON, so "we passed" can never quietly mean
# "we passed on two nodes".
FLEET_NODES_PINNED="${HW_FLEET_GPU_NODES:?versions.env must set HW_FLEET_GPU_NODES}"
FLEET_GPUS_PINNED="${HW_GPU_PER_NODE:?versions.env must set HW_GPU_PER_NODE}"
GPU_NODES="${GPU_NODES:-$FLEET_NODES_PINNED}"
GPU_PER_NODE="${GPU_PER_NODE:-$FLEET_GPUS_PINNED}"
FLEET_OVERRIDDEN=no
if [[ "$GPU_NODES" != "$FLEET_NODES_PINNED" || "$GPU_PER_NODE" != "$FLEET_GPUS_PINNED" ]]; then
  FLEET_OVERRIDDEN="yes(${GPU_NODES}x${GPU_PER_NODE} instead of ${FLEET_NODES_PINNED}x${FLEET_GPUS_PINNED})"
  printf '\033[33m  !! FLEET SIZE OVERRIDDEN: checking %sx%s, versions.env pins %sx%s.\n     A PASS below is a PASS FOR THAT SMALLER FLEET ONLY.\033[0m\n' \
    "$GPU_NODES" "$GPU_PER_NODE" "$FLEET_NODES_PINNED" "$FLEET_GPUS_PINNED"
fi
EXPECT_TOTAL=$((GPU_NODES * GPU_PER_NODE))
# Under evidence/RUN-*/ so .gitignore covers it: this is a RUN RESULT for one
# cluster at one moment, not source. Archive it out-of-band if a Day-0 run
# needs to be preserved (same rule as the lab evidence packs).
OUT="${VERIFY_DGX_OUT:-$REPO/evidence/RUN-dgx/verify-dgx.json}"

PASS=0; FAIL=0; WARN=0
declare -a RESULTS
chk() {  # chk <id> <description> <condition-exit-code>
  if [[ $3 -eq 0 ]]; then
    printf '  \033[32mPASS\033[0m %-10s %s\n' "$1" "$2"; PASS=$((PASS+1))
    RESULTS+=("{\"id\":\"$1\",\"status\":\"PASS\",\"desc\":\"$2\"}")
  else
    printf '  \033[31mFAIL\033[0m %-10s %s\n' "$1" "$2"; FAIL=$((FAIL+1))
    RESULTS+=("{\"id\":\"$1\",\"status\":\"FAIL\",\"desc\":\"$2\"}")
  fi
}
warn() {  # warn <id> <description>  — not yet installed, not a failure
  printf '  \033[33mWARN\033[0m %-10s %s\n' "$1" "$2"; WARN=$((WARN+1))
  RESULTS+=("{\"id\":\"$1\",\"status\":\"WARN\",\"desc\":\"$2\"}")
}

# A WARN that is correct DURING Day-0 and fatal AFTER it. This gate runs while
# the cluster is still being built, so "GPU Operator not installed yet" must
# not fail it — but three of these mean the platform cannot serve a paying
# customer, and nothing anywhere promoted them (audit 2026-08-31): every alert
# paging a local sink, a devbox image that is still the day0 sentinel, and a
# console SPA that cannot start. LAUNCH=1 is the "we are about to take money"
# run; the README's Day-0 step 12 (cutover) uses it.
LAUNCH="${LAUNCH:-0}"
warn_or_fail() {  # warn_or_fail <id> <description>
  if [[ "$LAUNCH" == "1" ]]; then
    printf '  \033[31mFAIL\033[0m %-10s %s\n' "$1" "$2 [LAUNCH_BLOCKER]"
    FAIL=$((FAIL+1))
    RESULTS+=("{\"id\":\"$1\",\"status\":\"FAIL\",\"launch_blocker\":true,\"desc\":\"$2\"}")
  else
    printf '  \033[33mWARN\033[0m %-10s %s\n' "$1" "$2 (LAUNCH=1 makes this a FAIL)"
    WARN=$((WARN+1))
    RESULTS+=("{\"id\":\"$1\",\"status\":\"WARN\",\"launch_blocker\":true,\"desc\":\"$2\"}")
  fi
}

echo "=== DGX completion gate (context: $CTX) ==="

# --- the cluster ------------------------------------------------------------
# "Ready,SchedulingDisabled" is still Ready: a maintenance-cordoned node must
# not fail the gate (review 2026-08-27).
READY=$($K get nodes --no-headers 2>/dev/null | grep -cE ' Ready(,SchedulingDisabled)? ')
[[ "$READY" -ge $((GPU_NODES + 1)) ]]
chk DGX-01 "at least $((GPU_NODES + 1)) nodes Ready — $GPU_NODES GPU + head (got $READY)" $?

LABELLED=$($K get nodes -l arise.ai/node-id --no-headers 2>/dev/null | wc -l)
[[ "$LABELLED" == "$GPU_NODES" ]]
chk DGX-02 "$GPU_NODES nodes carry arise.ai/node-id (got $LABELLED) — run make dgx-onboard" $?

# --- REAL GPUs are present and complete ------------------------------------
TOTAL=0; PERNODE_OK=1; MISSING=""
for n in $($K get nodes -l arise.ai/node-id --no-headers -o custom-columns=N:.metadata.name 2>/dev/null); do
  c=$($K get node "$n" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null)
  c=${c:-0}
  [[ "$c" == "$GPU_PER_NODE" ]] || { PERNODE_OK=0; MISSING="$MISSING $n=$c"; }
  TOTAL=$((TOTAL + c))
done
[[ $LABELLED -gt 0 && $PERNODE_OK -eq 1 ]]     # zero labelled nodes must not read as "all fine"
chk DGX-03 "every GPU node allocatable nvidia.com/gpu=$GPU_PER_NODE${MISSING:+ (off:$MISSING)}" $?
[[ "$TOTAL" == "$EXPECT_TOTAL" ]]
chk DGX-04 "fleet nvidia.com/gpu=$EXPECT_TOTAL (got $TOTAL)" $?

# A GPU count of zero with everything else green is the failure mode this gate
# exists to catch: the GPU Operator did not install, or the driver did not load,
# and every other assertion would still look fine.
[[ "$TOTAL" -gt 0 ]]
chk DGX-05 "GPUs are actually schedulable (not a driverless cluster)" $?

# --- NO simulated resources anywhere ---------------------------------------
# The mirror of the lab's SEC-03a. A single fake GPU here means the lab overlay
# was applied to hardware — workloads would believe they are in a sandbox while
# running on billable, contractually committed machines.
SIM=$($K get nodes -o json 2>/dev/null | python3 -c "
import json,sys
tot=0
for n in json.load(sys.stdin)['items']:
    for k,v in (n['status'].get('allocatable') or {}).items():
        if k.startswith('arise.dev/'):
            tot += int(v)
print(tot)" 2>/dev/null || echo 0)
[[ "${SIM:-0}" == "0" ]]
chk DGX-06 "zero arise.dev/* simulated capacity on the fleet (got $SIM)" $?

$K get ds -n platform-system fake-gpu-plugin >/dev/null 2>&1 && FAKE_DS=1 || FAKE_DS=0
[[ "$FAKE_DS" == "0" ]]
chk DGX-07 "the simulated device plugin is NOT installed" $?

$K get ns vast-mock >/dev/null 2>&1 && MOCK_NS=1 || MOCK_NS=0
[[ "$MOCK_NS" == "0" ]]
chk DGX-08 "the vast-mock namespace does NOT exist on hardware" $?

# --- the platform itself ----------------------------------------------------
for d in capacity-controller ops-console tenant-portal platform-gateway metering; do
  $K -n platform-system rollout status "deploy/$d" --timeout=120s >/dev/null 2>&1
  chk DGX-09 "platform-system/$d Available" $?
done
# The fleet that PAGES is part of the platform: a gate that passed with
# prometheus/alertmanager Pending (no PVC bound) was green on a cluster that
# alerts nobody (review 2026-08-27 P1-7).
for d in monitoring/prometheus monitoring/alertmanager monitoring/kube-state-metrics \
         storage-system/local-path-provisioner; do
  $K -n "${d%%/*}" rollout status "deploy/${d##*/}" --timeout=120s >/dev/null 2>&1
  chk DGX-09 "$d Available" $?
done
for cj in etcd-backup audit-archive; do
  $K -n platform-system get cronjob "$cj" >/dev/null 2>&1
  chk DGX-09 "CronJob platform-system/$cj present (etcd snapshots / audit archive on /raid)" $?
done
for pvc in platform-system/metering-ledger platform-system/platform-gateway-auth platform-system/platform-auth-backups monitoring/prometheus-data monitoring/alertmanager-data; do
  [[ "$($K -n "${pvc%%/*}" get pvc "${pvc##*/}" -o jsonpath='{.status.phase}' 2>/dev/null)" == "Bound" ]]
  chk DGX-09 "PVC $pvc Bound (head node has its /raid data path)" $?
done

# --- the gates that fence customer contracts --------------------------------
for pol in arise-deny-simulated-gpu arise-tenant-owner-gate \
           arise-tenant-host-isolation arise-queue-binding \
           arise-priority-binding arise-flavor-quantization \
           arise-storage-quantization arise-tenant-suspended; do
  $K get validatingadmissionpolicybinding "$pol" >/dev/null 2>&1
  chk DGX-10 "admission binding $pol present" $?
done

$K get crd nodeownerships.infrastructure.arise.ai >/dev/null 2>&1
chk DGX-11 "NodeOwnership CRD established" $?

# --- posture: the switches that must be right before customers arrive -------
ADAPTER=$($K -n platform-system get deploy capacity-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VAST_ADAPTER")].value}' 2>/dev/null)
[[ "$ADAPTER" == "none" ]]     # the only value the render gate, DGX-13 and VST-06 accept
chk DGX-12 "capacity-controller adapter is 'none'; got '$ADAPTER'" $?

PRODFLAG=$($K -n platform-system get deploy capacity-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VAST_PRODUCTION_ADAPTER_ENABLED")].value}' 2>/dev/null)
[[ "$PRODFLAG" == "false" ]]
chk DGX-13 "production marketplace adapter flag=false (VST-06); got '$PRODFLAG'" $?

GPURES=$($K -n platform-system get deploy capacity-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="FAKE_GPU_RESOURCE")].value}' 2>/dev/null)
[[ "$GPURES" == "nvidia.com/gpu" ]]
chk DGX-14 "drain gate watches nvidia.com/gpu (got '$GPURES')" $?

PUBLIC=$($K -n platform-system get deploy platform-gateway \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="GW_PUBLIC_MODE")].value}' 2>/dev/null)
[[ "$PUBLIC" == "true" ]]
chk DGX-15 "gateway runs in public mode (fail-closed credentials); got '$PUBLIC'" $?

$K -n platform-system get secret platform-gateway-auth >/dev/null 2>&1
chk DGX-16 "Secret platform-gateway-auth exists (make dgx-gateway-secret)" $?

# --- tenant quotas bound to the REAL resource -------------------------------
for ns in tenant-arise tenant-direct; do
  # The VALUE, not just the key: a quota raised to the fleet total bounds
  # nothing (falsification audit 2026-08-30 — both gates tested membership).
  Q=$(EXPECT_TOTAL="$EXPECT_TOTAL" $K -n "$ns" get resourcequota -o json 2>/dev/null | python3 -c "
import json,os,sys
fleet=int(os.environ['EXPECT_TOTAL'])
for i in json.load(sys.stdin).get('items',[]):
    h=(i['spec'].get('hard') or {})
    if 'requests.nvidia.com/gpu' in h:
        n=int(str(h['requests.nvidia.com/gpu']))
        print(f'{n}' if 0 < n < fleet else f'BAD:{n}/{fleet}'); break
else: print('ABSENT')" 2>/dev/null)
  [[ "$Q" =~ ^[0-9]+$ ]]
  chk DGX-17 "$ns GPU quota is a real ceiling below the fleet (got $Q of $EXPECT_TOTAL)" $?
done

# --- storage ----------------------------------------------------------------
for sc in arise-shared arise-longterm; do
  $K get storageclass "$sc" >/dev/null 2>&1
  chk DGX-18 "StorageClass $sc present" $?
done

# --- the tenant register the services actually read -------------------------
$K -n platform-system get configmap platform-tenants >/dev/null 2>&1
chk DGX-19 "ConfigMap platform-tenants present (make dgx-code)" $?

# Three states, each answered honestly: no config at all (FAIL — alertmanager
# cannot even start), config routing to the local sink (WARN — legal at
# bring-up, illegal at launch: every alert pages NOBODY), config routing to a
# real receiver (PASS). The earlier cut collapsed "no config" into "real
# receiver" because grep -c over empty input is 0 — a false pass on the worst
# of the three states.
if ! $K -n monitoring get secret alertmanager-config >/dev/null 2>&1; then
  chk DGX-21 "Secret alertmanager-config exists (make dgx-code seeds it)" 1
  chk DGX-22 "Alertmanager receiver unknowable — no config Secret" 1
else
  chk DGX-21 "Secret alertmanager-config exists" 0
  AMCFG=$($K -n monitoring get secret alertmanager-config \
    -o jsonpath='{.data.alertmanager\.yml}' 2>/dev/null | base64 -d 2>/dev/null)
  if printf '%s' "$AMCFG" | grep -q "name: local-sink"; then
    warn_or_fail DGX-22 "Alertmanager routes to the LOCAL SINK — every alert pages NOBODY. Wire it: make dgx-alert-receiver WEBHOOK_URL=https://..."
  else
    chk DGX-22 "Alertmanager routes to a real receiver" 0
  fi
fi

# --- every tenant namespace carries the HARDWARE egress fence ---------------
# Onboarding a third tenant with the lab list would open 10/8 and 192.168/16
# to it — node IPs, BMCs, the API server (review 2026-08-27 P1-2). The label
# is the rule; the list is checked on each namespace that carries it.
for ns in $($K get ns -l arise.ai/tier=tenant -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  EXC=$($K -n "$ns" get networkpolicy deny-imds-and-host-links -o jsonpath='{.spec.egress[0].to[0].ipBlock.except[*]}' 2>/dev/null)
  ok=0; for c in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
    printf '%s\n' $EXC | grep -qx "$c" || ok=1; done
  chk DGX-25 "$ns egress denies every private range (got: ${EXC:-<no policy>})" $ok
done

# --- edge and gateway flags move together --------------------------------
# Three honest states: no edge + flags false (bring-up, PASS); edge + flags
# true (cutover, PASS); anything mixed (FAIL — shared-IP lockout or spoofable
# X-Forwarded-For). make dgx-edge / dgx-edge-off keep them paired.
EDGE_REPLICAS=$($K -n edge-system get deploy platform-edge -o jsonpath='{.spec.replicas}' 2>/dev/null)
EDGE=0
[[ "${EDGE_REPLICAS:-0}" -gt 0 ]] && EDGE=1
TP=$($K -n platform-system get deploy platform-gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="GW_TRUST_PROXY")].value}' 2>/dev/null)
CS=$($K -n platform-system get deploy platform-gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="GW_COOKIE_SECURE")].value}' 2>/dev/null)
if [[ "$EDGE" == 1 ]]; then [[ "$TP" == "true" && "$CS" == "true" ]]; else [[ "$TP" == "false" && "$CS" == "false" ]]; fi
chk DGX-26 "edge($EDGE) and gateway flags (trust_proxy=$TP cookie_secure=$CS) are paired" $?

# A running edge pod does not prove ACME, DNS or the external route works.
if [[ "$EDGE" == 1 ]]; then
  $K -n edge-system rollout status deploy/platform-edge --timeout=60s >/dev/null 2>&1
  chk DGX-26a "Caddy public edge is Available" $?
  FQDN=$($K -n edge-system get cm platform-edge-settings -o jsonpath='{.data.CONSOLE_FQDN}' 2>/dev/null)
  if [[ "$FQDN" =~ ^[a-z0-9][a-z0-9.-]+\.[a-z]{2,63}$ ]]; then
    curl --fail --silent --show-error --connect-timeout 5 --max-time 15 \
      "https://$FQDN/readyz" >/dev/null 2>&1
    chk DGX-26b "public HTTPS readiness succeeds with trusted certificate validation ($FQDN)" $?
  else
    chk DGX-26b "public edge has no valid configured FQDN" 1
  fi
fi

# --- kubelet serving certs approved ---------------------------------------
PENDING=$($K get csr -o jsonpath='{range .items[?(@.spec.signerName=="kubernetes.io/kubelet-serving")]}{.metadata.name} {.status.conditions[*].type}{"\n"}{end}' 2>/dev/null | awk 'NF==1' | wc -l)
[[ "$PENDING" == 0 ]]
chk DGX-27 "no pending kubelet-serving CSRs (got $PENDING; scripts/approve-kubelet-csrs.sh)" $?

# --- the tenant->platform fence, EMPIRICALLY under the real CNI -------------
# A NetworkPolicy object is not evidence of enforcement (networkpolicies.yaml
# header). One probe from a tenant pod to the portal's pod IP must FAIL; a
# CNI that does not enforce policy makes this succeed and the gate red.
PORTAL_IP=$($K -n platform-system get pod -l app.kubernetes.io/name=tenant-portal -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)
if [[ -n "$PORTAL_IP" ]]; then
  # A throw-away tenant-tier namespace with NO egress policy: the tenant
  # overlays' own egress deny would block the probe first and prove nothing
  # about platform-internal-ingress (the fence this check is named for).
  $K delete ns dgx-fence-probe --ignore-not-found --wait=true >/dev/null 2>&1
  $K create ns dgx-fence-probe >/dev/null 2>&1
  $K label ns dgx-fence-probe arise.ai/tier=tenant pod-security.kubernetes.io/enforce=restricted --overwrite >/dev/null 2>&1
  PROBE=$($K -n dgx-fence-probe run dgx-fence-probe --restart=Never --rm -i --quiet --timeout=60s \
    --image="$WEB_BASE_IMAGE" --overrides='{"spec":{"automountServiceAccountToken":false,"securityContext":{"runAsNonRoot":true,"runAsUser":65532,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"p","image":"'"$WEB_BASE_IMAGE"'","command":["sh","-c","wget -q -T 5 -O- http://'"$PORTAL_IP"':8080/healthz >/dev/null 2>&1 && echo OPEN || echo BLOCKED"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},"resources":{"requests":{"cpu":"500m","memory":"512Mi"},"limits":{"cpu":"500m","memory":"512Mi"}}}]}}' 2>/dev/null | tail -1)
  $K delete ns dgx-fence-probe --ignore-not-found --wait=false >/dev/null 2>&1
  [[ "$PROBE" == "BLOCKED" ]]
  chk DGX-28 "unlisted tenant-tier ns -> tenant-portal pod IP is BLOCKED by platform-internal-ingress under the real CNI (got '${PROBE:-no result}')" $?
else
  chk DGX-28 "tenant->portal fence probe (portal pod IP unknown)" 1
fi

# --- Volcano + CNI: what make dgx-test stands on ------------------------------
for d in volcano-scheduler volcano-admission volcano-controllers; do
  $K -n volcano-system rollout status "deploy/$d" --timeout=60s >/dev/null 2>&1
  chk DGX-29 "volcano-system/$d Available" $?
  NODE=$($K -n volcano-system get pod -l "app=$d" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)
  [[ -n "$NODE" && "$($K get node "$NODE" -o jsonpath='{.metadata.labels.node-role\.kubernetes\.io/control-plane}' 2>/dev/null)" != "<no value>" ]] \
    && $K get node "$NODE" -o jsonpath='{.metadata.labels}' 2>/dev/null | grep -q 'node-role.kubernetes.io/control-plane'
  chk DGX-29 "volcano-system/$d runs on the head node, not a sellable GPU node (on '${NODE:-?}')" $?
done
for q in arise-internal direct-customer system; do
  $K get queue "$q" >/dev/null 2>&1
  chk DGX-29 "Volcano queue $q present" $?
done
CALICO_DESIRED=$($K -n kube-system get ds calico-node -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null)
CALICO_READY=$($K -n kube-system get ds calico-node -o jsonpath='{.status.numberReady}' 2>/dev/null)
[[ -n "$CALICO_DESIRED" && "$CALICO_DESIRED" == "$CALICO_READY" && "$CALICO_READY" -ge $((GPU_NODES + 1)) ]]
chk DGX-30 "calico-node ready on every node (ready $CALICO_READY / desired ${CALICO_DESIRED:-?})" $?

# --- LOADED == DESIRED: a mounted config is not a running config -----------
# 2026-08-30: the lab ran for 17 days with a prometheus that had never loaded
# the metering scrape its ConfigMap declared. Every rule and every job below
# is a promise the running process has to actually hold.
PROM_JOBS_CM=$($K -n monitoring get cm prometheus-config -o jsonpath='{.data.prometheus\.yml}' 2>/dev/null | grep -oE 'job_name: [^ ]+' | awk '{print $2}' | sort -u)
PROM_JOBS_LIVE=$($K -n monitoring exec deploy/prometheus -- wget -qO- 'http://127.0.0.1:9090/api/v1/targets?state=any' 2>/dev/null \
  | python3 -c "import json,sys; print('\n'.join(sorted({t['labels'].get('job','') for t in json.load(sys.stdin)['data']['activeTargets']})))" 2>/dev/null)
MISSING_JOBS=""
for j in $PROM_JOBS_CM; do printf '%s\n' $PROM_JOBS_LIVE | grep -qx "$j" || MISSING_JOBS="$MISSING_JOBS $j"; done
[[ -z "$MISSING_JOBS" && -n "$PROM_JOBS_CM" ]]
chk DGX-33 "every scrape job in the ConfigMap is LIVE in prometheus (missing:${MISSING_JOBS:- none})" $?

PROM_GROUPS_CM=$($K -n monitoring get cm prometheus-rules -o jsonpath='{.data}' 2>/dev/null \
  | python3 -c "
import json,sys,yaml
d=json.load(sys.stdin); out=[]
for v in d.values():
    try: out += [g['name'] for g in (yaml.safe_load(v) or {}).get('groups',[])]
    except Exception: pass
print('\n'.join(sorted(set(out))))" 2>/dev/null)
PROM_GROUPS_LIVE=$($K -n monitoring exec deploy/prometheus -- wget -qO- 'http://127.0.0.1:9090/api/v1/rules' 2>/dev/null \
  | python3 -c "import json,sys; print('\n'.join(sorted({g['name'] for g in json.load(sys.stdin)['data']['groups']})))" 2>/dev/null)
MISSING_GROUPS=""
for g in $PROM_GROUPS_CM; do printf '%s\n' $PROM_GROUPS_LIVE | grep -qx "$g" || MISSING_GROUPS="$MISSING_GROUPS $g"; done
[[ -z "$MISSING_GROUPS" && -n "$PROM_GROUPS_CM" ]]
chk DGX-33 "every alert rule group in the ConfigMap is LOADED (missing:${MISSING_GROUPS:- none})" $?

AM_LIVE=$($K -n monitoring exec deploy/alertmanager -- wget -qO- 'http://127.0.0.1:9093/api/v2/status' 2>/dev/null \
  | python3 -c "import json,sys,hashlib; print(hashlib.sha256(json.load(sys.stdin)['config']['original'].encode()).hexdigest()[:16])" 2>/dev/null)
AM_CM=$($K -n monitoring get secret alertmanager-config -o jsonpath='{.data.alertmanager\.yml}' 2>/dev/null \
  | base64 -d 2>/dev/null | python3 -c "import sys,hashlib; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:16])" 2>/dev/null)
if $K -n monitoring get secret alertmanager-config >/dev/null 2>&1; then
  # Only meaningful once the Secret exists; whether it exists at all is DGX-21's
  # job, and reporting "config differs" for "no config yet" would be a lie.
  [[ -n "$AM_LIVE" && "$AM_LIVE" == "$AM_CM" ]]
  chk DGX-33 "alertmanager is running the config in its Secret (live=$AM_LIVE secret=$AM_CM)" $?
else
  warn DGX-33 "no alertmanager-config Secret yet — DGX-21 owns that; nothing to compare"
fi

# --- RUNNING code == the code ConfigMap -----------------------------------
# `make dgx-code` writes ConfigMaps; the pods load their file at start. A pod
# still running yesterday's code while the gate is green is the same class of
# lie as the one above — and the next unrelated restart activates unreviewed code.
CODE_DRIFT=""
for pair in "capacity-controller:capacity_controller.py" "ops-console:console.py" \
            "tenant-portal:tenant_portal.py" "platform-gateway:gateway.py" "metering:metering.py"; do
  d="${pair%%:*}"; f="${pair##*:}"
  cm=$($K -n platform-system get cm "$d-code" -o go-template="{{index .data \"$f\"}}" 2>/dev/null | sha256sum | cut -c1-12)
  live=$($K -n platform-system exec "deploy/$d" -- python3 -c "
import hashlib,sys
print(hashlib.sha256(open('/app/$f','rb').read()).hexdigest()[:12])" 2>/dev/null)
  [[ -n "$live" && "$cm" == "$live" ]] || CODE_DRIFT="$CODE_DRIFT $d(cm=$cm run=${live:-?})"
done
[[ -z "$CODE_DRIFT" ]]
chk DGX-34 "every platform pod RUNS the code in its ConfigMap (drift:${CODE_DRIFT:- none})" $?

# --- the money is actually priced ------------------------------------------
# Metering records usage whatever the price book says; an invoice run against a
# window with no effective rate yields NOT PRICED lines and $0 (exit 2). Going
# live before the rate starts is free GPUs — caught here, not at month end.
PRICE_OK=$(REPO="$REPO" python3 - <<'PY' 2>/dev/null
import os, time, yaml
repo = os.environ["REPO"]
book = yaml.safe_load(open(f"{repo}/billing/pricebook.yaml"))
reg = yaml.safe_load(open(f"{repo}/platform/tenants.yaml"))
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
kinds = sorted({t["kind"] for t in reg["spec"]["tenants"]})     # the register's own field, not the doc kind
missing = [k for k in kinds
           if not [s for s in book["skus"]
                   if k in s.get("tenant_kinds", []) and s["effective_from"] <= now
                   and s["sku"].startswith("gpu-hour")]]
print("MISSING:" + ",".join(missing) if missing else "OK(" + ",".join(kinds) + ")")
PY
)
[[ "$PRICE_OK" == OK* ]]
chk DGX-35 "the price book has a gpu-hour rate IN FORCE today for every tenant kind ($PRICE_OK)" $?

# --- the ledger has exactly one writer -------------------------------------
# Two of these are single-writer BY CONSTRUCTION, with no lock to fall back on:
# metering owns one RWO ledger file, and the controller has no leader election
# (checked 2026-08-30: none is implemented, and the unused lease grant was
# removed). replicas: 1 + Recreate is the whole mechanism, so it is asserted.
# platform-gateway is a third singleton, for a different reason: the USER
# STORE is in-process (identity provider is decision D3). Two gateways
# disagree the moment an admin creates or deletes an account — and the
# default RollingUpdate surges to two on EVERY rollout, which is how a
# replicas: 1 Deployment still ran two at once until 2026-08-31.
for d in metering capacity-controller platform-gateway; do
  MREP=$($K -n platform-system get deploy "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  MSTRAT=$($K -n platform-system get deploy "$d" -o jsonpath='{.spec.strategy.type}' 2>/dev/null)
  [[ "$MREP" == "1" && "$MSTRAT" == "Recreate" ]]
  chk DGX-36 "$d is a singleton writer (replicas=$MREP strategy=$MSTRAT; a rolling update would run two at once)" $?
done

# --- the ledger chain is KEYED on hardware ---------------------------------
# A plain chain is forgeable by anyone who can write the file (measured
# 2026-08-30). The key is mounted only into the metering pod — but
# the key is a Kubernetes Secret, so it is also in etcd and therefore in
# every etcd SNAPSHOT — which is why Secrets are encrypted at rest (kubeadm
# encryption-provider-config, 2026-09-01) and why the encryption key never
# leaves the head node. Against root ON the head node none of this holds:
# that account reads the key, the ledger and Prometheus alike. The boundary
# this defends is "a copy that left the building", which is exactly the copy
# runbooks/etcd-restore.md tells the operator to make.
CHAIN_MODE=$($K -n platform-system exec deploy/metering -- python3 -c "
import urllib.request
for l in urllib.request.urlopen('http://127.0.0.1:8080/metrics').read().decode().splitlines():
    if l.startswith('arise_metering_ledger_chain_mode'):
        print(l.split('mode=\"')[1].split('\"')[0]); break" 2>/dev/null)
[[ "$CHAIN_MODE" == "hmac-sha256" ]]
chk DGX-37 "the allocation ledger chain is KEYED (mode='${CHAIN_MODE:-unknown}'; make dgx-ledger-key)" $?

$K -n platform-system get secret metering-chain-key >/dev/null 2>&1
chk DGX-37 "Secret metering-chain-key exists (and is recorded in the password vault)" $?

# --- Day-0 retags: sentinels are legal at bring-up, not at launch ---------
DEVBOX=$($K -n platform-system get deploy tenant-portal \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DEVBOX_IMAGE")].value}' 2>/dev/null)
if [[ "$DEVBOX" == day0-registry.invalid/* || -z "$DEVBOX" ]]; then
  warn_or_fail DGX-23 "DEVBOX_IMAGE is still the day0 sentinel ('$DEVBOX') — customers cannot create SSH dev machines until registry-mirror.sh runs and tenant-portal.yaml is retagged"
else
  chk DGX-23 "DEVBOX_IMAGE points at the registry ($DEVBOX)" 0
fi
WEBIMG=$($K -n platform-system get deploy platform-gateway \
  -o jsonpath='{.spec.template.spec.initContainers[0].image}' 2>/dev/null)
if [[ "$WEBIMG" == day0-registry.invalid/* || -z "$WEBIMG" ]]; then
  warn_or_fail DGX-24 "gateway SPA image is still the day0 sentinel ('$WEBIMG') — the console cannot start until retagged"
else
  chk DGX-24 "gateway SPA image points at the registry" 0
fi

# --- things that are expected LATER: warn, never fail -----------------------
# DCGM arrives with the GPU Operator. Its absence before that step is normal;
# its absence AFTER it means GPU health is unobserved, which is why this is
# surfaced rather than silently skipped.
if $K get ns gpu-operator >/dev/null 2>&1; then
  if $K -n gpu-operator get svc nvidia-dcgm-exporter >/dev/null 2>&1; then
    chk DGX-20 "dcgm-exporter present (GPU health is observable)" 0
  else
    warn_or_fail DGX-20 "GPU Operator installed but no dcgm-exporter service — GPU health is UNOBSERVED"
  fi
else
  warn_or_fail DGX-20 "GPU Operator not installed yet (Day-0 step 7); GPU health unobserved"
fi

# The operators' REAL configuration objects (audit 2026-08-27: the Network
# Operator's helm values carry nothing — NicClusterPolicy does). Absent
# before their step is normal; absent after it means the fabric / GPU stack
# is unconfigured while the chart says "installed".
if $K get ns gpu-operator >/dev/null 2>&1; then
  CP_STATE=$($K get clusterpolicies.nvidia.com cluster-policy -o jsonpath='{.status.state}' 2>/dev/null)
  [[ "$CP_STATE" == "ready" ]]
  chk DGX-31 "GPU Operator ClusterPolicy state=ready (got '${CP_STATE:-none}')" $?
  DCGM_CM=$($K -n gpu-operator get configmap arise-dcgm-metrics >/dev/null 2>&1 && echo 1 || echo 0)
  [[ "$DCGM_CM" == 1 ]]
  chk DGX-31 "ConfigMap gpu-operator/arise-dcgm-metrics present (the counter set the GPU rules need)" $?
else
  warn DGX-31 "GPU Operator not installed yet (Day-0 step 7)"
fi
if $K get ns nvidia-network-operator >/dev/null 2>&1; then
  NCP_STATE=$($K get nicclusterpolicies.mellanox.com nic-cluster-policy -o jsonpath='{.status.state}' 2>/dev/null)
  [[ "$NCP_STATE" == "ready" ]]
  chk DGX-32 "NicClusterPolicy nic-cluster-policy state=ready (got '${NCP_STATE:-none — apply infra/dgx/operators/nic-cluster-policy.yaml}')" $?
  NFD2=$($K -n nvidia-network-operator get ds -l app.kubernetes.io/name=node-feature-discovery --no-headers 2>/dev/null | wc -l)
  [[ "$NFD2" == 0 ]]
  chk DGX-32 "no second NFD from the Network Operator (nfd.enabled=false); got $NFD2 DaemonSet(s)" $?
else
  warn DGX-32 "Network Operator not installed yet (Day-0 step 8); fabric unconfigured, HW-06 cannot run"
fi

# --- GPU health must actually be SCRAPED, not just declared ---------------
# The dcgm-exporter job was a comment until 2026-09-08 ("uncomment at Day-0")
# that no step told anyone to uncomment. Now it is declared; this proves the
# running prometheus is pulling real samples from it once the operator is in.
if $K get ns gpu-operator >/dev/null 2>&1; then
  DCGM_UP=$($K -n monitoring exec deploy/prometheus -- wget -qO- \
    'http://127.0.0.1:9090/api/v1/query?query=up%7Bjob%3D%22dcgm-exporter%22%7D' 2>/dev/null \
    | python3 -c "import json,sys; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else 'absent')" 2>/dev/null)
  if [[ "$DCGM_UP" == "1" ]]; then
    chk DGX-20b "prometheus is scraping dcgm-exporter (up=1): GPU health is observed" 0
  else
    warn_or_fail DGX-20b "dcgm-exporter scrape is up=${DCGM_UP:-absent}: every Xid/ECC/thermal alert has no data source"
  fi
fi

# --- the money record and the cluster state each have a SECOND copy --------
# A backup that has never run is a plan. Both CronJobs must exist, and if one
# has ever fired, its most recent SUCCESS must be inside its own cadence —
# a job that has been failing for a week looks identical to a healthy one
# from the object alone (audit 2026-08-31).
for cj in etcd-backup ledger-backup auth-backup; do
  if ! $K -n platform-system get cronjob "$cj" >/dev/null 2>&1; then
    chk DGX-38 "$cj CronJob exists (the only copy of what customers owe / who owns what)" 1
    continue
  fi
  LAST=$($K -n platform-system get cronjob "$cj" -o jsonpath='{.status.lastSuccessfulTime}' 2>/dev/null)
  SUSP=$($K -n platform-system get cronjob "$cj" -o jsonpath='{.spec.suspend}' 2>/dev/null)
  if [[ "$SUSP" == "true" ]]; then
    chk DGX-38 "$cj is SUSPENDED — it produces nothing" 1
  elif [[ -z "$LAST" ]]; then
    # Newly applied and not yet fired: honest WARN in Day-0, blocker at launch.
    warn_or_fail DGX-38 "$cj has never completed successfully yet (applied but unproven)"
  else
    AGE=$(( $(date -u +%s) - $(date -u -d "$LAST" +%s 2>/dev/null || echo 0) ))
    # etcd runs 6-hourly, the ledger hourly; allow two missed runs each.
    [[ "$cj" == "ledger-backup" ]] && MAX=7200 || MAX=43200
    [[ "$AGE" -lt "$MAX" ]]
    chk DGX-38 "$cj last succeeded ${AGE}s ago (must be < ${MAX}s)" $?
  fi
done

# --- customer access: configured, running and reachable with the right key ---
ACCESS_REPLICAS=$($K -n access-system get deploy tenant-bastion -o jsonpath='{.spec.replicas}' 2>/dev/null || true)
if [[ "${ACCESS_REPLICAS:-0}" == 0 ]]; then
  warn_or_fail DGX-39 "tenant SSH/service bastion is not enabled (make dgx-access after configuring public keys)"
else
  ACCESS_CHECK=$(python3 "$REPO/scripts/verify-access.py" --context "$CTX" --public --keys "${ACCESS_KEYS:-$REPO/platform/access/keys.yaml}" 2>&1)
  ACCESS_STATUS=$?
  echo "$ACCESS_CHECK"
  chk DGX-39 "tenant bastion configuration, live tenant fences and public host identity verified" "$ACCESS_STATUS"
fi

# --- evidence ---------------------------------------------------------------
mkdir -p "$(dirname "$OUT")"
{
  echo "{"
  echo "  \"context\": \"$CTX\","
  echo "  \"at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"gpu_nodes_expected\": $GPU_NODES, \"gpu_per_node_expected\": $GPU_PER_NODE,"
  echo "  \"fleet_pinned\": \"${FLEET_NODES_PINNED}x${FLEET_GPUS_PINNED}\", \"fleet_overridden\": \"$FLEET_OVERRIDDEN\","
  echo "  \"passed\": $PASS, \"failed\": $FAIL, \"warned\": $WARN,"
  echo "  \"gate\": \"$([[ $FAIL -eq 0 ]] && echo PASS || echo FAIL)\","
  echo "  \"checks\": [$(IFS=,; echo "${RESULTS[*]}")]"
  echo "}"
} > "$OUT"

echo

echo "passed=$PASS failed=$FAIL warned=$WARN  -> $OUT"
if [[ "$LAUNCH" != "1" ]]; then
  # Name the mode. A gate that is green in its permissive mode and was never
  # run in its strict one is a gate nobody ran (audit 2026-08-31).
  echo "mode=DAY-0 — launch blockers are WARN here. Before taking a paying"
  echo "  customer run: LAUNCH=1 KUBE_CONTEXT=\$DGX_KCTX scripts/verify-dgx.sh"
else
  echo "mode=LAUNCH — every launch blocker counted as a FAIL"
fi
[[ $FAIL -eq 0 ]] || exit 1
