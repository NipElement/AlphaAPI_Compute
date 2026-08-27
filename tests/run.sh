#!/usr/bin/env bash
# ============================================================================
# run.sh — Phase A test matrix.
#
#   ./tests/run.sh smoke    P0 subset
#   ./tests/run.sh all      full matrix
#   ./tests/run.sh <ID>     a single case
#
# Every case asserts machine-checkable facts and writes evidence under
# evidence/<run_id>/tests/<ID>/. Verdicts follow plan §10.2 exactly:
# PASS / FAIL / BLOCKED / INVALID — and BLOCKED is never upgraded to PASS.
# ============================================================================
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO/tests/lib.sh"

IMG="python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
MODE="${1:-smoke}"

# ---- a PSA-'restricted'-compliant probe pod --------------------------------
probe_pod() {  # probe_pod <ns> <name> <python-code>
  cat <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $2
  namespace: $1
  labels: { arise.ai/test: "true", run-id: "$RUN_ID" }
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    seccompProfile: { type: RuntimeDefault }
  containers:
    - name: probe
      image: $IMG
      command: ["python3","-c","$3"]
      resources:
        requests: { cpu: 500m, memory: 512Mi }
        limits:   { cpu: "1", memory: 1Gi }
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: { drop: ["ALL"] }
YAML
}

run_connect_probe() {  # run_connect_probe <ns> <name> <host> <port> -> prints result
  local ns="$1" name="$2" host="$3" port="$4"
  # Probe three times over ~12s and report BOTH the first and the settled
  # result. CNI NetworkPolicy programming is not instantaneous: a pod can
  # complete an egress connection in the window between being scheduled and
  # having its policy rules installed. Sampling once produced a flapping
  # verdict (REACHABLE on one run, BLOCKED on the next) — reporting the
  # settled value keeps the test honest without hiding the transient window.
  local code="import socket,time
res=[]
for i in range(3):
    s=socket.socket(); s.settimeout(5)
    try:
        s.connect(('${host}',${port})); res.append('REACHABLE'); s.close()
    except Exception as e: res.append('BLOCKED_'+type(e).__name__)
    if i<2: time.sleep(5)
print('first='+res[0]+' steady='+res[-1])"
  code="${code//$'\n'/\\n}"
  $K delete pod "$name" -n "$ns" --ignore-not-found --wait=true >/dev/null 2>&1
  probe_pod "$ns" "$name" "$code" | $K apply -f - >/dev/null 2>&1
  $K wait --for=condition=Ready=false pod/"$name" -n "$ns" --timeout=60s >/dev/null 2>&1
  local deadline=$(( $(date +%s) + 75 )) phase
  while (( $(date +%s) < deadline )); do
    phase="$($K get pod "$name" -n "$ns" -o jsonpath='{.status.phase}' 2>/dev/null)"
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
    sleep 3
  done
  $K logs "$name" -n "$ns" 2>/dev/null | tr -d '\r\n'
  $K delete pod "$name" -n "$ns" --ignore-not-found --wait=false >/dev/null 2>&1
}


# =========================================================== fixtures ======
# Tests must be independently runnable (./tests/run.sh OWN-06). Depending on a
# previous case's leftover state makes failures order-dependent and, worse,
# lets an invalid scenario masquerade as a real one — which is exactly how the
# first OWN-04 draft drove a node into QUARANTINED and then blamed the node.

fixture_clean_arise() {  # fixture_clean_arise <logical> <kindnode>
  local node="$1" kn="$2"
  mock_post /v1/test/reset '{}' >/dev/null
  $K delete nodeownership "$node" --ignore-not-found --wait=true >/dev/null 2>&1
  $K label node "$kn" arise.ai/owner=ARISE --overwrite >/dev/null 2>&1
  $K taint node "$kn" arise.ai/vast-owned- >/dev/null 2>&1
  $K taint node "$kn" arise.ai/direct-owned- >/dev/null 2>&1
  $K taint node "$kn" arise.ai/transition- >/dev/null 2>&1
  $K uncordon "$kn" >/dev/null 2>&1
  sleep 2
}

fixture_vast_rented() {  # fixture_vast_rented <logical> <kindnode> <tid>
  local node="$1" kn="$2" tid="$3"
  fixture_clean_arise "$node" "$kn"
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: infrastructure.arise.ai/v1alpha1
kind: NodeOwnership
metadata: { name: $node }
spec: { desiredOwner: VAST, transitionId: $tid, pair: "03-04", approvedBy: tests }
Y
  wait_for 120 "VAST_READY" get nodeownership "$node" -o jsonpath='{.status.phase}' || return 1
  mock_post /v1/test/contracts "{\"machineId\":\"$node\",\"action\":\"create\",\"durationSeconds\":7200}" >/dev/null
  wait_for 90 "VAST_RENTED" get nodeownership "$node" -o jsonpath='{.status.phase}' || return 1
  return 0
}

# =========================================================== test cases =====

test_SEC_02() {
  begin SEC-02 P0 "privileged / hostPath / hostNetwork rejected for tenants"
  assert_rejected "hostNetwork is forbidden|host namespaces|hostNetwork" "hostNetwork denied" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-hostnet, namespace: tenant-arise }
spec:
  hostNetwork: true
  containers: [{ name: c, image: $IMG, command: [sleep,'1'] }]
Y"
  assert_rejected "hostPath volumes are forbidden|restricted volume types|hostPath" "hostPath denied" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-hostpath, namespace: tenant-arise }
spec:
  volumes: [{ name: v, hostPath: { path: /var/lib/mongodb } }]
  containers: [{ name: c, image: $IMG, command: [sleep,'1'] }]
Y"
  assert_rejected "privileged containers are forbidden|privileged (container|must not set securityContext.privileged" "privileged denied" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-priv, namespace: tenant-arise }
spec:
  containers: [{ name: c, image: $IMG, command: [sleep,'1'],
                 securityContext: { privileged: true } }]
Y"
  end
}

test_SEC_03() {
  begin SEC-03 P0 "real nvidia.com/gpu is rejected, never silently scheduled"
  assert_rejected "nvidia.com/gpu is not available" "container GPU request denied" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-realgpu, namespace: tenant-arise }
spec:
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'1'],
                 resources: { limits: { nvidia.com/gpu: '1' } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  local cap
  cap=$($K get nodes -o json | python3 -c "import json,sys;print(sum(int(n['status'].get('allocatable',{}).get('nvidia.com/gpu',0)) for n in json.load(sys.stdin)['items']))")
  assert_eq "$cap" "0" "cluster-wide nvidia.com/gpu capacity is zero"
  end
}

test_SEC_04() {
  begin SEC-04 P0 "ResourceQuota and LimitRange enforced"
  # Quota (requests.cpu=12) and LimitRange (max 8 per container) are different
  # gates. A single 100-CPU container trips the LimitRange first and never
  # reaches the quota check, so it proves nothing about quota. Two containers
  # at 7 satisfy the per-container max yet total 14 > 12.
  assert_rejected "exceeded quota" "over-quota request denied by ResourceQuota" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-overquota, namespace: tenant-arise }
spec:
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers:
    - { name: c1, image: $IMG, command: [sleep,'1'],
        resources: { requests: { cpu: '7', memory: 512Mi }, limits: { cpu: '7', memory: 512Mi } },
        securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }
    - { name: c2, image: $IMG, command: [sleep,'1'],
        resources: { requests: { cpu: '7', memory: 512Mi }, limits: { cpu: '7', memory: 512Mi } },
        securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }
Y"
  # LimitRange must inject defaults when the pod omits them
  $K delete pod t-defaults -n tenant-arise --ignore-not-found --wait=true >/dev/null 2>&1
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-defaults, namespace: tenant-arise }
spec:
  restartPolicy: Never
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [python3,-c,'pass'],
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y
  sleep 4
  local dcpu
  dcpu=$($K get pod t-defaults -n tenant-arise -o jsonpath='{.spec.containers[0].resources.requests.cpu}' 2>/dev/null)
  assert_eq "$dcpu" "500m" "LimitRange injected on-grid default CPU request"
  $K delete pod t-defaults -n tenant-arise --ignore-not-found --wait=false >/dev/null 2>&1
  end
}

test_SEC_05() {
  begin SEC-05 P0 "NetworkPolicy is ENFORCED (probed, not assumed)"
  local mock_ip
  mock_ip=$($K -n vast-mock get svc vast-mock -o jsonpath='{.spec.clusterIP}')
  if [[ -z "$mock_ip" ]]; then blocked "vast-mock service has no ClusterIP"; end; return; fi
  note "target vast-mock ClusterIP $mock_ip:8080"

  # DENIED path: tenant-arise has default-deny egress.
  local denied_raw denied allowed_raw allowed
  denied_raw="$(run_connect_probe tenant-arise np-denied "$mock_ip" 8080)"
  allowed_raw="$(run_connect_probe test-system np-control "$mock_ip" 8080)"
  denied="${denied_raw##*steady=}"; allowed="${allowed_raw##*steady=}"
  note "tenant-arise -> vast-mock : $denied_raw"
  note "test-system  -> vast-mock : $allowed_raw  (control)"
  # Surface the programming window explicitly rather than averaging it away.
  if [[ "$denied_raw" == first=REACHABLE* && "$denied" == BLOCKED_* ]]; then
    note "NOTE: egress succeeded on the first attempt and was blocked once the \
policy was programmed — a real, if brief, exposure window at pod start."
  fi

  if [[ "$allowed" != REACHABLE* ]]; then
    # Without a working control we cannot tell "policy enforced" from "network
    # broken", and guessing would manufacture a false PASS.
    blocked "control path is not reachable either; enforcement is unprovable in this state"
  elif [[ "$denied" == BLOCKED_* ]]; then
    ok "denied path blocked while control path reachable -> policy IS enforced"
  else
    fail "both paths reachable -> NetworkPolicy objects exist but are NOT enforced. \
kind's default CNI likely ignores them. Recreate the cluster with \
disableDefaultCNI:true plus a policy-capable CNI; do NOT relax this test."
  fi

  # ---- the fence must cover a tenant NOBODY ENUMERATED (2026-08-27) --------
  # platform-internal-ingress used to exclude tenants by listing their
  # namespace NAMES. That was fail-open for growth: a third tenant is in no
  # list, so it could reach tenant-portal / ops-console directly by pod IP and
  # bypass gateway authentication entirely. The rule is now keyed on the
  # arise.ai/tier label. This probes from a brand-new tenant namespace that
  # appears in no manifest — the exact situation an onboarded customer is in.
  # Note it deliberately has NO egress policies of its own: anything blocking
  # here is the platform-side INGRESS fence doing its job.
  local portal_ip
  portal_ip="$($K -n platform-system get svc tenant-portal -o jsonpath='{.spec.clusterIP}' 2>/dev/null)"
  if [[ -z "$portal_ip" ]]; then
    blocked "tenant-portal Service has no ClusterIP; fence probe unprovable"
  else
    $K delete ns tenant-fence-probe --ignore-not-found --wait=true >/dev/null 2>&1
    $K create ns tenant-fence-probe >/dev/null 2>&1
    $K label ns tenant-fence-probe arise.ai/tier=tenant project=arise-b300-prelab \
      pod-security.kubernetes.io/enforce=restricted --overwrite >/dev/null 2>&1
    local newt_raw newt
    newt_raw="$(run_connect_probe tenant-fence-probe sec05-newtenant "$portal_ip" 8080)"
    newt="${newt_raw##*steady=}"
    note "unlisted new tenant -> tenant-portal : ${newt_raw:-<no output>}"
    if [[ "$newt_raw" != *steady=* ]]; then
      # No probe output at all means the pod never ran (namespace still
      # terminating, image pull, scheduling). That is absence of evidence, not
      # evidence the fence failed — blaming the fence here would be a false
      # accusation, and passing would be a false clean bill.
      blocked "fence probe produced no result; enforcement unprovable this run"
    elif [[ "$newt" == BLOCKED_* ]]; then
      ok "a tenant namespace in no allow-list is still fenced (label-keyed)"
    else
      fail "an unlisted tenant namespace REACHED tenant-portal by pod IP — the \
platform-ingress fence is enumerating names again, which is fail-open for \
every customer onboarded after it was written."
    fi
    $K delete ns tenant-fence-probe --ignore-not-found --wait=false >/dev/null 2>&1
  fi
  end
}

test_SEC_06() {
  begin SEC-06 P0 "tenant RBAC cannot read secrets / nodes / other namespaces"
  local sa="system:serviceaccount:tenant-arise:tenant-runner"
  for probe in "get secrets -n tenant-arise" "list nodes" \
               "get pods -n platform-system" "get pods -n vast-mock" \
               "delete nodeownerships"; do
    # shellcheck disable=SC2086
    local ans; ans=$($K auth can-i $probe --as="$sa" 2>/dev/null)
    assert_eq "$ans" "no" "tenant cannot: $probe"
  done
  local own; own=$($K auth can-i create pods -n tenant-arise --as="$sa" 2>/dev/null)
  assert_eq "$own" "yes" "tenant CAN create pods in its own namespace"
  end
}

