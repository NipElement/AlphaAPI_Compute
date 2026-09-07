# ============================================================================
# ARISE B300 Prelab — orchestration only.
#
# Plan §7.1: "Make target 只编排；真实配置逻辑位于 Terraform/Ansible/Kustomize/
# Helm 文件中". Every target here is a thin wrapper; no configuration lives in
# this file beyond wiring.
#
# Every mutating target is bracketed by `guard`, which enforces the
# WAIVER-2026-08-11-001 controls: protected paths unchanged, free space above
# the 40 GiB stop line. There is NO EBS snapshot on this host, so the guard is
# the only thing standing between a mistake and 735 GB of production data.
# ============================================================================
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

include versions.env
export

RUN_ID  := $(shell if test -s .run_id; then cat .run_id; else date -u +RUN-lab-%Y%m%dT%H%M%SZ; fi)
EV      := evidence/$(RUN_ID)
KCTX    := kind-$(CLUSTER_NAME)
K       := kubectl --context $(KCTX)

.PHONY: help guard check validate tools docker-plan docker-apply cluster label code \
        web-image devbox-image dgx-render dgx-platform dgx-code dgx-deploy \
        dgx-gateway-secret dgx-verify dgx-test dgx-alert-receiver dgx-volcano \
        web plugin platform volcano deploy verify smoke test evidence hashes teardown \
        status

help:  ## show targets
	@echo "ARISE B300 Prelab — run_id=$(RUN_ID)"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Order:  validate -> guard -> tools -> docker-apply(you) -> cluster"
	@echo "        -> deploy -> verify -> test -> evidence"

# Evidence paths must exist BEFORE a pipeline mutates the cluster. The old
# recipe applied successfully then failed in tee because deploy/ was absent.
.PHONY: evidence-dirs new-run
new-run:  ## start a fresh local evidence campaign
	@date -u +RUN-lab-%Y%m%dT%H%M%SZ > .run_id
	@cat .run_id

evidence-dirs:
	@test ! -f $(EV)/hashes.sha256 || { echo "Evidence is sealed; run make new-run before mutating it"; exit 1; }
	@mkdir -p $(EV)/deploy $(EV)/inventory-after
	@test -s .run_id || echo $(RUN_ID) > .run_id

# ------------------------------------------------------------------ gates --
guard:  ## assert protected assets untouched + disk above stop line
	@./scripts/guard.sh check

check: validate web  ## validate backend/config gates and build the typed frontend (no deployment)

validate:  ## L0 static checks (no cluster, no docker needed)
	@./scripts/validate.sh

# -------------------------------------------------------------- toolchain --
tools:  ## install kind/kubectl/helm into ~/.local/bin (no sudo)
	@./scripts/install-tools.sh

docker-plan:  ## simulate the docker install; changes nothing
	@./scripts/install-docker.sh plan

docker-apply:  ## THE ONLY sudo STEP — run this yourself after review
	@echo "Read runbooks/docker-install-review.md first."
	@echo "Then run:  ./scripts/install-docker.sh apply"
	@false

# ---------------------------------------------------------------- cluster --
cluster: guard evidence-dirs  ## create the 8-node kind cluster (1 cp + 7 workers)
	@command -v docker >/dev/null || { echo "docker missing — see runbooks/docker-install-review.md"; exit 1; }
	kind create cluster --config kind/cluster.yaml --image $(KIND_NODE_IMAGE) \
	  2>&1 | tee $(EV)/deploy/kind-create.log
	$(K) wait --for=condition=Ready nodes --all --timeout=180s
	@$(MAKE) --no-print-directory label
	@./scripts/guard.sh check

label:  ## apply dgx01..04 / pair / owner labels from kind/node-map.yaml
	@./scripts/label-nodes.sh

