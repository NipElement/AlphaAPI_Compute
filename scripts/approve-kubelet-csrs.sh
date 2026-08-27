#!/usr/bin/env bash
# ============================================================================
# approve-kubelet-csrs.sh — approve pending kubelet SERVING-cert CSRs.
#
# kubeadm-cluster-config.yaml sets serverTLSBootstrap: true, so each kubelet
# asks the cluster CA for its serving certificate and waits. Nothing approves
# those automatically (a deliberate choice on a five-node fleet: an in-cluster
# auto-approver is one more privileged component). Until approval, the API
# server cannot verify the kubelet: `kubectl logs/exec` and the Prometheus node
# scrape fail with "tls: internal error". Run after every join and after a
# node's cert rotates (verify-dgx DGX-27 says when).
#
# Approves ONLY: signer kubernetes.io/kubelet-serving, requested by
# system:node:<name> where <name> is a node that exists in this cluster.
#   KUBE_CONTEXT=<dgx> scripts/approve-kubelet-csrs.sh
# ============================================================================
set -euo pipefail
K="kubectl${KUBE_CONTEXT:+ --context=$KUBE_CONTEXT}"
mapfile -t NODES < <($K get nodes -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n')
n=0
while read -r name signer user cond; do
  [[ "$signer" == "kubernetes.io/kubelet-serving" ]] || continue
  [[ -z "$cond" ]] || continue                      # already approved/denied
  node="${user#system:node:}"
  if printf '%s\n' "${NODES[@]}" | grep -qx "$node"; then
    $K certificate approve "$name" >/dev/null && echo "approved $name ($node)"; n=$((n+1))
  else
    echo "SKIP $name: requester $user is not a node of this cluster" >&2
  fi
done < <($K get csr -o jsonpath='{range .items[*]}{.metadata.name} {.spec.signerName} {.spec.username} {.status.conditions[*].type}{"\n"}{end}')
echo "approved $n kubelet-serving CSR(s)"