test_SCH_01() {
  begin SCH-01 P0 "8 fake GPU per worker, 32 total, 0 on control-plane"
  local total=0 pernode_ok=1
  for n in $($K get nodes -l arise.ai/node-id --no-headers -o custom-columns=N:.metadata.name); do
    local c; c=$($K get node "$n" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}')
    [[ "$c" == "8" ]] || { pernode_ok=0; note "$n has '$c'"; }
    total=$((total + ${c:-0}))
  done
  assert_eq "$pernode_ok" "1" "every worker advertises 8"
  assert_eq "$total" "32" "cluster total"
  local cp; cp=$($K get node "$(node_for control-plane)" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}')
  assert_eq "${cp:-0}" "0" "control-plane advertises none"
  $K get nodes -o custom-columns='NODE:.metadata.name,ID:.metadata.labels.arise\.ai/node-id,FAKEGPU:.status.allocatable.arise\.dev/fake-gpu' \
    > "$CUR_DIR/metrics/capacity.txt" 2>/dev/null
  end
}

test_SCH_02() {
  begin SCH-02 P0 "a 9-GPU request stays Pending with Insufficient reason"
  $K delete pod t-sch02 -n tenant-arise --ignore-not-found --wait=true >/dev/null 2>&1
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-sch02, namespace: tenant-arise }
spec:
  restartPolicy: Never
  nodeSelector: { arise.ai/owner: ARISE }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'30'],
                 resources: { requests: { cpu: 500m, memory: 512Mi, arise.dev/fake-gpu: '9' },
                              limits:   { cpu: '1', memory: 1Gi, arise.dev/fake-gpu: '9' } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y
  sleep 12
  local phase reason
  phase=$($K get pod t-sch02 -n tenant-arise -o jsonpath='{.status.phase}' 2>/dev/null)
  reason=$($K get pod t-sch02 -n tenant-arise -o jsonpath='{.status.conditions[?(@.type=="PodScheduled")].message}' 2>/dev/null)
  assert_eq "$phase" "Pending" "pod stays Pending"
  assert_contains "$reason" "Insufficient" "scheduler cites insufficient fake-gpu"
  echo "$reason" > "$CUR_DIR/events/scheduler-message.txt" 2>/dev/null
  $K delete pod t-sch02 -n tenant-arise --ignore-not-found --wait=false >/dev/null 2>&1
  end
}

test_SCH_04() {
  begin SCH-04 P0 "workloads cannot target a non-ARISE node"
  assert_rejected "may only target nodes with arise.ai/owner=ARISE" "nodeSelector owner=VAST denied" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-vastsel, namespace: tenant-arise }
spec:
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  nodeSelector: { arise.ai/owner: VAST }
  containers: [{ name: c, image: $IMG, command: [sleep,'1'], securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  assert_rejected "nodeName pinning is not permitted" "nodeName pinning denied" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-pinned, namespace: tenant-arise }
spec:
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  nodeName: $(node_for dgx03)
  containers: [{ name: c, image: $IMG, command: [sleep,'1'], securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  end
}

test_SCH_07() {
  begin SCH-07 P1 "marking a device unhealthy lowers allocatable"
  local svc; svc=$($K -n platform-system get svc fake-gpu-advertiser -o jsonpath='{.spec.clusterIP}')
  $K -n platform-system exec deploy/fake-gpu-advertiser -- python3 -c "
import urllib.request
req=urllib.request.Request('http://127.0.0.1:8080/test/unhealthy',method='POST')
req.add_header('Content-Type','application/json')
req.data=b'{\"device\":\"dgx01-fake-gpu-0\",\"faultId\":\"f-sch07\"}'
print(urllib.request.urlopen(req,timeout=8).read().decode())" >/dev/null 2>&1
  if wait_for 60 "7" get node "$(node_for dgx01)" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}'; then
    ok "dgx01 allocatable dropped 8 -> 7"
  else
    fail "allocatable did not drop; got $($K get node $(node_for dgx01) -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}')"
  fi
  # Device-manager semantics prove the update came from KUBELET, not from a
  # controller PATCH: capacity keeps counting the sick device (8) while
  # allocatable excludes it (7). The old status-patch advertiser dropped BOTH
  # to 7, so this assertion is exactly the §8.2 "随 kubelet 更新" evidence.
  assert_eq "$($K get node $(node_for dgx01) -o jsonpath='{.status.capacity.arise\.dev/fake-gpu}')" \
    "8" "capacity stays 8 while allocatable is 7 (kubelet device manager, not a controller patch)"
  $K -n platform-system exec deploy/fake-gpu-advertiser -- python3 -c "
import urllib.request
req=urllib.request.Request('http://127.0.0.1:8080/test/reset',method='POST')
req.add_header('Content-Type','application/json'); req.data=b'{}'
urllib.request.urlopen(req,timeout=8)" >/dev/null 2>&1
  if wait_for 60 "8" get node "$(node_for dgx01)" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}'; then
    ok "restored to 8 after clearing the fault"
  else
    fail "allocatable did not recover to 8"
  fi
  end
}

test_VST_06() {
  begin VST-06 P0 "VAST production adapter unreachable and triple-disabled"
  local flag
  flag=$($K -n platform-system get deploy capacity-controller \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VAST_PRODUCTION_ADAPTER_ENABLED")].value}')
  assert_eq "$flag" "false" "runtime flag disabled"
  local base
  base=$($K -n platform-system get deploy capacity-controller \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="VAST_API_BASE")].value}')
  assert_contains "$base" "vast-mock.vast-mock.svc" "adapter points at the in-cluster mock"
  assert_ne "$base" "https://console.vast.ai" "adapter is not a production endpoint"
  # The mock must have no outbound path at all.
  local egress_raw egress
  egress_raw="$(run_connect_probe vast-mock vst06-egress 1.1.1.1 443)"
  egress="${egress_raw##*steady=}"
  note "vast-mock -> internet : $egress_raw"
  if [[ "$egress" == BLOCKED_* ]]; then
    ok "mock namespace has no settled internet egress"
  else
    fail "mock namespace can still reach the internet after policy settled"
  fi
  # Prove NO production VAST hostname is in the source that could dial out.
  # The old check grepped $REPO/mock (does not exist) and only controller/,
  # so the target dir was absent, grep errored into /dev/null, and the ||
  # branch made this P0 sub-assertion incapable of ever failing. Grep the
  # REAL backend sources, and treat a missing dir as a failure (absence can
  # only be proven over dirs that exist).
  local vast_srcs=("$REPO/services" "$REPO/controller" "$REPO/platform")
  local d missing=0
  for d in "${vast_srcs[@]}"; do
    [[ -d "$d" ]] || { fail "cannot prove absence: source dir missing: $d"; missing=1; }
  done
  if [[ "$missing" == 0 ]]; then
    if grep -rqE "console\.vast\.ai|api\.vast\.ai" "${vast_srcs[@]}" 2>/dev/null; then
      fail "a production VAST hostname appears in source"
    else
      ok "no production VAST endpoint in services/controller/platform"
    fi
  fi
  end
}

test_VST_03() {
  begin VST-03 P0 "active contract hard-blocks reclaim (unlist is not reclaim)"
  local node=dgx03
  fixture_clean_arise dgx03 "$(node_for dgx03)"
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: infrastructure.arise.ai/v1alpha1
kind: NodeOwnership
metadata: { name: $node }
spec: { desiredOwner: VAST, transitionId: t-vst03-handover, pair: "03-04", approvedBy: tests }
Y
  if ! wait_for 90 "VAST_READY" get nodeownership $node -o jsonpath='{.status.phase}'; then
    blocked "handover did not reach VAST_READY; cannot test the contract gate"
    end; return
  fi
  ok "handover reached VAST_READY"
  mock_post /v1/test/contracts '{"machineId":"dgx03","action":"create","durationSeconds":7200}' >/dev/null
  wait_for 60 "VAST_RENTED" get nodeownership $node -o jsonpath='{.status.phase}' \
    && ok "contract created -> VAST_RENTED" || fail "did not enter VAST_RENTED"

  # Now demand reclaim while the contract is live.
  $K patch nodeownership $node --type=merge \
    -p '{"spec":{"desiredOwner":"ARISE","transitionId":"t-vst03-reclaim","requireSanitization":true}}' >/dev/null
  sleep 40
  assert_eq "$(nown_phase $node)" "VAST_RENTED" "phase held at VAST_RENTED"
  assert_eq "$(mock_field dgx03 activeContracts)" "1" "contract still active"
  assert_eq "$(mock_field dgx03 listed)" "False" "unlisted (stops NEW contracts only)"
  assert_eq "$($K get node $(node_for dgx03) -o jsonpath='{.spec.unschedulable}')" "true" \
    "node NOT uncordoned"
  assert_eq "$($K get node $(node_for dgx03) -o jsonpath='{.metadata.labels.arise\.ai/owner}')" "VAST" \
    "owner NOT returned to ARISE"
  local cond; cond=$($K get nodeownership $node -o jsonpath='{.status.conditions[?(@.type=="ReclaimBlocked")].reason}')
  assert_eq "$cond" "ActiveContracts" "explicit ReclaimBlocked condition"
  capture_events $node
  mock_get /v1/machines/dgx03 > "$CUR_DIR/response/machine.json" 2>/dev/null
  end
}

test_OWN_04() {
  begin OWN-04 P0 "same transitionId is idempotent; external side effect once"
  # A valid replay is the SAME request repeated while its effect still stands.
  # Replaying an id whose effect a later unlist has undone is NOT idempotency —
  # it is asking to resurrect superseded state, and the controller is right to
  # quarantine on the resulting readback mismatch.
  fixture_clean_arise dgx03 "$(node_for dgx03)"
  local tid="t-own04-$(date +%s)"
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: infrastructure.arise.ai/v1alpha1
kind: NodeOwnership
metadata: { name: dgx03 }
spec: { desiredOwner: VAST, transitionId: $tid, pair: "03-04", approvedBy: tests }
Y
  if ! wait_for 120 "VAST_READY" get nodeownership dgx03 -o jsonpath='{.status.phase}'; then
    blocked "handover did not complete; cannot test replay"; end; return
  fi
  local before; before=$(mock_get /v1/machines/dgx03 | python3 -c "import json,sys;print(json.load(sys.stdin)['sideEffectCounts']['list'])" 2>/dev/null)
  assert_eq "$before" "1" "first handover caused exactly one list"

  # Re-assert the identical desired state with the identical id.
  $K patch nodeownership dgx03 --type=merge \
    -p "{\"spec\":{\"desiredOwner\":\"VAST\",\"transitionId\":\"$tid\"}}" >/dev/null 2>&1
  sleep 35
  local after; after=$(mock_get /v1/machines/dgx03 | python3 -c "import json,sys;print(json.load(sys.stdin)['sideEffectCounts']['list'])" 2>/dev/null)
  assert_eq "$after" "$before" "replay caused NO additional list side effect"
  assert_ne "$(nown_phase dgx03)" "QUARANTINED" "replay did not destabilise the node"
  mock_get /v1/test/audit > "$CUR_DIR/response/audit.json" 2>/dev/null
  end
}

test_OWN_06() {
  begin OWN-06 P0 "tampering with owner label / taint is corrected"
  local n="$(node_for dgx03)"
  if ! fixture_vast_rented dgx03 "$n" "t-own06-$(date +%s)"; then
    blocked "could not establish a VAST_RENTED fixture"; end; return
  fi
  $K taint node "$n" arise.ai/vast-owned- >/dev/null 2>&1
  $K uncordon "$n" >/dev/null 2>&1
  note "stripped taint and uncordoned by hand"
  local fixed=0
  for _ in $(seq 1 12); do
    sleep 5
    local t c
    t=$($K get node "$n" -o jsonpath='{.spec.taints[*].key}')
    c=$($K get node "$n" -o jsonpath='{.spec.unschedulable}')
    if [[ "$t" == *"arise.ai/vast-owned"* && "$c" == "true" ]]; then fixed=1; break; fi
  done
  assert_eq "$fixed" "1" "controller restored taint + cordon within SLA"
  capture_events dgx03
  end
}