# --------------------------------------------------------------- platform --
code: guard  ## (re)create the component code ConfigMaps from Git sources
	# Namespaces are NOT created here. They are defined in platform/base with
	# their PSA levels and project labels; creating them bare with
	# `kubectl create namespace` produced namespaces that did not match the
	# repository, which E2E-01 caught as drift on 2026-08-13. `platform` owns
	# them, and `deploy` runs it first.
	@$(K) get ns platform-system >/dev/null 2>&1 || { \
	  echo "namespaces missing — run 'make platform' first (it owns them)"; exit 1; }
	$(K) -n platform-system create configmap fake-gpu-advertiser-code \
	  --from-file=platform/overlays/lab/fake_gpu_advertiser.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n platform-system create configmap capacity-controller-code \
	  --from-file=services/capacity-controller/capacity_controller.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n vast-mock create configmap vast-mock-code \
	  --from-file=services/vast-mock/vast_mock.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n platform-system create configmap ops-console-code \
	  --from-file=services/ops-console/console.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n platform-system create configmap tenant-portal-code \
	  --from-file=services/tenant-portal/tenant_portal.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n platform-system create configmap platform-gateway-code \
	  --from-file=services/gateway/gateway.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n platform-system create configmap auth-backup-code \
	  --from-file=services/auth-backup/auth_backup.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n platform-system create configmap metering-code \
	  --from-file=services/metering/metering.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n platform-system create configmap ledger-backup-code \
	  --from-file=services/ledger-backup/ledger_backup.py \
	  --dry-run=client -o yaml | $(K) apply -f -
	$(K) -n monitoring create configmap grafana-dashboards \
	  --from-file=dashboards/ \
	  --dry-run=client -o yaml | $(K) apply -f -
	# The tenant register (platform/tenants.yaml) as the JSON the portal and
	# gateway read. Onboarding a tenant is an edit there + this regenerate,
	# not a code change in three services.
	@T=$$(mktemp -d) && python3 scripts/tenants-json.py > $$T/tenants.json && \
	  $(K) -n platform-system create configmap platform-tenants \
	    --from-file=tenants.json=$$T/tenants.json \
	    --dry-run=client -o yaml | $(K) apply -f - && rm -rf $$T
# Accounts and logout revocations persist on the gateway auth PVC.

# Code in a ConfigMap is not RUNNING code: the services load their file at
# process start. Without this restart `make code` left the pods executing the
# OLD code while every gate reported green — and the next unrelated restart
# would have silently activated code nobody verified (2026-08-30).
	@for d in capacity-controller ops-console tenant-portal platform-gateway metering; do \
	  $(K) -n platform-system get deploy $$d >/dev/null 2>&1 && $(K) -n platform-system rollout restart deploy/$$d >/dev/null || true; done
# A rollout that never becomes ready means the NEW code is not running —
# reporting "activated" then would be the same silent success this whole
# change exists to remove. Fail the target instead.
	@for d in capacity-controller ops-console tenant-portal platform-gateway metering; do \
	  $(K) -n platform-system get deploy $$d >/dev/null 2>&1 || continue; \
	  $(K) -n platform-system rollout status deploy/$$d --timeout=180s >/dev/null \
	    || { echo "$$d did NOT become ready — the code in its ConfigMap is not running"; exit 1; }; done
	@echo "code ConfigMaps applied AND activated (pods restarted)"
	$(MAKE) web-assets

plugin: guard evidence-dirs  ## build the fake-gpu device plugin image + load into kind
	docker build -t $(FAKE_GPU_PLUGIN_IMAGE) services/fake-gpu-plugin \
	  | tee $(EV)/deploy/fake-gpu-plugin-build-$(RUN_ID).log 2>/dev/null \
	  || docker build -t $(FAKE_GPU_PLUGIN_IMAGE) services/fake-gpu-plugin
	kind load docker-image $(FAKE_GPU_PLUGIN_IMAGE) --name $(CLUSTER_NAME)
	-$(K) -n platform-system rollout restart ds/fake-gpu-plugin 2>/dev/null
	@docker inspect $(FAKE_GPU_PLUGIN_IMAGE) --format 'fake-gpu-plugin image id: {{.Id}}'

web:  ## build the Vue/Arco console into web/dist (the served frontend artifact)
	cd web && npm ci && npm run build

web-image: guard  ## build the SPA content image (the dgx delivery path — no docker cp)
	@test -f web/dist/index.html || { echo "web/dist missing — run 'make web' first"; exit 1; }
	rm -rf services/web/dist && cp -r web/dist services/web/dist
	docker build -t $(ARISE_WEB_IMAGE) services/web
	@docker inspect $(ARISE_WEB_IMAGE) --format 'arise/web image id: {{.Id}}'

devbox-image: guard evidence-dirs  ## build the SSH dev-machine image + load into kind
	docker build -t $(DEVBOX_IMAGE) services/devbox \
	  | tee $(EV)/deploy/devbox-build-$(RUN_ID).log 2>/dev/null \
	  || docker build -t $(DEVBOX_IMAGE) services/devbox
