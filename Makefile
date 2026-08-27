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
.DEFAULT_GOAL := help

include versions.env
export

RUN_ID  := $(shell cat .run_id 2>/dev/null || echo UNKNOWN)
EV      := evidence/$(RUN_ID)
KCTX    := kind-$(CLUSTER_NAME)
K       := kubectl --context $(KCTX)

.PHONY: help guard validate tools docker-plan docker-apply cluster label code \
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

# ------------------------------------------------------------------ gates --
guard:  ## assert protected assets untouched + disk above stop line
	@./scripts/guard.sh check

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
cluster: guard  ## create the 8-node kind cluster (1 cp + 7 workers)
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
	$(K) -n platform-system create configmap metering-code \
	  --from-file=services/metering/metering.py \
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
	# The built SPA (web/dist) is the served frontend. It is too large for a
	# ConfigMap, so it is staged onto the gateway's node (control-plane), where
	# gateway.yaml hostPath-mounts /arise/web read-only. dist is BUILD OUTPUT and
	# is not tracked in Git — run `make web` first (this target fails loudly if
	# it is missing).
	@test -f web/dist/index.html || { echo "web/dist missing — run 'make web' first"; exit 1; }
	# Overlay the new build ON TOP of the live dir (no pre-delete) so the running
	# gateway never sees an empty /arise/web — asset names are content-hashed and
	# coexist, index.html is overwritten last by docker cp. Then prune stale
	# hashed assets no longer in this build. Never rm the dir itself (that would
	# orphan the pod's hostPath mount -> 503).
	docker exec $(CLUSTER_NAME)-control-plane mkdir -p /arise/web/assets
	docker cp web/dist/. $(CLUSTER_NAME)-control-plane:/arise/web
	docker exec $(CLUSTER_NAME)-control-plane sh -c 'chmod -R a+rX /arise/web'
	@for f in $$(docker exec $(CLUSTER_NAME)-control-plane sh -c 'ls /arise/web/assets 2>/dev/null'); do \
	  test -f web/dist/assets/$$f || docker exec $(CLUSTER_NAME)-control-plane rm -f /arise/web/assets/$$f; \
	done

plugin: guard  ## build the fake-gpu device plugin image + load into kind
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

devbox-image: guard  ## build the SSH dev-machine image + load into kind
	docker build -t $(DEVBOX_IMAGE) services/devbox \
	  | tee $(EV)/deploy/devbox-build-$(RUN_ID).log 2>/dev/null \
	  || docker build -t $(DEVBOX_IMAGE) services/devbox
	kind load docker-image $(DEVBOX_IMAGE) --name $(CLUSTER_NAME)
	@docker inspect $(DEVBOX_IMAGE) --format 'devbox image id: {{.Id}}'

dgx-render:  ## static render gate for the dgx overlay (kubectl as renderer; no cluster)
	@./scripts/dgx-render-check.sh

# ---- DGX (hardware) targets — parameterized context, zero kind-isms.
# These run kubectl against the REMOTE dgx cluster and touch no local disk, so
# they are not guard-bracketed (guard protects THIS host's data). Volcano and
# the GPU/Network Operators are Day-0 runbook steps, not make targets — their
# install decisions need the machine (docs/production-readiness.md §3).
DGX_KCTX ?= arise-dgx
KD := kubectl --context $(DGX_KCTX)

dgx-platform:  ## apply the dgx overlay (set DGX_KCTX=<kube context>)
	$(KD) apply --server-side --force-conflicts -k platform/overlays/dgx