test_E2E_04() {
  begin E2E-04 P0 "VAST -> ARISE only after contracts hit zero, via sanitize gate"
  local node=dgx03
  if [[ "$(nown_phase $node)" != "VAST_RENTED" ]]; then
    if ! fixture_vast_rented dgx03 "$(node_for dgx03)" "t-e2e04-$(date +%s)"; then
      blocked "could not establish a VAST_RENTED fixture"; end; return
    fi
  fi
  local cid
  cid=$(mock_get /v1/machines/dgx03 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['contracts'][0]['id'] if d['contracts'] else '')" 2>/dev/null)
  [[ -n "$cid" ]] || { blocked "no active contract to end"; end; return; }

  # Step 1: REQUEST the reclaim while the contract is still live. Ending the
  # contract without asking for ARISE proves nothing — the controller has no
  # reason to act, and parking at VAST_READY is correct behaviour, not a bug.
  $K patch nodeownership $node --type=merge \
    -p '{"spec":{"desiredOwner":"ARISE","transitionId":"t-e2e04-reclaim","requireSanitization":true}}' >/dev/null
  sleep 30
  assert_eq "$(nown_phase $node)" "VAST_RENTED" "reclaim blocked while the contract runs"
  assert_eq "$($K get node $(node_for dgx03) -o jsonpath='{.spec.unschedulable}')" "true" \
    "still cordoned during the block"

  # Step 2: contract ends -> the gate opens, sanitize then health-check.
  mock_post /v1/test/contracts "{\"machineId\":\"dgx03\",\"action\":\"end\",\"contractId\":\"$cid\"}" >/dev/null
  note "ended contract $cid"
  if wait_for 180 "READY" get nodeownership $node -o jsonpath='{.status.phase}'; then
    ok "reached READY after contracts hit zero"
  else
    fail "did not reach READY; phase=$(nown_phase $node)"
  fi
  assert_eq "$(mock_field dgx03 activeContracts)" "0" "no active contracts"
  assert_eq "$($K get node $(node_for dgx03) -o jsonpath='{.metadata.labels.arise\.ai/owner}')" "ARISE" \
    "owner returned to ARISE"
  assert_eq "$($K get node $(node_for dgx03) -o jsonpath='{.spec.unschedulable}')" "" \
    "node uncordoned"
  local taints; taints=$($K get node "$(node_for dgx03)" -o jsonpath='{.spec.taints[*].key}')
  assert_not_contains "$taints" "arise.ai/vast-owned" "VAST taint removed"
  $K get nodeownership $node -o jsonpath='{.status.sanitizationResults}' \
    > "$CUR_DIR/response/sanitization.json" 2>/dev/null
  local sr; sr=$($K get nodeownership $node -o jsonpath='{.status.sanitizationResults[*].check}')
  assert_contains "$sr" "data_erasure" "sanitization gate recorded its checks"
  note "NOTE: data_erasure/health_score are SIMULATED — they prove gate ORDER, not erasure"
  capture_events $node
  end
}


test_SCH_03() {
  begin SCH-03 P0 "namespace fake-GPU quota is enforced at admission"
  # tenant-arise quota: requests.arise.dev/fake-gpu = 24. The LimitRange caps
  # cpu/memory only, so nothing masks the quota check here.
  assert_rejected "exceeded quota" "25 fake-GPU request denied by quota" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-gpuquota, namespace: tenant-arise }
spec:
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'1'],
                 resources: { requests: { cpu: 500m, memory: 512Mi, arise.dev/fake-gpu: '25' },
                              limits:   { cpu: 500m, memory: 512Mi, arise.dev/fake-gpu: '25' } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  end
}


# ====================================================== scheduling helpers ==
# Everything below models what the DGX fleet will actually run: multi-node
# gang jobs with per-role replicas, competing entitlements, and placement
# pinned to an NVLink pair. The resources are simulated; the scheduling
# semantics being exercised are the real ones.

sched_cleanup() {
  for ns in tenant-arise tenant-direct test-system; do
    $K -n "$ns" delete job.batch.volcano.sh --all --ignore-not-found --wait=false >/dev/null 2>&1
    $K -n "$ns" delete pod -l arise.ai/test=true --grace-period=1 \
      --ignore-not-found --wait=false >/dev/null 2>&1
    $K -n "$ns" delete podgroup --all --ignore-not-found >/dev/null 2>&1
  done
  # Wait for the CONDITION (capacity actually released), not for a fixed
  # duration. A terminating pod still holds its simulated GPUs, so a fixed
  # `sleep 6` handed the next test a fleet that only LOOKED idle — which is
  # what made SCH-05 report a blocked baseline when the scheduler was fine.
  local deadline=$(( $(date +%s) + 90 )) left
  while (( $(date +%s) < deadline )); do
    left=$($K get pods -A -l arise.ai/test=true --no-headers 2>/dev/null | wc -l)
    (( left == 0 )) && return 0
    sleep 3
  done
  echo "      $(c_ylw "· cleanup: $left test pod(s) still terminating after 90s")"
  return 1
}

# A single-role gang PodGroup + pods. Used where a vcjob would add noise.
gang_submit() {  # gang_submit <name> <ns> <queue> <pair> <gpu> [priorityClass]
  local name="$1" ns="$2" q="$3" pair="$4" gpu="$5" pc="${6:-arise-best-effort}"
  local cpu=$(( gpu * 500 ))     # 500m CPU per simulated GPU
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata: { name: $name, namespace: $ns }
spec:
  minMember: 2
  queue: $q
  # Volcano derives JOB priority from the PodGroup, not from member pods.
  # Setting priorityClassName only on the pods leaves every job at equal
  # job-level priority, so the preempt action finds no reason to act and
  # a "priority" model that looks configured does nothing. Both are set.
  priorityClassName: $pc
  minResources: { arise.dev/fake-gpu: "$((gpu*2))" }
Y
  for n in a b; do
    cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: ${name}-${n}
  namespace: $ns
  annotations: { scheduling.k8s.io/group-name: $name }
  labels: { arise.ai/test: "true", gang: "$name" }
spec:
  schedulerName: volcano
  restartPolicy: Never
  terminationGracePeriodSeconds: 2
  priorityClassName: $pc
  nodeSelector: { arise.ai/pair: "${pair}", arise.ai/owner: ARISE }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: c
      image: $IMG
      command: [sleep,'900']
      resources:
        # CPU is requested PROPORTIONAL to simulated GPU (500m per GPU).
        # Volcano's proportion/DRF share maths runs on cpu+memory; an extended
        # resource does not participate. With a token 50m request every queue
        # looks far under its deserved share no matter how many GPUs it holds,
        # so reclaim never fires and queue weights are decorative. Real
        # training jobs do request CPU alongside GPU — the token request was
        # the unrealistic part, and it silently disabled the fairness model.
        requests: { cpu: "${cpu}m", memory: 512Mi, arise.dev/fake-gpu: "${gpu}" }
        limits:   { cpu: "$((cpu*2))m", memory: 1Gi, arise.dev/fake-gpu: "${gpu}" }
      securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
Y
  done
}

running_count() {  # running_count <ns> <label-selector>
  $K -n "$1" get pods -l "$2" -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null \
    | grep -c Running
}

nodes_used() {  # nodes_used <ns> <label-selector>
  $K -n "$1" get pods -l "$2" --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | sort -u
}

set_device_health() {  # set_device_health <device> <unhealthy|healthy>
  $K -n platform-system exec deploy/fake-gpu-advertiser -- env DEV="$1" python3 -c "
import os,urllib.request,json
req=urllib.request.Request('http://127.0.0.1:8080/test/$2',method='POST')
req.add_header('Content-Type','application/json')
req.data=json.dumps({'device':os.environ['DEV']}).encode()
urllib.request.urlopen(req,timeout=8)" >/dev/null 2>&1
}

# ============================================================ test cases ====

test_SCH_08() {
  begin SCH-08 P0 "VolcanoJob: multi-role distributed job is gang-scheduled"
  sched_cleanup
  # This is the shape real training takes: distinct roles, per-role replicas,
  # one all-or-nothing admission decision across the whole job.
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata: { name: train-0102, namespace: tenant-arise }
spec:
  minAvailable: 2
  schedulerName: volcano
  queue: arise-internal
  policies:
    - event: PodEvicted
      action: RestartJob
  tasks:
    - replicas: 1
      name: master
      template:
        metadata: { labels: { arise.ai/test: "true", vcjob: train-0102 } }
        spec:
          restartPolicy: Never
          terminationGracePeriodSeconds: 2
          nodeSelector: { arise.ai/pair: "01-02", arise.ai/owner: ARISE }
          securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
          containers:
            - name: c
              image: $IMG
              command: [sleep,'600']
              resources:
                requests: { cpu: "4", memory: 512Mi, arise.dev/fake-gpu: "8" }
                limits:   { cpu: "8", memory: 1Gi, arise.dev/fake-gpu: "8" }
              securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
    - replicas: 1
      name: worker
      template:
        metadata: { labels: { arise.ai/test: "true", vcjob: train-0102 } }
        spec:
          restartPolicy: Never
          terminationGracePeriodSeconds: 2
          nodeSelector: { arise.ai/pair: "01-02", arise.ai/owner: ARISE }
          securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
          containers:
            - name: c
              image: $IMG
              command: [sleep,'600']
              resources:
                requests: { cpu: "4", memory: 512Mi, arise.dev/fake-gpu: "8" }
                limits:   { cpu: "8", memory: 1Gi, arise.dev/fake-gpu: "8" }
              securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
Y
  local ok=0
  for _ in $(seq 1 24); do
    sleep 5
    [[ "$(running_count tenant-arise vcjob=train-0102)" == "2" ]] && { ok=1; break; }
  done
  assert_eq "$ok" "1" "both roles Running (master + worker)"
  local roles; roles=$($K -n tenant-arise get pods -l vcjob=train-0102 \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | sort | tr '\n' ' ')
  note "pods: $roles"
  assert_contains "$roles" "master" "master role present"
  assert_contains "$roles" "worker" "worker role present"
  local n; n=$(nodes_used tenant-arise vcjob=train-0102 | wc -l)
  assert_eq "$n" "2" "roles placed on two distinct nodes"
  local off; off=$(nodes_used tenant-arise vcjob=train-0102 | grep -c 'worker3\|worker4' || true)
  assert_eq "$off" "0" "no role spilled outside pair 01-02"
  $K -n tenant-arise get job.batch.volcano.sh train-0102 -o yaml > "$CUR_DIR/response/vcjob.yaml" 2>/dev/null
  sched_cleanup
  end
}

test_SCH_09() {
  begin SCH-09 P0 "queue capability caps entitlement even on an idle fleet"
  sched_cleanup
  # The `system` queue is capped at 4 simulated GPUs. The fleet has 32 free.
  # A capability cap that only bites under contention is not a cap.
  gang_submit cap-probe test-system system "01-02" 8
  sleep 40
  # Guard against passing for the wrong reason: the pods must EXIST (namespace
  # quota admitted them) and be Pending because the QUEUE refused the job.
  local created; created=$($K -n test-system get pods -l gang=cap-probe --no-headers 2>/dev/null | wc -l)
  assert_eq "$created" "2" "probe pods were admitted by the namespace quota"
  local running; running=$(running_count test-system gang=cap-probe)
  assert_eq "$running" "0" "job over the queue cap stays Pending on an idle fleet"
  local free; free=$(prom_q 'sum(arise_fake_gpu_capacity)' | python3 -c "
import json,sys;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else '?')" 2>/dev/null)
  note "fleet capacity at the time: ${free} simulated GPUs, queue cap is 4"
  $K -n test-system get podgroup cap-probe -o yaml > "$CUR_DIR/response/capped-podgroup.yaml" 2>/dev/null
  sched_cleanup
  end
}

test_SCH_13() {
  begin SCH-13 P0 "device plugin injects ARISE_FAKE_GPU_IDS (plan §8.2 Allocate)"
  # A pod requesting N fake GPUs must see the N device IDs kubelet assigned,
  # as a comma-separated env var. Only a real device plugin Allocate response
  # can produce this — a status-patched extended resource injects nothing —
  # so a PASS here is direct evidence the §8.2 "分配行为" row is closed.
  $K delete pod t-sch13 -n tenant-arise --ignore-not-found --wait=true >/dev/null 2>&1
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-sch13, namespace: tenant-arise }
spec:
  restartPolicy: Never
  nodeSelector: { arise.ai/owner: ARISE }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG,
                 command: [python3, -c, "import os,time; print('IDS='+os.environ.get('ARISE_FAKE_GPU_IDS','<absent>'), flush=True); time.sleep(120)"],
                 resources: { requests: { cpu: 500m, memory: 512Mi, arise.dev/fake-gpu: '2' },
                              limits:   { cpu: '1', memory: 1Gi, arise.dev/fake-gpu: '2' } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y
  local phase="" i
  for i in $(seq 1 30); do
    phase=$($K get pod t-sch13 -n tenant-arise -o jsonpath='{.status.phase}' 2>/dev/null)
    [[ "$phase" == "Running" ]] && break
    sleep 2
  done
  assert_eq "$phase" "Running" "probe pod running"
  sleep 3
  local ids node logical
  ids=$($K logs t-sch13 -n tenant-arise 2>/dev/null | sed -n 's/^IDS=//p' | head -1)
  node=$($K get pod t-sch13 -n tenant-arise -o jsonpath='{.spec.nodeName}' 2>/dev/null)
  logical=$($K get node "$node" -o jsonpath='{.metadata.labels.arise\.ai/node-id}' 2>/dev/null)
  echo "$ids" > "$CUR_DIR/events/sch13-allocated-ids.txt" 2>/dev/null
  if [[ "$ids" =~ ^${logical}-fake-gpu-[0-7],${logical}-fake-gpu-[0-7]$ ]]; then
    ok "two device IDs injected, both on scheduling node $logical: $ids"
  else
    fail "ARISE_FAKE_GPU_IDS malformed or absent: '$ids' (node=$logical)"
  fi
  local d1 d2
  d1=${ids%%,*}; d2=${ids##*,}
  if [[ -n "$ids" && "$d1" != "$d2" ]]; then
    ok "the two IDs are distinct devices"
  else
    fail "device IDs not distinct: '$ids'"
  fi
  $K delete pod t-sch13 -n tenant-arise --ignore-not-found --wait=false >/dev/null 2>&1
  end
}

test_SCH_11() {
  begin SCH-11 P0 "pair affinity: a pair job never spills to the other pair"
  sched_cleanup
  # Break one member of pair 01-02. The job must wait for ITS pair rather than
  # relocating to 03-04 — on real hardware the other pair is a different
  # NVLink domain, so a silent relocation would quietly destroy the
  # performance assumption the job was written against.
  set_device_health dgx02-fake-gpu-0 unhealthy
  if ! wait_for 60 "7" get node "$(node_for dgx02)" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}'; then
    blocked "could not shrink dgx02"; set_device_health dgx02-fake-gpu-0 healthy; sched_cleanup; end; return
  fi
  note "dgx02 reduced to 7; an 8-GPU member no longer fits on pair 01-02"

  gang_submit pair-probe tenant-arise arise-internal "01-02" 8
  sleep 45
  assert_eq "$(running_count tenant-arise gang=pair-probe)" "0" "nothing started"
  local spilled; spilled=$($K -n tenant-arise get pods -l gang=pair-probe \
    -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' 2>/dev/null | grep -c 'worker3\|worker4' || true)
  assert_eq "$spilled" "0" "no pod placed on pair 03-04"
  $K -n tenant-arise get pods -l gang=pair-probe \
    -o custom-columns='POD:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName' \
    --no-headers > "$CUR_DIR/stdout/pair-probe.txt" 2>/dev/null

  set_device_health dgx02-fake-gpu-0 healthy
  wait_for 60 "8" get node "$(node_for dgx02)" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}' >/dev/null
  local ok=0
  for _ in $(seq 1 20); do
    sleep 5
    [[ "$(running_count tenant-arise gang=pair-probe)" == "2" ]] && { ok=1; break; }
  done
  assert_eq "$ok" "1" "runs on its own pair once the pair is whole again"
  sched_cleanup
  end
}