# `kind load` is LAB delivery. On the Day-0 admin box there is no kind cluster,
# and this line hard-failed step 0 with "no nodes found for cluster
# b300-prelab" AFTER building the image, leaving the operator unsure whether
# the artifact was usable (2026-09-01). versions.env:101 already says the
# rule — "kind-loaded for the lab, registry-pushed at Day-0" — and web-image
# already follows it; this target did not.
	@if kind get clusters 2>/dev/null | grep -qx "$(CLUSTER_NAME)"; then \
	  kind load docker-image $(DEVBOX_IMAGE) --name $(CLUSTER_NAME); \
	else \
	  echo "no kind cluster '$(CLUSTER_NAME)' — image built only."; \
	  echo "  lab:  create the cluster, then re-run 'make devbox-image'"; \
	  echo "  dgx:  push it with 'REGISTRY=<host:port> ./scripts/registry-mirror.sh'"; \
	fi
	@docker inspect $(DEVBOX_IMAGE) --format 'devbox image id: {{.Id}}'

dgx-render:  ## static render gate for the dgx overlay (kubectl as renderer; no cluster)
	@./scripts/dgx-render-check.sh

gate-selftest:  ## prove the render gate REJECTS bad manifests (mutation test of the gate itself)
	@./scripts/gate-selftest.sh

# ---- DGX (hardware) targets — parameterized context, zero kind-isms.
# These run kubectl against the REMOTE dgx cluster and touch no local disk, so
# they are not guard-bracketed (guard protects THIS host's data). Volcano is
# vendored and installed by dgx-deploy; the GPU/Network Operators stay Day-0
# runbook steps (helm + infra/dgx/operators values) because driver ownership
# needs the delivered machine (runbooks/day0-setup.md).
DGX_KCTX ?= arise-dgx
KD := kubectl --context $(DGX_KCTX)

dgx-platform:  ## apply dgx config while preserving public security before any rollout
	python3 scripts/render-dgx-platform.py --context $(DGX_KCTX) | $(KD) apply --server-side --force-conflicts -f -
	@KUBE_CONTEXT=$(DGX_KCTX) ./scripts/prometheus-reload.sh

dgx-code:  ## (re)create dgx code ConfigMaps and activate the running services
	@$(KD) get ns platform-system >/dev/null 2>&1 || { \
	  echo "namespaces missing — run 'make dgx-platform' first (it owns them)"; exit 1; }
	$(KD) -n platform-system create configmap capacity-controller-code \
	  --from-file=services/capacity-controller/capacity_controller.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	$(KD) -n platform-system create configmap ops-console-code \
	  --from-file=services/ops-console/console.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	$(KD) -n platform-system create configmap tenant-portal-code \
	  --from-file=services/tenant-portal/tenant_portal.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	$(KD) -n platform-system create configmap platform-gateway-code \
	  --from-file=services/gateway/gateway.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	$(KD) -n platform-system create configmap auth-backup-code \
	  --from-file=services/auth-backup/auth_backup.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	$(KD) -n platform-system create configmap metering-code \
	  --from-file=services/metering/metering.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	$(KD) -n platform-system create configmap ledger-backup-code \
	  --from-file=services/ledger-backup/ledger_backup.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	@T=$$(mktemp -d) && python3 scripts/tenants-json.py > $$T/tenants.json && \
	  $(KD) -n platform-system create configmap platform-tenants \
	    --from-file=tenants.json=$$T/tenants.json \
	    --dry-run=client -o yaml | $(KD) apply -f - && rm -rf $$T
# Accounts and logout revocations persist on the gateway auth PVC.

# Same activation rule as the lab: a ConfigMap edit is not a running change.
	@for d in capacity-controller ops-console tenant-portal platform-gateway metering; do \
	  $(KD) -n platform-system get deploy $$d >/dev/null 2>&1 && $(KD) -n platform-system rollout restart deploy/$$d >/dev/null || true; done