dgx-code:  ## (re)create the four dgx code ConfigMaps from Git sources
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
	$(KD) -n platform-system create configmap metering-code \
	  --from-file=services/metering/metering.py \
	  --dry-run=client -o yaml | $(KD) apply -f -
	@T=$$(mktemp -d) && python3 scripts/tenants-json.py > $$T/tenants.json && \
	  $(KD) -n platform-system create configmap platform-tenants \
	    --from-file=tenants.json=$$T/tenants.json \
	    --dry-run=client -o yaml | $(KD) apply -f - && rm -rf $$T
	# No SPA staging step here: on dgx the built web/dist travels inside the
	# arise/web content image (make web-image + registry push), not docker cp.
	# Seed the Alertmanager config Secret ONLY if absent: dgx-code must be
	# safely re-runnable without reverting a wired pager to the null sink.
	@$(KD) -n monitoring get secret alertmanager-config >/dev/null 2>&1 || \
	  ./scripts/alertmanager-config.sh $(DGX_KCTX)

dgx-gateway-secret:  ## generate the gateway auth Secret (random; prints once)
	# The ONLY place these credentials exist is the cluster and this one
	# terminal print. They are never written to Git, never to a file, and
	# cannot be read back afterwards (`kubectl get secret -o yaml` returns
	# them base64'd, which is why the print happens here, once, on creation).
	@$(KD) get ns platform-system >/dev/null 2>&1 || { \
	  echo "namespaces missing — run 'make dgx-platform' first"; exit 1; }
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

dgx-volcano:  ## install Volcano from the VENDORED, digest-pinned manifest (no GitHub at Day-0)
	$(KD) apply -f platform/vendor/volcano-$(VOLCANO_VERSION).yaml
	$(KD) -n volcano-system rollout status deploy/volcano-scheduler --timeout=180s
	$(KD) -n volcano-system rollout status deploy/volcano-admission --timeout=180s
	# Scheduler config + queues are control-plane objects (pair names are
	# LOGICAL ids), so the rehearsed lab files apply unchanged. Config lands
	# AFTER the installer, whose default lacks the preempt/reclaim actions.
	$(KD) apply -f platform/overlays/lab/volcano-scheduler-config.yaml
	$(KD) -n volcano-system rollout restart deploy/volcano-scheduler
	$(KD) -n volcano-system rollout status deploy/volcano-scheduler --timeout=180s
	$(KD) apply -f platform/overlays/lab/volcano-queues.yaml

dgx-alert-receiver:  ## wire the real pager (WEBHOOK_URL=https://... required)
	@test -n "$(WEBHOOK_URL)" || { \
	  echo "WEBHOOK_URL is required:  make dgx-alert-receiver WEBHOOK_URL=https://..."; \
	  echo "(without it the fleet keeps the local sink, which pages nobody)"; exit 1; }
	@WEBHOOK_URL="$(WEBHOOK_URL)" ./scripts/alertmanager-config.sh $(DGX_KCTX)

dgx-verify:  ## DGX completion gate (Day-0 step 11; needs DGX_KCTX)
	@KUBE_CONTEXT=$(DGX_KCTX) ./scripts/verify-dgx.sh

dgx-test:  ## run the FULL matrix against the dgx cluster (Day-0 step 11)
	# The same 34 cases the lab runs. They are portable because no assertion
	# spells a physical node name — node_for() resolves logical ids through the
	# labels label-nodes.sh applies (validate.sh §12 keeps it that way).
	@KUBE_CONTEXT=$(DGX_KCTX) ./tests/run.sh all

dgx-deploy: dgx-render dgx-platform dgx-code dgx-volcano  ## dgx bring-up: render -> overlay -> code -> volcano
	@echo "dgx-deploy done. Next per Day-0 runbook: GPU/Network Operators"
	@echo "(infra/dgx/operators/*-values.yaml), then: make dgx-verify && make dgx-test"

platform: guard  ## apply the lab overlay (namespaces, policy, CRD, workloads)
	$(K) apply --server-side --force-conflicts \
	  -k platform/overlays/lab 2>&1 | tee $(EV)/deploy/kustomize-apply.log

volcano: guard  ## install Volcano at the locked version + pair queues
	$(K) apply -f https://raw.githubusercontent.com/volcano-sh/volcano/$(VOLCANO_VERSION)/installer/volcano-development.yaml \
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