test_SCH_12() {
  begin SCH-12 P0 "entitlement cannot be borrowed: queue and priority bindings"
  # A tenant that can name another tenant's queue inherits its weight AND its
  # reclaim protection — one line of YAML converting internal work into work
  # the scheduler refuses to reclaim.
  assert_rejected "may not submit to queue" "tenant-arise cannot use direct-customer queue" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata: { name: steal-queue, namespace: tenant-arise }
spec: { minMember: 1, queue: direct-customer }
Y"
  assert_accepted "tenant-arise CAN use its own queue" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata: { name: own-queue, namespace: tenant-arise }
spec: { minMember: 1, queue: arise-internal }
Y"
  $K -n tenant-arise delete podgroup own-queue --ignore-not-found >/dev/null 2>&1

  # ---- the LABEL is what binds, not an enumerated list (2026-08-27) -------
  # Tenants used to be listed in the policy's CEL map, so onboarding a customer
  # meant remembering to edit it in both overlays; forgetting silently dropped
  # that paying customer to the 'default' queue — no weight, no reclaim
  # protection. Entitlement now comes from the namespace's arise.ai/queue
  # label. This probes a NAMESPACE THAT NO CEL MAP MENTIONS, which is exactly
  # the situation a newly onboarded tenant is in.
  $K delete ns tenant-queue-probe --ignore-not-found --wait=true >/dev/null 2>&1
  $K create ns tenant-queue-probe >/dev/null 2>&1
  $K label ns tenant-queue-probe arise.ai/tier=tenant arise.ai/queue=system \
    project=arise-b300-prelab --overwrite >/dev/null 2>&1
  assert_accepted "an unlisted namespace CAN use the queue its label names" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata: { name: label-bound, namespace: tenant-queue-probe }
spec: { minMember: 1, queue: system }
Y"
  assert_rejected "may not submit to queue" "and NOT a queue its label does not name" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata: { name: label-steal, namespace: tenant-queue-probe }
spec: { minMember: 1, queue: direct-customer }
Y"
  # An unlabelled namespace has declared no entitlement, so it gets none.
  $K label ns tenant-queue-probe arise.ai/queue- >/dev/null 2>&1
  assert_rejected "may not submit to queue" "no label at all -> default queue only" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: scheduling.volcano.sh/v1beta1
kind: PodGroup
metadata: { name: label-none, namespace: tenant-queue-probe }
spec: { minMember: 1, queue: arise-internal }
Y"
  $K delete ns tenant-queue-probe --ignore-not-found --wait=false >/dev/null 2>&1

  # Contract-bound priority is not self-service either.
  assert_rejected "reserved for workloads in tenant-direct" "internal pod cannot claim contract priority" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: steal-prio, namespace: tenant-arise }
spec:
  priorityClassName: arise-contract-bound
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'1'],
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  end
}

test_SCH_05() {
  begin SCH-05 P1 "priority: a reservation displaces best-effort work"
  sched_cleanup
  # Fill pair 01-02 with best-effort work, then submit an approved
  # reservation. Without the preempt action this cannot succeed — which is
  # why the scheduler config enables it explicitly.
  gang_submit be-job tenant-arise arise-internal "01-02" 8 arise-best-effort
  local up=0
  for _ in $(seq 1 20); do sleep 5; [[ "$(running_count tenant-arise gang=be-job)" == "2" ]] && { up=1; break; }; done
  if [[ "$up" != "1" ]]; then blocked "best-effort baseline did not start"; sched_cleanup; end; return; fi
  ok "best-effort job holds the pair (2 pods, 16 GPU)"

  # The reserved job asks for 4 GPU per member, not 8. Not to make it easier
  # to schedule — it still cannot fit without evicting the baseline — but
  # because ResourceQuota admits pods BEFORE the scheduler ever sees them.
  # With baseline 16 + preemptor 16 against a 24-GPU namespace quota, the
  # preemptor's second pod is refused at admission, the gang never reaches
  # minMember, and preemption has no viable candidate to schedule. The
  # scheduler looks broken when the real limit is quota headroom.
  # See runbooks/gaps.md — this is a live constraint for the real fleet.
  gang_submit rsv-job tenant-arise arise-internal "01-02" 4 arise-reserved
  local won=0
  for _ in $(seq 1 30); do
    sleep 5
    [[ "$(running_count tenant-arise gang=rsv-job)" == "2" ]] && { won=1; break; }
  done
  assert_eq "$won" "1" "reserved job obtained the capacity"

  # Volcano evicts the MINIMUM number of victims needed, which is efficient
  # and correct. The consequence is not: a gang job with minMember=2 that
  # loses one member is left with a survivor that can never form a gang,
  # produces nothing, and still holds its GPUs until someone notices.
  local be; be=$(running_count tenant-arise gang=be-job)
  assert_eq "$(( be < 2 ? 1 : 0 ))" "1" "best-effort job lost capacity to the reservation"
  if (( be > 0 )); then
    note "STRANDED SURVIVOR: ${be}/2 best-effort member(s) still Running after \
preemption. A raw PodGroup has nothing that reacts to eviction, so this pod \
burns capacity indefinitely. Mitigation is the vcjob policy asserted in \
SCH-08 (PodEvicted -> RestartJob), which requeues the whole job instead of \
leaving half of it alive. Do NOT submit bare PodGroups for gang work on the \
real fleet."
    # The hazard is the point of this case, so record it as evidence rather
    # than smoothing it away.
    $K -n tenant-arise get pods -l gang=be-job \
      -o custom-columns='POD:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName' \
      --no-headers > "$CUR_DIR/stdout/stranded-survivor.txt" 2>/dev/null
  fi
  $K -n tenant-arise get pods -l arise.ai/test=true \
    -o custom-columns='POD:.metadata.name,PHASE:.status.phase,PRIO:.spec.priorityClassName,NODE:.spec.nodeName' \
    --no-headers > "$CUR_DIR/stdout/preemption.txt" 2>/dev/null
  sched_cleanup
  end
}

# OBSOLETE-BY-DESIGN, not in the default set (full story: gaps.md §7 终章).
# Under Option B the admission owner-gate FORBIDS the very placement this case
# submits (tenant-direct pods onto ARISE nodes) — the claimer pods are rejected
# at admission and the reclaim path is never reached, so the case can no longer
# measure what it was written to measure. Kept runnable for archaeology and for
# re-evaluation if reclaim ever regains a product role (capacity plugin path).
test_SCH_10() {
  begin SCH-10 P2 "reclaim: internal yields to contract (OBSOLETE-BY-DESIGN, see gaps §7)"
  sched_cleanup
  # arise-internal is reclaimable; direct-customer is not. Internal fills the
  # fleet, then a customer job arrives. The customer must get capacity without
  # anyone deleting anything by hand.
  # Sized against the namespace quota, not against the fleet: tenant-arise is
  # capped at 24 simulated GPUs, so 2x8 + 2x8 would have its last pod refused
  # at admission and the fleet would never actually fill. 2x8 + 2x4 = 24 uses
  # the entitlement exactly and leaves 8 GPUs free on pair 03-04.
  gang_submit int-a tenant-arise arise-internal "01-02" 8 arise-best-effort
  gang_submit int-b tenant-arise arise-internal "03-04" 4 arise-best-effort
  local filled=0
  for _ in $(seq 1 24); do
    sleep 5
    local r=$(( $(running_count tenant-arise gang=int-a) + $(running_count tenant-arise gang=int-b) ))
    [[ "$r" == "4" ]] && { filled=1; break; }
  done
  if [[ "$filled" != "1" ]]; then
    blocked "internal work did not fill the fleet; reclaim is untestable from here"
    sched_cleanup; end; return
  fi
  ok "arise-internal holds its full 24-GPU entitlement"

  # The customer needs 16 on pair 03-04 where only 8 are free. The remaining 8
  # can only come from reclaiming arise-internal, which is the queue property
  # under test: reclaimable=true yields, direct-customer's false never does.
  gang_submit cust tenant-direct direct-customer "03-04" 8 arise-contract-bound
  local got=0
  for _ in $(seq 1 36); do
    sleep 5
    [[ "$(running_count tenant-direct gang=cust)" == "2" ]] && { got=1; break; }
  done
  assert_eq "$got" "1" "contracted job scheduled after reclaim"
  local left; left=$(( $(running_count tenant-arise gang=int-a) + $(running_count tenant-arise gang=int-b) ))
  note "internal pods still running after reclaim: $left of 4"
  if (( got == 1 )); then
    assert_eq "$(( left < 4 ? 1 : 0 ))" "1" "internal capacity was actually reclaimed"
  fi
  $K get queue -o custom-columns='NAME:.metadata.name,STATE:.status.state,RUNNING:.status.running,PENDING:.status.pending' \
    --no-headers > "$CUR_DIR/metrics/queues.txt" 2>/dev/null
  sched_cleanup
  end
}


test_SCH_06() {
  begin SCH-06 P0 "gang scheduling: all-or-nothing, never half-started"
  sched_cleanup
  # Phase 1 — a whole pair: both members must run.
  gang_submit gang-0102 tenant-arise arise-internal "01-02" 8
  local ok1=0
  for _ in $(seq 1 20); do
    sleep 5
    [[ "$(running_count tenant-arise gang=gang-0102)" == "2" ]] && { ok1=1; break; }
  done
  assert_eq "$ok1" "1" "both members Running when the pair is whole"
  assert_eq "$(nodes_used tenant-arise gang=gang-0102 | wc -l)" "2" \
    "members on two distinct nodes"
  sched_cleanup

  # Phase 2 — shrink one member. The property under test is ZERO, not one:
  # a single started member would hold half a pair while producing nothing,
  # and on real hardware would also hold NVLink/IB the other job needs.
  set_device_health dgx02-fake-gpu-0 unhealthy
  if ! wait_for 60 "7" get node "$(node_for dgx02)" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}'; then
    blocked "could not shrink dgx02"; set_device_health dgx02-fake-gpu-0 healthy; sched_cleanup; end; return
  fi
  note "dgx02 reduced 8 -> 7; one member can no longer fit"
  gang_submit gang-0102 tenant-arise arise-internal "01-02" 8
  sleep 45
  assert_eq "$(running_count tenant-arise gang=gang-0102)" "0" "ZERO members started"
  local pg; pg=$($K -n tenant-arise get podgroup gang-0102 -o jsonpath='{.status.phase}' 2>/dev/null)
  note "PodGroup phase = ${pg:-<none>}"
  $K -n tenant-arise get pods -l gang=gang-0102 \
    -o custom-columns='POD:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName' \
    --no-headers > "$CUR_DIR/stdout/gang-blocked.txt" 2>/dev/null

  # Phase 3 — restore: both start together.
  set_device_health dgx02-fake-gpu-0 healthy
  wait_for 60 "8" get node "$(node_for dgx02)" -o jsonpath='{.status.allocatable.arise\.dev/fake-gpu}' >/dev/null
  local ok3=0
  for _ in $(seq 1 24); do
    sleep 5
    [[ "$(running_count tenant-arise gang=gang-0102)" == "2" ]] && { ok3=1; break; }
  done
  assert_eq "$ok3" "1" "both start together once capacity returns"
  sched_cleanup
  end
}