# A rollout that never becomes ready means the NEW code is not running —
# reporting "activated" then would be the same silent success this whole
# change exists to remove. Fail the target instead.
	@for d in capacity-controller ops-console tenant-portal platform-gateway metering; do \
	  $(KD) -n platform-system get deploy $$d >/dev/null 2>&1 || continue; \
	  $(KD) -n platform-system rollout status deploy/$$d --timeout=300s >/dev/null \
	    || { echo "$$d did NOT become ready — the code in its ConfigMap is not running"; exit 1; }; done
	@echo "dgx code ConfigMaps applied AND activated (pods restarted)"
# No SPA staging step here: on dgx the built web/dist travels inside the
# arise/web content image (make web-image + registry push), not docker cp.
# Seed the Alertmanager config Secret ONLY if absent: dgx-code must be
# safely re-runnable without reverting a wired pager to the null sink.
	@$(KD) -n monitoring get secret alertmanager-config >/dev/null 2>&1 || \
	  ./scripts/alertmanager-config.sh $(DGX_KCTX)

DGX_HOSTS ?= dgx01 dgx02 dgx03 dgx04
dgx-onboard:  ## label + register the four B300s (Day-0 step 7; DGX_HOSTS="<k8s node names>" in dgx01..04 order)
# onboard-node.sh <node> gpu <dgxNN> <pair> against the dgx context: node-id,
# role, pair labels and the NodeOwnership object every later step resolves
# through (node_for, DGX-02..05, the drain gates). The kind labeller
# (label-nodes.sh) must never run here — it names kind nodes.
	@set -e; i=1; for h in $(DGX_HOSTS); do \
	  id=$$(printf 'dgx%02d' $$i); pair=$$( [ $$i -le 2 ] && echo 01-02 || echo 03-04 ); \
	  echo "== $$h -> $$id (pair $$pair)"; \
	  KUBE_CONTEXT=$(DGX_KCTX) ./scripts/onboard-node.sh "$$h" gpu "$$id" "$$pair"; i=$$((i+1)); done
	@$(KD) get nodes -l arise.ai/node-id -o custom-columns='NODE:.metadata.name,ID:.metadata.labels.arise\.ai/node-id,PAIR:.metadata.labels.arise\.ai/pair,OWNER:.metadata.labels.arise\.ai/owner'

dgx-gateway-secret:  ## generate the gateway auth Secret (random; prints once)
# The ONLY place these credentials exist is the cluster and this one
# terminal print. They are never written to Git, never to a file, and
# can be read by authorized Secret readers: base64 is not encryption.
# Do not record terminal output or Secret exports in Git or test evidence.
# The namespace is created bare if absent so this can run BEFORE dgx-deploy
# (the documented order); the overlay's later apply adds its labels/PSA.
	@$(KD) get ns platform-system >/dev/null 2>&1 || $(KD) create ns platform-system >/dev/null
	@if $(KD) -n platform-system get secret platform-gateway-auth >/dev/null 2>&1; then \
	  echo "platform-gateway-auth already exists."; \
	  echo "Rotating it is deliberate: delete it, re-run this target, then"; \
	  echo "rollout restart the gateway. Every live session is invalidated"; \
	  echo "by the rotation (that is the point of a rotation)."; exit 1; \
	fi
	@ADMIN=$$(head -c 24 /dev/urandom | base64 | tr -d '=+/' | head -c 24); \
	 ARISE=$$(head -c 24 /dev/urandom | base64 | tr -d '=+/' | head -c 24); \
	 DIRECT=$$(head -c 24 /dev/urandom | base64 | tr -d '=+/' | head -c 24); \
	 SKEY=$$(head -c 48 /dev/urandom | base64 | tr -d '=+/' | head -c 48); \
	 $(KD) -n platform-system create secret generic platform-gateway-auth \
	   --from-literal=admin-password="$$ADMIN" \
	   --from-literal=arise-password="$$ARISE" \
	   --from-literal=direct-password="$$DIRECT" \
	   --from-literal=session-key="$$SKEY" >/dev/null && \
	 printf '\n  platform-gateway-auth created. RECORD THESE NOW:\n\n' && \
	 printf '    admin        %s\n    arise-dev    %s\n    direct-cust  %s\n\n' \
	   "$$ADMIN" "$$ARISE" "$$DIRECT" && \
	 printf '  (session-key is machine-only; it is never needed by a human)\n\n'