test_OBS_01() {
  begin OBS-01 P0 "ownership / contract / capacity metrics are complete"
  local n
  n=$(prom_q 'count(count by (node) (arise_node_owner))' | python3 -c "import json,sys;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)" 2>/dev/null)
  assert_eq "${n:-0}" "4" "arise_node_owner covers all four managed nodes"

  # Every node must sum to exactly 1 — the single-owner invariant, observable.
  local bad
  bad=$(prom_q 'count(sum by (node) (arise_node_owner) != 1)' | python3 -c "import json,sys;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)" 2>/dev/null)
  assert_eq "${bad:-0}" "0" "no node violates exactly-one-owner"

  local gpu
  gpu=$(prom_q 'sum(arise_fake_gpu_healthy)' | python3 -c "import json,sys;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)" 2>/dev/null)
  assert_eq "${gpu:-0}" "32" "arise_fake_gpu_healthy totals 32"

  # Cardinality guard (plan §9.2): labels must not carry pod UIDs, raw errors,
  # customer names or contract text. Series counts stay small and bounded.
  local series
  series=$(prom_q 'count({__name__=~"arise_.*"})' | python3 -c "import json,sys;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)" 2>/dev/null)
  note "arise_* series count = ${series}"
  if (( ${series:-0} > 0 && ${series:-0} < 500 )); then
    ok "series cardinality bounded (${series} < 500)"
  else
    fail "series cardinality suspicious: ${series}"
  fi
  prom_q 'arise_node_owner' > "$CUR_DIR/metrics/arise_node_owner.json" 2>/dev/null
  end
}

test_OBS_02() {
  begin OBS-02 P0 "OwnerConflict P0 alert actually fires and reaches Alertmanager"
  # dgx01 has no NodeOwnership CR, so nothing will auto-correct the label out
  # from under the test. Stripping it makes every owner series 0, so
  # sum by (node) == 0 != 1 and the rule must fire after its 1m `for`.
  local kn="$(node_for dgx01)"
  local orig; orig=$($K get node "$kn" -o jsonpath='{.metadata.labels.arise\.ai/owner}')
  note "dgx01 owner label was '$orig'; removing it to induce the conflict"
  $K label node "$kn" arise.ai/owner- >/dev/null 2>&1

  # Wait for state=firing specifically. An alert in `pending` has merely
  # started its `for:` timer — it has not fired and nothing has been sent
  # anywhere. Accepting "appears in /alerts" would pass on pending alone.
  local fired=0 state=""
  for _ in $(seq 1 24); do
    sleep 10
    state=$(prom_alerts | python3 -c "
import json,sys
try:
    for a in json.load(sys.stdin)['data']['alerts']:
        if a['labels'].get('alertname')=='OwnerConflict': print(a['state']); break
except Exception: pass" 2>/dev/null)
    [[ "$state" == "firing" ]] && { fired=1; break; }
  done
  note "final Prometheus alert state = ${state:-<absent>}"
  assert_eq "$fired" "1" "OwnerConflict reached state=firing (not merely pending)"

  if (( fired )); then
    prom_alerts > "$CUR_DIR/metrics/prometheus-alerts.json" 2>/dev/null
    # Reaching Alertmanager is what proves the pipeline, not just the rule.
    local delivered=0
    for _ in $(seq 1 18); do
      sleep 10
      if am_alerts | grep -q OwnerConflict; then delivered=1; break; fi
    done
    assert_eq "$delivered" "1" "alert delivered to Alertmanager within SLA"
    am_alerts > "$CUR_DIR/metrics/alertmanager-alerts.json" 2>/dev/null
    local sev
    sev=$(am_alerts | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    if a['labels'].get('alertname')=='OwnerConflict': print(a['labels'].get('severity')); break" 2>/dev/null)
    assert_eq "$sev" "P0" "alert carries severity=P0"
  fi

  # restore
  $K label node "$kn" "arise.ai/owner=${orig:-ARISE}" --overwrite >/dev/null 2>&1
  note "restored dgx01 owner=${orig:-ARISE}"
  # NOTE: a `wait_for 120 "1" get --raw "/readyz"` used to sit here. It could
  # never succeed — /readyz returns the string "ok", never "1" — so it always
  # burned the full 120 s timeout and then swallowed the failure with `|| true`.
  # Two minutes of dead time per matrix run for nothing. The restore above is
  # synchronous (kubectl label returns after the write), and the next test does
  # its own fixture setup, so no wait is needed here at all.
  end
}


# ---- console helpers -------------------------------------------------------
# Payload goes through an env var rather than being interpolated into the
# python source: embedding JSON inside nested quoting is how the first draft of
# this file broke, and a test helper that is hard to quote correctly will be
# quoted incorrectly.
ui_get() {  # ui_get <path>
  $K -n platform-system exec deploy/ops-console -- python3 -c "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:8080$1',timeout=10).read().decode())" 2>/dev/null
}

ui_post() {  # ui_post <path> <json>  -> "<status> <body>"
  $K -n platform-system exec deploy/ops-console -- env PAYLOAD="$2" python3 -c "
import os,urllib.request,urllib.error
req=urllib.request.Request('http://127.0.0.1:8080$1',method='POST')
req.add_header('Content-Type','application/json')
req.data=os.environ['PAYLOAD'].encode()
try:
    r=urllib.request.urlopen(req,timeout=10); print(r.status, r.read().decode())
except urllib.error.HTTPError as e: print(e.code, e.read().decode())" 2>/dev/null
}

test_UI_01() {
  begin UI-01 P0 "console cannot bypass the state machine"
  # 1. RBAC — the console identity must not be able to write observed state.
  local sa="system:serviceaccount:platform-system:ops-console"
  for probe in "patch nodes" "update nodes" "create pods/eviction" "delete nodeownerships"; do
    # shellcheck disable=SC2086
    assert_eq "$($K auth can-i $probe --as=$sa 2>/dev/null)" "no" "console cannot: $probe"
  done
  assert_eq "$($K auth can-i patch nodeownerships --as=$sa 2>/dev/null)" "yes" \
    "console CAN write desired state"

  # 2. Audit — an ownership change with no approver must be refused.
  local r; r=$(ui_post /api/transition '{"nodeId":"dgx01","desiredOwner":"VAST"}')
  assert_contains "$r" "400" "transition without an approver is refused"
  assert_contains "$r" "approvedBy" "refusal names the missing field"

  # 3. THE ONE THAT MATTERS — a contract-blocked transition must be refused
  #    SERVER-SIDE, not merely hidden in the UI. A disabled button is a UX
  #    affordance; an operator with curl is not bound by it.
  if ! fixture_vast_rented dgx04 "$(node_for dgx04)" "t-ui01-$(date +%s)"; then
    blocked "could not establish a VAST_RENTED fixture on dgx04"; end; return
  fi
  note "dgx04 is VAST_RENTED with a live contract"

  local fleet; fleet=$(ui_get /api/fleet)
  local allowed; allowed=$(printf '%s' "$fleet" | python3 -c "
import json,sys
for n in json.load(sys.stdin)['nodes']:
    if n['nodeId']=='dgx04': print(n['gates']['ARISE']['allowed']); break" 2>/dev/null)
  assert_eq "$allowed" "False" "console marks ARISE unavailable for dgx04"
  local why; why=$(printf '%s' "$fleet" | python3 -c "
import json,sys
for n in json.load(sys.stdin)['nodes']:
    if n['nodeId']=='dgx04': print(' | '.join(n['gates']['ARISE']['reasons'])); break" 2>/dev/null)
  assert_contains "$why" "active contract" "reason explains the contract gate"
  note "gate reason: ${why:0:110}"

  # Attempt it anyway, straight at the API.
  r=$(ui_post /api/transition '{"nodeId":"dgx04","desiredOwner":"ARISE","approvedBy":"tests@ariselabs.ai"}')
  assert_contains "$r" "409" "direct API call refused server-side (409)"
  assert_contains "$r" "blocked by gate" "refusal cites the gate"
  printf '%s\n' "$r" > "$CUR_DIR/response/blocked-transition.json" 2>/dev/null

  # The node must be untouched by the attempt.
  assert_eq "$($K get node $(node_for dgx04) -o jsonpath='{.metadata.labels.arise\.ai/owner}')" \
    "VAST" "node owner unchanged after the refused attempt"
  assert_eq "$(mock_field dgx04 activeContracts)" "1" "contract still intact"

  # 4. A PERMITTED transition still goes through the controller, not the UI.
  r=$(ui_post /api/transition '{"nodeId":"dgx01","desiredOwner":"VAST","approvedBy":"tests@ariselabs.ai","reason":"UI-01"}')
  assert_contains "$r" "202" "permitted transition is accepted"
  sleep 8
  assert_eq "$($K get nodeownership dgx01 -o jsonpath='{.spec.desiredOwner}' 2>/dev/null)" "VAST" \
    "console wrote DESIRED state only"
  assert_eq "$($K get nodeownership dgx01 -o jsonpath='{.spec.approvedBy}' 2>/dev/null)" \
    "tests@ariselabs.ai" "approver recorded for audit"
  $K get nodeownership dgx01 -o yaml > "$CUR_DIR/response/dgx01-nodeownership.yaml" 2>/dev/null

  fixture_clean_arise dgx01 "$(node_for dgx01)"
  fixture_clean_arise dgx04 "$(node_for dgx04)"
  end
}


graf() {  # graf <path>
  $K -n monitoring exec deploy/grafana -- wget -qO- "http://127.0.0.1:3000$1" 2>/dev/null
}

test_OBS_04() {
  begin OBS-04 P1 "Grafana provisions from empty state with stable UIDs"
  # Plan §9.1/OBS-04: datasource and dashboard must come from files, with UIDs
  # that survive a rebuild. A dashboard assembled by clicking would not
  # reproduce, and E2E-01 compares dashboard UIDs across two rebuilds.
  local health; health=$(graf /api/health)
  assert_contains "$health" '"database": "ok"' "grafana healthy"

  local dsuid; dsuid=$(graf /api/datasources | python3 -c "
import json,sys
ds=json.load(sys.stdin)
print(ds[0]['uid'] if ds else '')" 2>/dev/null)
  assert_eq "$dsuid" "arise-prometheus" "datasource UID is pinned, not generated"

  local dash; dash=$(graf /api/dashboards/uid/arise-b300-fleet)
  local panels; panels=$(printf '%s' "$dash" | python3 -c "
import json,sys
try: print(len(json.load(sys.stdin)['dashboard']['panels']))
except Exception: print(0)" 2>/dev/null)
  assert_eq "$panels" "8" "dashboard provisioned with all panels"

  # Read-only by construction: allowUiUpdates=false means the file wins.
  local prov; prov=$(printf '%s' "$dash" | python3 -c "
import json,sys
try: print(json.load(sys.stdin)['meta'].get('provisioned'))
except Exception: print('?')" 2>/dev/null)
  assert_eq "$prov" "True" "dashboard is marked provisioned (not UI-editable)"

  # The part that actually proves the wiring: query Prometheus THROUGH the
  # provisioned datasource. A dashboard that loads but cannot reach its
  # datasource looks fine and shows nothing.
  local v; v=$(graf '/api/datasources/proxy/uid/arise-prometheus/api/v1/query?query=sum(arise_fake_gpu_healthy)' \
    | python3 -c "
import json,sys
r=json.load(sys.stdin)['data']['result']
print(r[0]['value'][1] if r else 'nodata')" 2>/dev/null)
  assert_eq "$v" "32" "live query through the provisioned datasource returns data"

  graf /api/search?query= > "$CUR_DIR/response/grafana-search.json" 2>/dev/null
  printf '%s\n' "$dash" > "$CUR_DIR/response/grafana-dashboard.json" 2>/dev/null
  end
}


# A customer workload on a reserved node. It must tolerate the DIRECT taint —
# that toleration is part of the customer job contract, and is what makes the
# reservation usable by its owner while remaining closed to everyone else.
direct_submit() {  # direct_submit <name> <node-id> <gpu>
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: $1
  namespace: tenant-direct
  labels: { arise.ai/test: "true", cust: "$1" }
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 2
  priorityClassName: arise-contract-bound
  nodeSelector: { arise.ai/node-id: "$2", arise.ai/owner: DIRECT }
  tolerations:
    - { key: arise.ai/direct-owned, operator: Equal, value: "true", effect: NoSchedule }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: c
      image: $IMG
      command: [sleep,'600']
      resources:
        requests: { cpu: 500m, memory: 512Mi, arise.dev/fake-gpu: "$3" }
        limits:   { cpu: "1", memory: 1Gi, arise.dev/fake-gpu: "$3" }
      securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
Y
}

test_DIR_01() {
  begin DIR-01 P0 "DIRECT reservation guarantees customer capacity"
  # This is the mechanism the platform relies on after cross-queue reclaim was
  # found not to deliver customer guarantees (gaps.md §7, option B). The
  # guarantee has to be structural: whole nodes, enforced by admission and
  # taint, not a fairness share that can be renegotiated under load.
  sched_cleanup
  local KN="$(node_for dgx04)"
  fixture_clean_arise dgx04 "$KN"

  # 1. Internal work is running on the node we are about to reserve.
  gang_submit pre-int tenant-arise arise-internal "03-04" 8 arise-best-effort
  local up=0
  for _ in $(seq 1 20); do sleep 5; [[ "$(running_count tenant-arise gang=pre-int)" == "2" ]] && { up=1; break; }; done
  if [[ "$up" != "1" ]]; then blocked "internal baseline did not start"; sched_cleanup; end; return; fi
  ok "internal work occupies pair 03-04 before the reservation"

  # 2. Reserve the node for a Direct customer.
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: infrastructure.arise.ai/v1alpha1
kind: NodeOwnership
metadata: { name: dgx04 }
spec:
  desiredOwner: DIRECT
  transitionId: t-dir01-$(date +%s)
  pair: "03-04"
  approvedBy: tests@ariselabs.ai
  reason: "customer reservation"
Y
  if ! wait_for 180 "DIRECT_ASSIGNED" get nodeownership dgx04 -o jsonpath='{.status.phase}'; then
    fail "did not reach DIRECT_ASSIGNED; phase=$(nown_phase dgx04)"
    $K -n tenant-arise get pods -l gang=pre-int -o wide --no-headers \
      > "$CUR_DIR/stdout/drain-stuck.txt" 2>/dev/null
    sched_cleanup; end; return
  fi
  ok "reached DIRECT_ASSIGNED"

  # 3. The node must be isolated AND usable: tainted, labelled, NOT cordoned.
  #    A cordoned reservation denies the customer the capacity they paid for,
  #    which is as much a breach as letting someone else onto it.
  assert_eq "$($K get node "$KN" -o jsonpath='{.metadata.labels.arise\.ai/owner}')" "DIRECT" \
    "owner label is DIRECT"
  local taints; taints=$($K get node "$KN" -o jsonpath='{.spec.taints[*].key}')
  assert_contains "$taints" "arise.ai/direct-owned" "DIRECT taint applied"
  assert_eq "$($K get node "$KN" -o jsonpath='{.spec.unschedulable}')" "" \
    "node NOT cordoned — the customer must be able to schedule"
  # Count only what is ON the reserved node. The gang's other member sits on
  # dgx03, which was never reserved and must keep running — draining it would
  # be over-reach, not thoroughness.
  local on_node; on_node=$($K -n tenant-arise get pods -l gang=pre-int \
    --field-selector="spec.nodeName=$KN,status.phase=Running" \
    --no-headers 2>/dev/null | wc -l)
  assert_eq "$on_node" "0" "internal work drained off the RESERVED node"
  local elsewhere; elsewhere=$(running_count tenant-arise gang=pre-int)
  if (( elsewhere > 0 )); then
    note "NOTE: ${elsewhere} member(s) of the internal gang still run on the \
un-reserved node. Same stranded-gang hazard as SCH-05: a minMember=2 job with \
one member drained can never re-form. Reserving a node out from under a \
running gang must be paired with the vcjob PodEvicted->RestartJob policy."
  fi

  # 4. Internal work can no longer target it — admission refuses before the
  #    scheduler is even consulted.
  assert_rejected "may only target nodes with arise.ai/owner=ARISE" \
    "internal workload cannot target the reserved node" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: intruder, namespace: tenant-arise }
spec:
  nodeSelector: { arise.ai/owner: DIRECT }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'1'],
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"

  # 5. The customer CAN use it — the whole point of the reservation.
  direct_submit cust-job dgx04 8
  local got=0
  for _ in $(seq 1 24); do sleep 5; [[ "$(running_count tenant-direct cust=cust-job)" == "1" ]] && { got=1; break; }; done
  assert_eq "$got" "1" "customer workload runs on its reserved node"
  local where; where=$($K -n tenant-direct get pod cust-job -o jsonpath='{.spec.nodeName}' 2>/dev/null)
  assert_eq "$where" "$KN" "customer landed on the reserved node specifically"
  $K get node "$KN" -o custom-columns='NODE:.metadata.name,OWNER:.metadata.labels.arise\.ai/owner,CORDON:.spec.unschedulable,TAINTS:.spec.taints[*].key' \
    --no-headers > "$CUR_DIR/stdout/reserved-node.txt" 2>/dev/null

  # 6. Release: back to ARISE, taint gone, internal work可以再进来.
  $K -n tenant-direct delete pod cust-job --grace-period=1 --ignore-not-found >/dev/null 2>&1
  sleep 5
  $K patch nodeownership dgx04 --type=merge \
    -p '{"spec":{"desiredOwner":"ARISE","transitionId":"t-dir01-release","requireSanitization":true}}' >/dev/null
  if wait_for 180 "READY" get nodeownership dgx04 -o jsonpath='{.status.phase}'; then
    ok "released back to ARISE"
  else
    fail "release did not reach READY; phase=$(nown_phase dgx04)"
  fi
  local t2; t2=$($K get node "$KN" -o jsonpath='{.spec.taints[*].key}')
  assert_not_contains "$t2" "arise.ai/direct-owned" "DIRECT taint removed on release"
  assert_eq "$($K get node "$KN" -o jsonpath='{.metadata.labels.arise\.ai/owner}')" "ARISE" \
    "owner returned to ARISE"
  capture_events dgx04
  sched_cleanup
  fixture_clean_arise dgx04 "$KN"
  end
}


test_DIR_02() {
  begin DIR-02 P0 "VAST->DIRECT unlists first: a rentable node is never dual-owned"
  # Audit 2026-08 (C2): reserving a VAST-LISTED but idle node for a Direct
  # customer without unlisting first leaves it rentable on VAST while k8s hands
  # it to the customer -> two owners on one GPU node, invisible to OwnerConflict
  # (the label reads a single owner). The controller must unlist AND confirm
  # before the handover.
  local node=dgx03 KN="$(node_for dgx03)"
  fixture_clean_arise dgx03 "$KN"

  # 1. List the node on VAST (idle, rentable).
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: infrastructure.arise.ai/v1alpha1
kind: NodeOwnership
metadata: { name: $node }
spec: { desiredOwner: VAST, transitionId: t-dir02-vast, pair: "03-04", approvedBy: tests }
Y
  if ! wait_for 90 "VAST_READY" get nodeownership $node -o jsonpath='{.status.phase}'; then
    blocked "handover did not reach VAST_READY; cannot test the DIRECT unlist gate"
    end; return
  fi
  assert_eq "$(mock_field dgx03 listed)" "True" "node is listed (rentable) on VAST before reservation"
  assert_eq "$(mock_field dgx03 activeContracts)" "0" "node idle (no active contract)"

  # 2. Reserve the still-listed node for a Direct customer.
  $K patch nodeownership $node --type=merge \
    -p '{"spec":{"desiredOwner":"DIRECT","transitionId":"t-dir02-direct"}}' >/dev/null
  if ! wait_for 180 "DIRECT_ASSIGNED" get nodeownership $node -o jsonpath='{.status.phase}'; then
    fail "did not reach DIRECT_ASSIGNED; phase=$(nown_phase $node)"; sched_cleanup
    fixture_clean_arise dgx03 "$KN"; end; return
  fi
  ok "reached DIRECT_ASSIGNED"

  # 3. THE FIX: the machine must have been UNLISTED before handover, so VAST can
  #    never match a fresh contract onto the node we just gave the customer.
  assert_eq "$(mock_field dgx03 listed)" "False" \
    "node UNLISTED from VAST before the DIRECT handover (no dual ownership)"
  assert_eq "$($K get node "$KN" -o jsonpath='{.metadata.labels.arise\.ai/owner}')" "DIRECT" \
    "owner label is DIRECT"
  assert_eq "$($K get node "$KN" -o jsonpath='{.spec.unschedulable}')" "" \
    "node NOT cordoned — the customer can schedule"
  # the reclaimed VAST node must shed its vast-owned NoSchedule taint, or the
  # Direct customer cannot actually place pods on the node they reserved.
  local dtaints; dtaints=$($K get node "$KN" -o jsonpath='{.spec.taints[*].key}')
  assert_not_contains "$dtaints" "arise.ai/vast-owned" \
    "stale VAST NoSchedule taint removed — customer can schedule on the reserved node"
  capture_events $node
  mock_get /v1/machines/dgx03 > "$CUR_DIR/response/dir02-machine.json" 2>/dev/null
  fixture_clean_arise dgx03 "$KN"
  end
}


test_FLV_01() {
  begin FLV-01 P0 "flavor quantization: off-grid requests rejected, on-grid accepted"
  # The anti-fragmentation gate. Custom sizes are allowed — that is the
  # product promise — but only on the 500m / 512Mi / 10Gi grid, because a
  # fleet full of 300m/700Mi remainders ends up with nothing whole to rent.
  assert_rejected "multiple of 500m" "300m CPU rejected (off-grid)" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-flv-cpu, namespace: tenant-arise }
spec:
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'1'],
                 resources: { requests: { cpu: 300m, memory: 512Mi }, limits: { cpu: 300m, memory: 512Mi } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  assert_rejected "multiple of 512Mi" "600Mi memory rejected (off-grid)" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-flv-mem, namespace: tenant-arise }
spec:
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'1'],
                 resources: { requests: { cpu: 500m, memory: 600Mi }, limits: { cpu: 500m, memory: 600Mi } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  # Custom-but-on-grid must be ACCEPTED: 1.5 cores, 3.5 GiB (= 7 x 512Mi).
  # This is the difference from Volcengine's closed catalog — the user asked
  # for custom sizes, so the grid is the rule, not a flavor list.
  $K -n tenant-arise delete pod t-flv-ok --ignore-not-found --wait=true >/dev/null 2>&1
  assert_accepted "custom on-grid size accepted (1500m / 3584Mi)" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-flv-ok, namespace: tenant-arise, labels: { arise.ai/test: 'true' } }
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 2
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'30'],
                 resources: { requests: { cpu: 1500m, memory: 3584Mi }, limits: { cpu: '2', memory: 4Gi } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  # PVC grid: 15Gi off, 20Gi on; class outside the product's two rejected.
  assert_rejected "multiple of 10Gi" "15Gi PVC rejected (off-grid)" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: t-flv-pvc-bad, namespace: tenant-arise }
spec:
  storageClassName: arise-shared
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 15Gi } }
Y"
  assert_rejected "must be arise-shared" "non-product storage class rejected" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: t-flv-pvc-cls, namespace: tenant-arise }