dgx-etcd-encryption-key:  ## write /etc/kubernetes/enc/keys.yaml (run ON the head node, BEFORE kubeadm init)
# Must exist before `kubeadm init`: the API server refuses to start when
# encryption-provider-config points at a missing file. Root, because it writes
# under /etc/kubernetes and must be 0600.
	@test "$$(id -u)" = 0 || { echo "run as root ON THE HEAD NODE"; exit 1; }
	@test ! -f /etc/kubernetes/enc/keys.yaml || { \
	  echo "/etc/kubernetes/enc/keys.yaml already exists — refusing to overwrite."; \
	  echo "Overwriting it makes every existing Secret unreadable. Rotate per the"; \
	  echo "procedure in infra/dgx/encryption-config.yaml.template instead."; exit 1; }
	@install -d -m 0700 /etc/kubernetes/enc
	@K=$$(head -c 32 /dev/urandom | base64) && \
	  sed "s|REPLACE_WITH_BASE64_32_BYTE_KEY|$$K|" \
	    $$(pwd)/infra/dgx/encryption-config.yaml.template > /etc/kubernetes/enc/keys.yaml && \
	  chmod 0600 /etc/kubernetes/enc/keys.yaml && \
	  echo "" && \
	  echo "  etcd encryption key written to /etc/kubernetes/enc/keys.yaml (0600)." && \
	  echo "  RECORD THIS IN THE PASSWORD VAULT — it is printed once and never again:" && \
	  echo "    $$K" && \
	  echo "" && \
	  echo "  Losing it makes every Secret in an etcd BACKUP unrecoverable."

dgx-ledger-key:  ## create the ledger chain key Secret (random; printed ONCE)
# Why: with a plain sha256 chain, anyone who can write the ledger file can
# rewrite history end-to-end and the chain still verifies (measured
# 2026-08-30: a spliced record moved a test invoice from $114.84 to
# $51,563.16). An HMAC key mounted only into the metering pod means a file-level
# edit no longer verifies. It does NOT defend against the meter itself —
# that is what the external anchor (invoice.py --expect-head) is for.
#
# RECORD THE KEY IN THE PASSWORD VAULT. Without it nobody, us included, can
# verify the ledger the invoices come from. Create it BEFORE the first tenant
# workload: keying a ledger that already has unkeyed records makes that
# history unverifiable in the new mode (the meter says so loudly at startup).
	@$(KD) get ns platform-system >/dev/null 2>&1 || { \
	  echo "namespaces missing — run 'make dgx-platform' first"; exit 1; }
	@if $(KD) -n platform-system get secret metering-chain-key >/dev/null 2>&1; then \
	  echo "metering-chain-key already exists. Rotating it makes every EXISTING"; \
	  echo "record unverifiable — close the books, archive the ledger with its"; \
	  echo "key, then start a new one. Not something to do casually."; exit 1; fi
	@RECS=$$($(KD) -n platform-system exec deploy/metering -- python3 -c \
	   "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8080/ledger'))['total'])" 2>/dev/null || echo 0); \
	 if [ "$${RECS:-0}" != "0" ]; then \
	   echo "the ledger already has $$RECS unkeyed records: keying it now makes"; \
	   echo "that history unverifiable. Archive it first (copy the file into"; \
	   echo "evidence/ and record its head), then start a fresh ledger."; exit 1; fi
	@KEY=$$(head -c 32 /dev/urandom | base64); \
	 $(KD) -n platform-system create secret generic metering-chain-key \
	   --from-literal=key="$$KEY" >/dev/null && \
	 printf '\n  metering-chain-key created. RECORD THIS IN THE VAULT NOW:\n\n    %s\n\n' "$$KEY" && \
	 printf '  Without it no one can verify the ledger the invoices come from.\n' && \
	 printf '  Next: kubectl -n platform-system rollout restart deploy/metering\n\n'

dgx-volcano:  ## install Volcano from the VENDORED, digest-pinned manifest (no GitHub at Day-0)
	$(KD) apply -f platform/vendor/volcano-$(VOLCANO_VERSION).yaml