spec:
  storageClassName: standard
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 20Gi } }
Y"
  # Platform namespaces stay exempt: a 25m metrics sidecar must NOT be forced
  # up to 500m — quantizing infra would waste what the gate protects.
  local prom_cpu; prom_cpu=$($K -n monitoring get deploy prometheus \
    -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null)
  assert_eq "$prom_cpu" "300m" "platform namespaces exempt from the grid"
  $K -n tenant-arise delete pod t-flv-ok --grace-period=1 --ignore-not-found >/dev/null 2>&1
  end
}

test_FLV_02() {
  begin FLV-02 P0 "pure-CPU rental lands on the CPU pool, never on a GPU node"
  # The product sells CPU-only containers (small/large/custom). They must be
  # placeable on the dedicated CPU nodes so GPU-node CPU stays with the GPUs —
  # a GPU node whose cores are eaten by CPU rentals can't sell its cards.
  $K -n tenant-arise delete pod t-cpu-rental --ignore-not-found --wait=true >/dev/null 2>&1
  cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-cpu-rental, namespace: tenant-arise, labels: { arise.ai/test: 'true' } }
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 2
  nodeSelector: { arise.ai/role: cpu }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: c
      image: $IMG
      command: [sleep,'60']
      resources:
        requests: { cpu: '2', memory: 2Gi }
        limits:   { cpu: '2', memory: 2Gi }
      securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
Y
  local ok=0 where=""
  for _ in $(seq 1 20); do
    sleep 3
    [[ "$($K -n tenant-arise get pod t-cpu-rental -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && { ok=1; break; }
  done
  assert_eq "$ok" "1" "2-core/2Gi CPU rental is Running"
  where=$($K -n tenant-arise get pod t-cpu-rental -o jsonpath='{.spec.nodeName}' 2>/dev/null)
  local role; role=$($K get node "$where" -o jsonpath='{.metadata.labels.arise\.ai/role}' 2>/dev/null)
  assert_eq "$role" "cpu" "landed on a CPU-pool node ($where)"
  # And the storage node is closed to it even with a matching selector:
  assert_accepted "storage-node probe created (will stay Pending)" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: Pod
metadata: { name: t-stor-probe, namespace: tenant-arise, labels: { arise.ai/test: 'true' } }
spec:
  restartPolicy: Never
  nodeSelector: { arise.ai/role: storage }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, seccompProfile: { type: RuntimeDefault } }
  containers: [{ name: c, image: $IMG, command: [sleep,'30'],
                 resources: { requests: { cpu: 500m, memory: 512Mi }, limits: { cpu: 500m, memory: 512Mi } },
                 securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } } }]
Y"
  sleep 12
  assert_eq "$($K -n tenant-arise get pod t-stor-probe -o jsonpath='{.status.phase}' 2>/dev/null)" \
    "Pending" "tenant pod cannot run on the tainted storage node"
  $K -n tenant-arise delete pod t-cpu-rental t-stor-probe --grace-period=1 --ignore-not-found >/dev/null 2>&1
  end
}

test_FLV_03() {
  begin FLV-03 P1 "long-term storage: data survives the workload that wrote it"
  # The 'long-term allocation' product path: a CPU rental with an
  # arise-longterm volume. The claim outlives any single pod; data written by
  # one pod is read by the next. (Control-surface test only — the DATA PATH is
  # kind local-path, not the real storage pool. See storage.yaml header.)
  $K -n tenant-arise delete pod t-lt-writer t-lt-reader --grace-period=1 --ignore-not-found >/dev/null 2>&1
  $K -n tenant-arise delete pvc t-lt-data --ignore-not-found >/dev/null 2>&1
  sleep 3
  assert_accepted "20Gi long-term claim accepted" -- \
    bash -c "cat <<'Y' | $K apply -f - 2>&1
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: t-lt-data, namespace: tenant-arise }
spec:
  storageClassName: arise-longterm
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 20Gi } }
Y"
  lt_pod() {  # lt_pod <name> <python-code>
    cat <<Y | $K apply -f - >/dev/null 2>&1
apiVersion: v1
kind: Pod
metadata: { name: $1, namespace: tenant-arise, labels: { arise.ai/test: 'true' } }
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 2
  nodeSelector: { arise.ai/role: cpu }
  securityContext: { runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532, fsGroup: 65532, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: c
      image: $IMG
      command: [python3, -c, "$2"]
      resources:
        requests: { cpu: 500m, memory: 512Mi }
        limits:   { cpu: 500m, memory: 512Mi }
      securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
      volumeMounts: [{ name: data, mountPath: /data }]
  volumes:
    - name: data
      persistentVolumeClaim: { claimName: t-lt-data }
Y
  }
  lt_pod t-lt-writer "open('/data/model.bin','w').write('weights-v1'); print('wrote')"
  local wok=0
  for _ in $(seq 1 30); do
    sleep 3
    [[ "$($K -n tenant-arise get pod t-lt-writer -o jsonpath='{.status.phase}' 2>/dev/null)" == "Succeeded" ]] && { wok=1; break; }
  done
  assert_eq "$wok" "1" "writer pod wrote to the long-term volume and exited"
  $K -n tenant-arise delete pod t-lt-writer --grace-period=1 --ignore-not-found >/dev/null 2>&1

  lt_pod t-lt-reader "print('READBACK='+open('/data/model.bin').read())"
  local rok=0
  for _ in $(seq 1 30); do
    sleep 3
    [[ "$($K -n tenant-arise get pod t-lt-reader -o jsonpath='{.status.phase}' 2>/dev/null)" == "Succeeded" ]] && { rok=1; break; }
  done
  assert_eq "$rok" "1" "a later pod could mount the same claim"
  local got; got=$($K -n tenant-arise logs t-lt-reader 2>/dev/null | tr -d '\r')
  assert_contains "$got" "READBACK=weights-v1" "data written by the first pod survived to the second"
  # Retain semantics: the PV must be set to Retain, so even deleting the
  # claim would leave the data for operator recovery.
  local pv; pv=$($K -n tenant-arise get pvc t-lt-data -o jsonpath='{.spec.volumeName}' 2>/dev/null)
  assert_eq "$($K get pv "$pv" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}' 2>/dev/null)" \
    "Retain" "backing volume is Retain — deletion needs an operator, by design"
  $K -n tenant-arise get pvc t-lt-data -o yaml > "$CUR_DIR/response/pvc.yaml" 2>/dev/null
  $K -n tenant-arise delete pod t-lt-reader --grace-period=1 --ignore-not-found >/dev/null 2>&1
  $K -n tenant-arise delete pvc t-lt-data --ignore-not-found >/dev/null 2>&1
  end
}


portal_get() {  # portal_get <path>
  $K -n platform-system exec deploy/tenant-portal -- python3 -c "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:8080$1',timeout=15).read().decode())" 2>/dev/null
}

portal_post() {  # portal_post <path> <json> -> "<status> <body>"
  $K -n platform-system exec deploy/tenant-portal -- env PAYLOAD="$2" python3 -c "
import os,urllib.request,urllib.error
req=urllib.request.Request('http://127.0.0.1:8080$1',method='POST')
req.add_header('Content-Type','application/json')
req.data=os.environ['PAYLOAD'].encode()
try:
    r=urllib.request.urlopen(req,timeout=20); print(r.status, r.read().decode())
except urllib.error.HTTPError as e: print(e.code, e.read().decode())" 2>/dev/null
}

portal_delete() {  # portal_delete <path>
  $K -n platform-system exec deploy/tenant-portal -- python3 -c "
import urllib.request,urllib.error
req=urllib.request.Request('http://127.0.0.1:8080$1',method='DELETE')
try:
    r=urllib.request.urlopen(req,timeout=20); print(r.status, r.read().decode())
except urllib.error.HTTPError as e: print(e.code, e.read().decode())" 2>/dev/null
}

test_UI_02() {
  begin UI-02 P0 "tenant portal: full workload lifecycle, zero policy bypass"
  # ---- 1. RBAC: the portal identity's reach is workloads-only --------------
  local sa="system:serviceaccount:platform-system:tenant-portal"
  for probe in "patch nodes" "list nodes" "create nodeownerships" \
               "delete nodeownerships" "create queues" \
               "get pods -n platform-system" "get pods -n vast-mock"; do
    # shellcheck disable=SC2086
    assert_eq "$($K auth can-i $probe --as=$sa 2>/dev/null)" "no" "portal cannot: $probe"
  done
  assert_eq "$($K auth can-i create jobs.batch.volcano.sh -n tenant-arise --as=$sa 2>/dev/null)" \
    "yes" "portal CAN create jobs in a tenant namespace"

  # ---- 2. the platform gates fire THROUGH the portal -----------------------
  local r
  # The portal API works in REAL hardware units (whole vCPU / GiB). Millicore
  # strings are refused at the API schema, and off-grid storage still hits the
  # platform VAP — schema errors are the portal's, policy errors the server's.
  r=$(portal_post '/api/devmachines?ns=tenant-arise' '{"name":"ui2-offgrid","cpu":"300m","memory":"2Gi"}')
  assert_contains "$r" "400" "millicore value refused (whole-vCPU API)"
  assert_contains "$r" "whole vCPUs" "refusal explains the unit model"
  r=$(portal_post '/api/volumes?ns=tenant-arise' '{"name":"ui2-offgrid-vol","sizeGi":15}')
  assert_contains "$r" "multiple of 10Gi" "off-grid volume rejected through the portal"
  r=$(portal_post '/api/devmachines?ns=nonexistent' '{"name":"x","cpu":"1","memory":"1Gi"}')
  assert_contains "$r" "403" "unknown namespace refused"

  # ---- 3. happy path: job + devmachine + volume all reach Running ----------
  $K -n tenant-arise delete pod ui2-dev --grace-period=1 --ignore-not-found >/dev/null 2>&1
  $K -n tenant-arise delete pvc ui2-dev-data --ignore-not-found >/dev/null 2>&1
  $K -n tenant-arise delete job.batch.volcano.sh ui2-train --ignore-not-found >/dev/null 2>&1
  sleep 5
  r=$(portal_post '/api/jobs?ns=tenant-arise' \
      '{"name":"ui2-train","replicas":2,"gpu":4,"vcpu":128,"memGi":1024,"priority":"arise-reserved"}')
  assert_contains "$r" "201" "training job accepted (2x gpu.4 B300 slices)"
  assert_contains "$r" "arise-reserved" "priority recorded"
  assert_contains "$r" "arise-internal" "queue auto-selected from the tenant"
  r=$(portal_post '/api/devmachines?ns=tenant-arise' \
      '{"name":"ui2-dev","vcpu":4,"memGi":16,"volume":{"sizeGi":10,"class":"arise-longterm"}}')
  assert_contains "$r" "201" "devmachine + long-term volume accepted"

  local jok=0 dok=0
  for _ in $(seq 1 30); do
    sleep 5
    local jr; jr=$($K -n tenant-arise get pods -l arise.ai/workload=ui2-train \
      --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l)
    local dp; dp=$($K -n tenant-arise get pod ui2-dev -o jsonpath='{.status.phase}' 2>/dev/null)
    [[ "$jr" == "2" ]] && jok=1
    [[ "$dp" == "Running" ]] && dok=1
    [[ $jok -eq 1 && $dok -eq 1 ]] && break
  done
  assert_eq "$jok" "1" "gang job: both replicas Running"
  assert_eq "$dok" "1" "devmachine Running"
  local dev_node; dev_node=$($K -n tenant-arise get pod ui2-dev -o jsonpath='{.spec.nodeName}' 2>/dev/null)
  assert_eq "$($K get node "$dev_node" -o jsonpath='{.metadata.labels.arise\.ai/role}' 2>/dev/null)" \
    "cpu" "CPU-only devmachine landed on the CPU pool"

  # ---- 4. overview reflects reality ---------------------------------------
  local ov; ov=$(portal_get '/api/overview?ns=tenant-arise')
  assert_contains "$ov" '"ui2-train"' "overview lists the job"
  assert_contains "$ov" '"ui2-dev"' "overview lists the devmachine"
  assert_contains "$ov" 'requests.arise.dev/fake-gpu' "overview exposes quota usage"
  printf '%s\n' "$ov" > "$CUR_DIR/response/overview.json" 2>/dev/null

  # ---- 5. teardown through the portal (the user's own path) ----------------
  assert_contains "$(portal_delete '/api/jobs/ui2-train?ns=tenant-arise')" "200" \
    "job deleted via portal"
  assert_contains "$(portal_delete '/api/devmachines/ui2-dev?ns=tenant-arise')" "200" \
    "devmachine deleted via portal"
  assert_contains "$(portal_delete '/api/volumes/ui2-dev-data?ns=tenant-arise')" "200" \
    "volume deleted via portal"
  sleep 5
  end
}

test_NODE_01() {
  begin NODE-01 P0 "machine registration: deregister and onboard round trip"
  # The path real hardware will take, exercised end to end. cpu02 is the
  # guinea pig — it carries no state machine and no workloads.
  local KN="$(node_for cpu02)"

  # Predecessors in the matrix (UI-03's devmachine, FLV pods) may still be
  # Terminating on the cpu pool when we get here; deregister then correctly
  # REFUSES ("tenant pods still on node") and the whole test fails on a race,
  # not on behaviour — observed 2026-08-16. Deregistering only an EMPTY node
  # is the honest precondition, so wait (bounded) for the stragglers to end.
  local i
  for i in $(seq 1 30); do
    [[ -z "$($K get pods -A --field-selector spec.nodeName="$KN" \
              --no-headers 2>/dev/null | grep -E '^tenant-' )" ]] && break
    sleep 3
  done

  # -- deregister: node drops out of every pool ------------------------------
  assert_accepted "deregister accepted" -- ./scripts/onboard-node.sh "$KN" deregister
  assert_eq "$($K get node "$KN" -o jsonpath='{.metadata.labels.arise\.ai/role}')" "" \
    "role label stripped"
  # A CPU rental can no longer land on it: capacity really left the pool.
  local pool; pool=$($K get nodes -l arise.ai/role=cpu --no-headers 2>/dev/null | wc -l)
  assert_eq "$pool" "1" "CPU pool shrank to 1 node"

  # -- onboard back ----------------------------------------------------------
  assert_accepted "onboard as cpu02 accepted" -- ./scripts/onboard-node.sh "$KN" cpu cpu02
  assert_eq "$($K get node "$KN" -o jsonpath='{.metadata.labels.arise\.ai/role}')" "cpu" \
    "role restored"
  assert_eq "$($K get node "$KN" -o jsonpath='{.metadata.labels.arise\.ai/owner}')" "ARISE" \
    "owner label restored"
  assert_eq "$($K get nodes -l arise.ai/role=cpu --no-headers 2>/dev/null | wc -l)" "2" \
    "CPU pool back to 2 nodes"

  # -- GPU-node deregistration is guarded by the state machine ---------------
  # dgx01 currently has no NodeOwnership CR (only dgx03/dgx04 got them in
  # tests); create one via the ONBOARD path to prove gpu onboarding, then
  # verify deregister refuses while a synthetic contract is active.
  ./scripts/onboard-node.sh "$(node_for dgx01)" gpu dgx01 "01-02" >/dev/null 2>&1
  wait_for 90 "READY" get nodeownership dgx01 -o jsonpath='{.status.phase}' \
    && ok "gpu onboarding engaged the state machine (dgx01 READY)" \
    || fail "state machine did not pick up onboarded dgx01"
  # give it a live contract via the ownership path
  mock_post /v1/test/reset '{}' >/dev/null
  $K patch nodeownership dgx01 --type=merge \
    -p '{"spec":{"desiredOwner":"VAST","transitionId":"t-node01-vast"}}' >/dev/null
  if wait_for 120 "VAST_READY" get nodeownership dgx01 -o jsonpath='{.status.phase}'; then
    mock_post /v1/test/contracts '{"machineId":"dgx01","action":"create","durationSeconds":3600}' >/dev/null
    wait_for 60 "VAST_RENTED" get nodeownership dgx01 -o jsonpath='{.status.phase}' >/dev/null
    local out; out=$(./scripts/onboard-node.sh "$(node_for dgx01)" deregister 2>&1); local rc=$?
    assert_eq "$rc" "1" "deregister REFUSED while a contract is active"
    assert_contains "$out" "active contract" "refusal names the reason"
    # cleanup: end contract, reclaim, deregister CR, restore original labels
    local cid; cid=$(mock_get /v1/machines/dgx01 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['contracts'][0]['id'] if d['contracts'] else '')" 2>/dev/null)
    [[ -n "$cid" ]] && mock_post /v1/test/contracts "{\"machineId\":\"dgx01\",\"action\":\"end\",\"contractId\":\"$cid\"}" >/dev/null
    $K patch nodeownership dgx01 --type=merge \
      -p '{"spec":{"desiredOwner":"ARISE","transitionId":"t-node01-back","requireSanitization":true}}' >/dev/null
    wait_for 180 "READY" get nodeownership dgx01 -o jsonpath='{.status.phase}' >/dev/null \
      || note "dgx01 still reclaiming at teardown; controller will finish"
  else
    blocked "could not stage a VAST contract on dgx01; refusal path untested this run"
  fi
  # leave dgx01 registered (that IS its normal state); drop only the test CR
  $K delete nodeownership dgx01 --ignore-not-found >/dev/null 2>&1
  $K label node "$(node_for dgx01)" arise.ai/owner=ARISE --overwrite >/dev/null 2>&1
  $K taint node "$(node_for dgx01)" arise.ai/vast-owned- >/dev/null 2>&1 || true
  $K uncordon "$(node_for dgx01)" >/dev/null 2>&1 || true
  end
}