# Control-plane placement (review 2026-08-27): the upstream installer has no
# nodeSelector, so scheduler/admission/controllers would land on SELLABLE GPU
# nodes and be evicted by every ownership drain. Same rule as every platform
# component — the head node, tolerating its taint.
	@for d in volcano-scheduler volcano-admission volcano-controllers; do \
	  $(KD) -n volcano-system patch deploy $$d --type merge -p \
	    '{"spec":{"template":{"spec":{"nodeSelector":{"node-role.kubernetes.io/control-plane":""},"tolerations":[{"key":"node-role.kubernetes.io/control-plane","effect":"NoSchedule"}]}}}}' >/dev/null; \
	done
	$(KD) -n volcano-system rollout status deploy/volcano-scheduler --timeout=180s
	$(KD) -n volcano-system rollout status deploy/volcano-admission --timeout=180s
	$(KD) -n volcano-system rollout status deploy/volcano-controllers --timeout=180s
	# Scheduler config + queues are control-plane objects (pair names are
	# LOGICAL ids), so the rehearsed lab files apply unchanged. Config lands
	# AFTER the installer, whose default lacks the preempt/reclaim actions.
	$(KD) apply -f platform/overlays/dgx/volcano-scheduler-config.yaml
	$(KD) -n volcano-system rollout restart deploy/volcano-scheduler
	$(KD) -n volcano-system rollout status deploy/volcano-scheduler --timeout=180s
# Scheduler config + queues are the dgx PORTS: same actions/tiers/names/weights,
# binpack and capability on nvidia.com/gpu (the lab files weight the simulated
# resource — applied on hardware, packing was a silent no-op; review 2026-08-27).
# (The lab file bounds arise.dev/fake-gpu — applied on hardware it bounded
# nothing real; review 2026-08-27.)
	$(KD) apply -f platform/overlays/dgx/volcano-queues.yaml

dgx-alert-receiver:  ## wire the real pager (WEBHOOK_URL=https://... required)
	@test -n "$(WEBHOOK_URL)" || { \
	  echo "WEBHOOK_URL is required:  make dgx-alert-receiver WEBHOOK_URL=https://..."; \
	  echo "(without it the fleet keeps the local sink, which pages nobody)"; exit 1; }
	@WEBHOOK_URL="$(WEBHOOK_URL)" ./scripts/alertmanager-config.sh $(DGX_KCTX)

dgx-hw-accept:  ## Day-0 step 13: NVLink + XDR acceptance jobs, graded against hw-thresholds.env
	@KUBE_CONTEXT=$(DGX_KCTX) ./scripts/hw-accept.sh all

dgx-restore-drill:  ## prove the newest etcd snapshot RESTORES (no cutover, nothing stopped)
	@KUBE_CONTEXT=$(DGX_KCTX) ./scripts/etcd-restore-drill.sh

dgx-launch-verify:  ## the "we are about to take money" gate (launch blockers become FAIL)
	@LAUNCH=1 KUBE_CONTEXT=$(DGX_KCTX) ./scripts/verify-dgx.sh

dgx-verify:  ## DGX completion gate (Day-0 step 11; needs DGX_KCTX)
	@KUBE_CONTEXT=$(DGX_KCTX) ./scripts/verify-dgx.sh

dgx-test:  ## run the PORTABLE matrix against the dgx cluster (Day-0 step 11)
# OVERLAY=dgx selects native GPU resources and skips lab-only cases.
# tests/run.sh owns the current case list and records each SKIPPED result;
# a successful exit does not turn a skipped hardware claim into a PASS.
	@OVERLAY=dgx KUBE_CONTEXT=$(DGX_KCTX) ./tests/run.sh all

dgx-cni:  ## apply the VENDORED Calico manifest (right after kubeadm init)
	$(KD) apply -f platform/vendor/calico-$(CALICO_VERSION).yaml
	$(KD) -n kube-system rollout status ds/calico-node --timeout=300s

dgx-approve-csrs:  ## approve pending kubelet serving-cert CSRs (after every join)
	@KUBE_CONTEXT=$(DGX_KCTX) ./scripts/approve-kubelet-csrs.sh

dgx-edge:  ## validate settings, secure the gateway, then start the TLS edge
	python3 scripts/edge-config-check.py
	$(KD) -n platform-system set env deploy/platform-gateway GW_TRUST_PROXY=true GW_COOKIE_SECURE=true
	$(KD) -n platform-system rollout status deploy/platform-gateway --timeout=120s
	$(KD) apply -k platform/overlays/dgx/edge
	$(KD) -n edge-system rollout status deploy/platform-edge --timeout=180s
	@echo "Edge started. Verify public DNS, a trusted TLS certificate, login and logout per runbooks/public-edge.md."

dgx-edge-off:  ## stop public listeners before resetting flags; retain ACME keys and certificates
	$(KD) -n edge-system scale deploy/platform-edge --replicas=0
	$(KD) -n edge-system wait --for=delete pod -l app.kubernetes.io/name=platform-edge --timeout=120s
	$(KD) -n platform-system set env deploy/platform-gateway GW_TRUST_PROXY=false GW_COOKIE_SECURE=false
	$(KD) -n platform-system rollout status deploy/platform-gateway --timeout=120s

dgx-deploy: dgx-render dgx-sentinel-check dgx-platform dgx-code dgx-volcano  ## dgx bring-up: render -> overlay -> code -> volcano
	@echo "dgx-deploy done. Next per Day-0 runbook: GPU/Network Operators"

dgx-sentinel-check:  ## refuse to deploy while images are still day0-registry.invalid sentinels
# The render gate ACCEPTS the sentinel (it is what lets the overlay render
# before the registry exists). Deploying it is a different matter: every
# platform pod would ImagePullBackOff. Retag from the mirror record first
# (scripts/registry-mirror.sh prints the digests) — or ALLOW_SENTINEL=1 for
# a deliberate dry-apply.
	@if [ -z "$(ALLOW_SENTINEL)" ] && grep -rq 'day0-registry.invalid' platform/overlays/dgx/kustomization.yaml platform/overlays/dgx/tenant-portal.yaml; then \
	  echo "platform/overlays/dgx still references day0-registry.invalid sentinel images."; \
	  echo "Retag to the mirror digests (evidence/RUN-dgx/registry-mirror-*.txt) or set ALLOW_SENTINEL=1."; exit 1; fi
	@echo "(infra/dgx/operators/*-values.yaml), then: make dgx-verify && make dgx-test"

# A mounted config is not a LOADED config, and a reload right after `apply`
# re-reads the OLD file (ConfigMap volumes sync asynchronously). Both traps,
# plus the verification that the reload actually took, live in one script.
platform: guard evidence-dirs  ## apply the lab overlay (namespaces, policy, CRD, workloads)
	$(K) apply --server-side --force-conflicts \
	  -k platform/overlays/lab 2>&1 | tee $(EV)/deploy/kustomize-apply.log
	@KUBE_CONTEXT=$(KCTX) ./scripts/prometheus-reload.sh

volcano: guard evidence-dirs  ## install Volcano at the locked version + pair queues
	$(K) apply -f platform/vendor/volcano-$(VOLCANO_VERSION).yaml \
	  2>&1 | tee $(EV)/deploy/volcano-apply.log
	$(K) -n volcano-system rollout status deploy/volcano-scheduler --timeout=180s
	$(K) -n volcano-system rollout status deploy/volcano-admission --timeout=180s
	# Queues live here rather than in the lab overlay because they depend on
	# CRDs that this target installs; putting them in the overlay would make a
	# first-run `make platform` fail on a missing resource type.
	# Scheduler config must land AFTER the upstream installer, which ships a
	# config without the preempt/reclaim actions this platform depends on.
	$(K) apply -f platform/overlays/lab/volcano-scheduler-config.yaml \
	  2>&1 | tee -a $(EV)/deploy/volcano-apply.log
	$(K) -n volcano-system rollout restart deploy/volcano-scheduler
	$(K) -n volcano-system rollout status deploy/volcano-scheduler --timeout=180s
	$(K) apply -f platform/overlays/lab/volcano-queues.yaml \
	  2>&1 | tee -a $(EV)/deploy/volcano-apply.log

# platform FIRST: it owns the namespaces that code writes into. Deployments
# briefly wait on their ConfigMaps, which is self-healing.
deploy: platform code volcano  ## full platform bring-up
	@$(MAKE) --no-print-directory verify

# ----------------------------------------------------------------- verify --
verify:  ## Stage 6 completion gate (plan §7.8)
	@./scripts/verify.sh

status:  ## quick human view
	@$(K) get nodes -L arise.ai/node-id,arise.ai/pair,arise.ai/owner
	@$(K) get nodeownership 2>/dev/null || echo "(no NodeOwnership CRs yet)"
	@$(K) get pods -A --field-selector=status.phase!=Running | head -20

# ------------------------------------------------------------------ tests --
smoke:  ## P0 smoke subset
	@./tests/run.sh smoke

test:  ## full Phase A matrix
	@./tests/run.sh all