gw() {  # gw <method> <path> [json] — authenticated via $GW_COOKIE if set
  $K -n platform-system exec deploy/platform-gateway -- \
    env M="$1" P="$2" B="${3:-}" CK="${GW_COOKIE:-}" python3 -c "
import os,urllib.request,urllib.error
m,p,b,ck=os.environ['M'],os.environ['P'],os.environ.get('B',''),os.environ.get('CK','')
req=urllib.request.Request('http://127.0.0.1:8080'+p,method=m)
if ck: req.add_header('Cookie','arise_session='+ck)
if b:
    req.add_header('Content-Type','application/json'); req.data=b.encode()
try:
    r=urllib.request.urlopen(req,timeout=20); print(r.status, r.read().decode())
except urllib.error.HTTPError as e: print(e.code, e.read().decode()[:400])" 2>/dev/null
}

gw_login() {  # gw_login <user> <password> -> prints session token
  $K -n platform-system exec deploy/platform-gateway -- \
    env U="$1" PW="$2" python3 -c "
import os,json,urllib.request
req=urllib.request.Request('http://127.0.0.1:8080/auth/login',method='POST')
req.add_header('Content-Type','application/json')
req.data=json.dumps({'username':os.environ['U'],'password':os.environ['PW']}).encode()
r=urllib.request.urlopen(req,timeout=15)
ck=r.headers.get('Set-Cookie','')
print(ck.split('arise_session=')[1].split(';')[0] if 'arise_session=' in ck else '')" 2>/dev/null
}

test_UI_03() {
  begin UI-03 P0 "unified console: login gate, role isolation, zero credentials"
  # ---- 1. the gateway pod holds NO kubernetes credentials ------------------
  assert_eq "$($K -n platform-system get pod -l app.kubernetes.io/name=platform-gateway \
    -o jsonpath='{.items[0].spec.automountServiceAccountToken}')" "false" \
    "automountServiceAccountToken=false"
  local mounts
  mounts=$($K -n platform-system exec deploy/platform-gateway -- \
    ls /var/run/secrets/kubernetes.io/serviceaccount 2>&1 || true)
  assert_contains "$mounts" "No such file" "no SA token directory in the pod"

  # ---- 2. the SPA shell loads for everyone; every API stays closed ---------
  # The Vue/Arco SPA is served same-origin to all; its router guard calls
  # /auth/me and shows the login view. Authorization is enforced at the API,
  # never by withholding HTML — so the security assertions are all on the API.
  GW_COOKIE=""
  local anon; anon=$(gw GET /)
  assert_contains "$anon" '<div id="app">' "/ serves the SPA shell (built Vue/Arco)"
  assert_contains "$anon" 'assets/' "SPA references its hashed asset bundle"
  assert_contains "$(gw GET /papi/flavors)" "401" "portal API closed before login"
  assert_contains "$(gw GET /oapi/fleet)" "401" "ops API closed before login"
  assert_contains "$(gw POST /auth/login '{"username":"admin","password":"wrong"}')" \
    "401" "bad password refused"

  # ---- 3. admin: SPA + its bundle actually served, full API reach ----------
  GW_COOKIE=$(gw_login admin "${GW_ADMIN_PASSWORD:-arise-admin}")
  [[ -n "$GW_COOKIE" ]] && ok "admin login issued a session" || fail "admin login failed"
  local index; index=$(gw GET /)
  assert_contains "$index" 'ARISE' "SPA index.html served (title present)"
  local asset; asset=$(printf '%s' "$index" | grep -oE 'assets/[A-Za-z0-9._-]+\.js' | head -1)
  if [[ -n "$asset" ]]; then
    assert_contains "$(gw GET "/$asset" | head -c 40)" "200" "SPA JS bundle serves (dist mounted, not 404)"
  else
    fail "index.html referenced no hashed JS asset"
  fi
  # path traversal out of the web root is refused
  assert_contains "$(gw GET '/../../etc/passwd' | head -c 40)" "404" "static server rejects path traversal"
  assert_contains "$(gw GET /oapi/fleet)" '"infraNodes"' "admin reaches the ops API"
  assert_contains "$(gw GET '/prom/api/v1/query?query=sum(arise_fake_gpu_healthy)')" \
    '"result"' "embedded monitoring data path"
  assert_contains "$(gw DELETE '/prom/api/v1/query?query=up')" "403" \
    "prom proxy refuses non-GET even for admin"
  # i18n correctness is now structural (vue-i18n keyed lookup); the gate checks
  # the zh/en locale key sets are in lockstep so nothing renders raw or missing.
  if python3 "$REPO/scripts/i18n-check.py" >/dev/null 2>&1; then
    ok "i18n locale parity gate PASS (zh/en key sets match)"
  else
    fail "i18n locale parity gate FAILED — run scripts/i18n-check.py"
  fi

  # ---- 4. admin user management CRUD ---------------------------------------
  gw DELETE /auth/users/ui3-alice >/dev/null 2>&1 || true
  assert_contains "$(gw POST /auth/users '{"username":"ui3-alice","password":"alice-pass-1","role":"user","tenant":"tenant-arise"}')" \
    "201" "admin creates a user"
  assert_contains "$(gw GET /auth/users)" "ui3-alice" "user appears in the list"

  # ---- 5. tenant user: scoped to their tenant, ops plane closed ------------
  local ADMIN_COOKIE="$GW_COOKIE"
  GW_COOKIE=$(gw_login ui3-alice alice-pass-1)
  [[ -n "$GW_COOKIE" ]] && ok "created user can sign in" || fail "created user login failed"
  assert_contains "$(gw GET '/papi/overview?ns=tenant-arise')" "200" \
    "user reaches their own tenant"
  assert_contains "$(gw GET '/papi/overview?ns=tenant-direct')" "403" \
    "cross-tenant access refused AT THE GATEWAY"
  assert_contains "$(gw GET /oapi/fleet)" "403" "ops plane refused for users"
  assert_contains "$(gw GET /auth/users)" "403" "user management refused for users"
  assert_contains "$(gw GET '/prom/api/v1/query?query=arise_node_owner')" "403" \
    "prometheus refused for users (fleet operator metrics are admin-only)"

  # ---- 5b. ns-pinning regression: a tenant-direct user OMITTING ns must be
  #          served their own tenant, never default to tenant-arise. This is the
  #          exact cross-tenant isolation bypass the audit found (audit #1). -----
  local ALICE_COOKIE="$GW_COOKIE"
  GW_COOKIE=$(gw_login direct-cust "${GW_DIRECT_PASSWORD:-direct-cust}")
  local ovd; ovd=$(gw GET '/papi/overview')            # NOTE: no ns param
  assert_contains "$ovd" "direct-customer" \
    "omitted ns pins to the caller tenant (served the tenant-direct queue)"
  assert_not_contains "$ovd" "arise-internal" \
    "omitted-ns overview never leaks tenant-arise (isolation holds)"
  # path-injection: a crafted pod name must be rejected, not interpolated raw
  assert_contains "$(gw GET '/papi/logs?pod=..%2F..%2Fetc')" "400" \
    "path-injection pod name rejected at the portal"
  GW_COOKIE="$ALICE_COOKIE"

  # ---- 6. workload round trip as the tenant user ---------------------------
  $K -n tenant-arise delete pod ui3-dev --grace-period=1 --ignore-not-found >/dev/null 2>&1
  sleep 3
  assert_contains "$(gw POST '/papi/devmachines?ns=tenant-arise' '{"name":"ui3-dev","vcpu":4,"memGi":16}')" \
    "201" "devmachine created by the tenant user"
  local ok3=0
  for _ in $(seq 1 20); do sleep 3
    [[ "$($K -n tenant-arise get pod ui3-dev -o jsonpath='{.status.phase}' 2>/dev/null)" == "Running" ]] && { ok3=1; break; }
  done
  assert_eq "$ok3" "1" "devmachine Running"
  assert_contains "$(gw POST '/papi/devmachines?ns=tenant-arise' '{"name":"ui3-bad","cpu":"300m","memory":"1Gi"}')" \
    "whole vCPUs" "unit-model refusal surfaces through the gateway"
  assert_contains "$(gw POST '/papi/volumes?ns=tenant-arise' '{"name":"ui3-badvol","sizeGi":15}')" \
    "multiple of 10Gi" "platform VAP fires through the gateway"
  assert_contains "$(gw DELETE '/papi/devmachines/ui3-dev?ns=tenant-arise')" "200" \
    "deleted through the gateway"

  # ---- 7. logout invalidates; cleanup --------------------------------------
  gw POST /auth/logout >/dev/null
  assert_contains "$(gw GET '/papi/overview?ns=tenant-arise')" "401" \
    "session dead after logout"
  GW_COOKIE="$ADMIN_COOKIE"
  assert_contains "$(gw DELETE /auth/users/ui3-alice)" "200" "test user removed"
  GW_COOKIE=""
  end
}

# =============================================================== driver =====
SMOKE=(SEC_03 SCH_01 VST_06 VST_03)
ALL=(SEC_02 SEC_03 SEC_04 SEC_05 SEC_06 SCH_01 SCH_02 SCH_03 SCH_04 SCH_06 \
     SCH_07 SCH_08 SCH_09 SCH_13 SCH_11 SCH_12 SCH_05 \
     FLV_01 FLV_02 FLV_03 DIR_01 DIR_02 OBS_01 OBS_02 OBS_04 UI_01 UI_02 UI_03 NODE_01 \
     VST_06 VST_03 OWN_04 OWN_06 E2E_04)

echo "=== Phase A tests  run_id=$RUN_ID  mode=$MODE ==="
"$REPO/scripts/guard.sh" check || { echo "guard failed; refusing to run"; exit 1; }
echo

case "$MODE" in
  smoke) SET=("${SMOKE[@]}") ;;
  all)   SET=("${ALL[@]}") ;;
  *)     SET=("${MODE//-/_}") ;;
esac

for t in "${SET[@]}"; do
  if declare -F "test_$t" >/dev/null; then "test_$t"; else echo "  no such test: $t"; fi
done

echo
write_reports
"$REPO/scripts/guard.sh" check >/dev/null || echo "WARNING: guard check failed after tests"

# Exit status. FAIL is obviously non-zero, but INVALID must be too: an INVALID
# result means the test could not produce trustworthy evidence, and exiting 0
# on it would let CI record "matrix green" for a run that proved nothing.
# BLOCKED is different — §10.2 REQUIRES the environment-blocked cases to be
# reported as BLOCKED rather than PASS, so a blocked run is a correct outcome,
# not a failure. It is surfaced in the summary and in results.json; it does not
# fail the gate. Anything unexpected (a status we do not recognise) also fails.
if (( FAIL_N > 0 )); then
  echo "gate: FAIL"
  exit 1
fi
if (( INVALID_N > 0 )); then
  echo "gate: INVALID present — evidence is not trustworthy, treating as failure"
  exit 2
fi
if (( BLOCK_N > 0 )); then
  echo "gate: PASS with $BLOCK_N BLOCKED (expected per plan §10.2; see runbooks/gaps.md §2)"
fi
exit 0