# --------------------------------------------------------------- evidence --
evidence:  ## refresh preflight + inventory-after into the evidence pack
	@./scripts/preflight.sh
	-$(K) get nodeownership -o yaml > $(EV)/inventory-after/nodeownership.yaml 2>/dev/null
	-$(K) get nodes -o yaml         > $(EV)/inventory-after/nodes.yaml 2>/dev/null
	-$(K) get pods -A -o yaml       > $(EV)/inventory-after/pods.yaml 2>/dev/null
	@$(MAKE) --no-print-directory hashes

evidence-seal:  ## freeze the current evidence campaign (manifest + sha256 + redaction sweep)
	@./scripts/hash-evidence.sh

evidence-verify:  ## re-check the sealed pack; FAILS if anything was overwritten
	@./scripts/hash-evidence.sh verify

hashes:  ## SHA-256 every evidence artifact and freeze the manifest
	@./scripts/hash-evidence.sh

# --------------------------------------------------------------- teardown --
teardown: guard  ## delete ONLY this lab; never a global prune
	@echo "Scoped teardown — kind cluster + labelled objects only."
	-kind delete cluster --name $(CLUSTER_NAME)
	@echo "Residual objects (inspect, do NOT prune globally):"
	-docker ps -a  --filter label=io.x-k8s.kind.cluster=$(CLUSTER_NAME)
	-docker volume ls --filter label=arise.project=b300-prelab
	-docker network ls --filter label=arise.project=b300-prelab
	@./scripts/guard.sh check

# Optional tenant SSH and private-service access; keys are operator-managed.
ACCESS_KEYS ?= platform/access/keys.yaml
.PHONY: access-render access dgx-access access-off dgx-access-off
access-render:  ## render tenant access for review (no cluster mutation)
	python3 scripts/render-access.py --overlay dgx --keys "$(ACCESS_KEYS)"

access: guard  ## enable/update the lab tenant SSH bastion with registered keys
	bash scripts/deploy-access.sh lab $(KCTX) "$(ACCESS_KEYS)"

dgx-access:  ## enable/update production SSH :2222 after key/image/hostname validation
	bash scripts/deploy-access.sh dgx $(DGX_KCTX) "$(ACCESS_KEYS)"

access-off: guard  ## stop lab tenant access, retaining host identity and config
	$(K) -n access-system scale deployment/tenant-bastion --replicas=0
	$(K) -n access-system wait --for=delete pod -l app.kubernetes.io/name=tenant-bastion --timeout=60s

dgx-access-off:  ## stop all customer SSH/service tunnels; retain host keys
	$(KD) -n access-system scale deployment/tenant-bastion --replicas=0
	$(KD) -n access-system wait --for=delete pod -l app.kubernetes.io/name=tenant-bastion --timeout=60s

.PHONY: test-browser test-browser-quality test-auth-recovery
test-browser: guard  ## real Chromium workflows against the deployed kind platform
	cd web && npm run test:e2e

test-browser-quality: guard  ## theme, responsive layout and axe checks against the deployed kind platform
	cd web && npm run test:quality

test-auth-recovery:  ## isolated HTTP concurrency, SIGKILL and backup/restore acceptance
	python3 tests/live_auth_recovery.py

.PHONY: web-assets
web-assets: guard  ## update lab frontend assets without restarting backend services
	@test -f web/dist/index.html || { echo "web/dist missing — run make web first"; exit 1; }
	# Publish hashed assets before atomically replacing the entry document.
	docker exec $(CLUSTER_NAME)-control-plane mkdir -p /arise/web/assets
	docker cp web/dist/assets/. $(CLUSTER_NAME)-control-plane:/arise/web/assets
	docker exec $(CLUSTER_NAME)-control-plane sh -c 'chmod -R a+rX /arise/web/assets'
	docker cp web/dist/index.html $(CLUSTER_NAME)-control-plane:/arise/web/index.html.next
	docker exec $(CLUSTER_NAME)-control-plane sh -c 'chmod a+r /arise/web/index.html.next && mv /arise/web/index.html.next /arise/web/index.html'
	@for f in $$(docker exec $(CLUSTER_NAME)-control-plane sh -c 'ls /arise/web/assets 2>/dev/null'); do \
	  test -f web/dist/assets/$$f || docker exec $(CLUSTER_NAME)-control-plane rm -f /arise/web/assets/$$f; \
	done
