<script setup lang="ts">
import { selectScrollbar } from "@/utils/accessibility";
import DataTable from "@/components/DataTable.vue";
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import PageHeading from "@/components/PageHeading.vue";
import MetricCard from "@/components/MetricCard.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
import { useI18n } from "vue-i18n";
import { Message } from "@arco-design/web-vue";
import { oapi, papi } from "@/api";
import { ApiError } from "@/api/client";
import type { Fleet, FleetNode } from "@/api/types";

const { t } = useI18n();
const fleet = ref<Fleet | null>(null);
const loadError = ref("");
const loading = ref(false);
const submitting = ref(false);
const transitionOpen = ref(false),
  transitionError = ref(""),
  search = ref("");
const route = useRoute();
const filteredNodes = computed(() =>
  (fleet.value?.nodes ?? []).filter((n) =>
    `${n.nodeId} ${n.owner} ${n.pair}`
      .toLowerCase()
      .includes(search.value.toLowerCase().trim()),
  ),
);
const summary = computed(() => {
  const nodes = fleet.value?.nodes ?? [];
  return {
    nodes: nodes.length,
    ready: nodes.filter((n) => n.ready).length,
    gpu: nodes.reduce((n, v) => n + v.gpuUsed, 0),
    totalGpu: nodes.reduce((n, v) => n + v.gpuTotal, 0),
    contracts: nodes.reduce((n, v) => n + v.activeContracts, 0),
  };
});
function openTransition(node: FleetNode) {
  form.nodeId = node.nodeId;
  form.target = node.owner === "ARISE" ? "VAST" : "ARISE";
  form.approver = "";
  transitionError.value = "";
  transitionOpen.value = true;
}

const form = reactive({
  nodeId: "",
  target: "ARISE",
  approver: "",
  tenant: "",
});
const customerTenants = ref<string[]>([]);
const detail = reactive({
  visible: false,
  node: null as FleetNode | null,
  tab: "spec",
});

const ownerColor: Record<string, string> = {
  ARISE: "arcoblue",
  VAST: "orange",
  DIRECT: "green",
  QUARANTINED: "red",
  MAINTENANCE: "purple",
  UNKNOWN: "gray",
};

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const [nodes, catalog] = await Promise.all([oapi.fleet(), papi.flavors()]);
    fleet.value = nodes;
    customerTenants.value = catalog.tenants.filter((ns) =>
      catalog.priorities[ns]?.includes("arise-contract-bound"),
    );
    if (!customerTenants.value.includes(form.tenant))
      form.tenant = customerTenants.value[0] || "";
    if (!form.nodeId && fleet.value.nodes.length)
      form.nodeId = fleet.value.nodes[0].nodeId;
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

const selectedNode = computed(
  () => fleet.value?.nodes.find((n) => n.nodeId === form.nodeId) || null,
);
const gate = computed(() => selectedNode.value?.gates?.[form.target] || null);

async function submit() {
  if (
    submitting.value ||
    !gate.value?.allowed ||
    form.approver.trim().length < 3
  )
    return;
  transitionError.value = "";
  submitting.value = true;
  try {
    const r = await oapi.transition({
      nodeId: form.nodeId,
      desiredOwner: form.target,
      approvedBy: form.approver,
      tenant: form.target === "DIRECT" ? form.tenant : undefined,
    });
    Message.success(t("fleetOps.accepted", { id: r.transitionId }));
    transitionOpen.value = false;
    await load();
  } catch (e) {
    transitionError.value = e instanceof ApiError ? e.message : String(e);
  } finally {
    submitting.value = false;
  }
}

function openDetail(node: FleetNode) {
  detail.node = node;
  detail.tab = "spec";
  detail.visible = true;
}

const infraColumns = computed(() => [
  { title: t("fleet.node"), dataIndex: "name" },
  { title: t("fleetOps.role"), dataIndex: "role" },
  { title: "vCPU", dataIndex: "vcpuTotal" },
  { title: t("overview.memoryGi"), dataIndex: "memGiTotal" },
  { title: t("common.status"), dataIndex: "ready", slotName: "ready" },
]);
const specRows = computed(() => {
  const h = detail.node?.hw || {};
  return [
    ["GPU", h.gpus],
    ["CPU", h.cpus],
    [t("overview.memoryGi"), h.memory],
    [t("fleetOps.storage"), h.storage],
    [t("fleetOps.network"), h.network],
    [t("fleetOps.interconnect"), h.interconnect],
    [t("fleetOps.power"), h.power],
  ].filter(([, v]) => v);
});
onMounted(async () => {
  await load();
  const node = fleet.value?.nodes.find((n) => n.nodeId === route.query.node);
  if (node) openDetail(node);
});
</script>
<template>
  <div class="page">
    <PageHeading :title="t('nav.fleet')" :description="t('console.fleetDesc')">
      <template #actions>
        <a-button :loading="loading" @click="load">
          <template #icon><icon-refresh /></template>
          {{ t("common.refresh") }}
        </a-button>
      </template>
    </PageHeading>
    <a-alert v-if="loadError" type="error" class="page-error">
      {{ loadError }}
    </a-alert>
    <div v-if="fleet && !loadError" class="metrics-grid fleet-metrics">
      <MetricCard
        :label="t('console.gpuNodes')"
        :value="summary.nodes"
        icon="icon-computer"
        :detail="t('console.totalCapacity')"
      />
      <MetricCard
        :label="t('console.readyNodes')"
        :value="summary.ready"
        :total="summary.nodes"
        icon="icon-check-circle"
        accent="teal"
        :detail="t('console.operational')"
      />
      <MetricCard
        :label="t('overview.gpuAllocated')"
        :value="summary.gpu"
        :total="summary.totalGpu"
        icon="icon-thunderbolt"
        accent="violet"
        :detail="t('console.currentAllocation')"
      />
      <MetricCard
        :label="t('overview.contracts')"
        :value="summary.contracts"
        icon="icon-file"
        accent="amber"
        :detail="t('fleetOps.contracts')"
      />
    </div>
    <div class="fleet-section-heading">
      <h2>{{ t("console.gpuNodes") }}</h2>
      <a-input
        v-model="search"
        allow-clear
        :placeholder="t('console.searchResources')"
        :input-attrs="{ 'aria-label': t('console.searchResources') }"
      >
        <template #prefix><icon-search /></template>
      </a-input>
    </div>
    <a-spin :loading="loading" style="display: block">
      <div v-if="!loadError" class="fleet-grid">
        <article
          v-for="node in filteredNodes"
          :key="node.nodeId"
          class="fleet-node"
        >
          <header>
            <span class="resource-symbol"><icon-computer /></span>
            <div>
              <button
                type="button"
                class="resource-link"
                @click="openDetail(node)"
              >
                {{ node.nodeId }}
              </button>
              <small>{{ t("fleet.pair") }} {{ node.pair }}</small>
            </div>
            <StatusBadge :status="node.ready ? 'Ready' : 'NotReady'" />
          </header>
          <div class="node-tags">
            <a-tag :color="ownerColor[node.owner] || 'gray'">
              {{ node.owner }}
            </a-tag>
            <span>{{ node.phase }}</span>
            <a-tag v-if="node.cordoned" color="orange">
              {{ t("fleet.cordoned") }}
            </a-tag>
            <a-tag v-if="node.listed" color="orange">
              {{ t("fleet.listed") }}
            </a-tag>
          </div>
          <div class="node-gpu">
            <span>
              GPU
              <span class="muted">{{ t("console.allocated") }}</span>
            </span>
            <strong>
              {{ node.gpuUsed }}
              <span>/ {{ node.gpuTotal }}</span>
            </strong>
          </div>
          <div class="node-gpu-slots">
            <i
              v-for="n in node.gpuTotal"
              :key="n"
              :class="{ allocated: n <= node.gpuUsed }"
            />
          </div>
          <div class="node-resources">
            <div>
              <span>vCPU</span>
              <strong>
                {{ node.vcpuUsed }}
                <span>/ {{ node.vcpuTotal }}</span>
              </strong>
            </div>
            <div>
              <span>RAM · GiB</span>
              <strong>
                {{ node.memGiUsed }}
                <span>/ {{ node.memGiTotal }}</span>
              </strong>
            </div>
            <div>
              <span>{{ t("fleetOps.contracts") }}</span>
              <strong>{{ node.activeContracts }}</strong>
            </div>
          </div>
          <footer>
            <a-button size="small" type="text" @click="openDetail(node)">
              {{ t("console.details") }}
              <icon-arrow-rise />
            </a-button>
            <a-button size="small" @click="openTransition(node)">
              <template #icon><icon-swap /></template>
              {{ t("console.manageOwnership") }}
            </a-button>
          </footer>
        </article>
      </div>
      <EmptyState
        v-if="!loading && !filteredNodes.length && !loadError"
        :title="t('console.emptyFiltered')"
        :description="t('console.emptyFilteredDesc')"
        compact
      >
        <a-button @click="search = ''">
          {{ t("console.clearFilters") }}
        </a-button>
      </EmptyState>
    </a-spin>
    <section v-if="fleet && !loadError" class="panel infra-panel">
      <div class="panel-heading">
        <h2>{{ t("console.cpuNodes") }}</h2>
        <span class="muted">{{ fleet.infraNodes.length }}</span>
      </div>
      <DataTable
        :columns="infraColumns"
        :data="fleet.infraNodes"
        :pagination="false"
        row-key="name"
        :bordered="false"
      >
        <template #ready="{ record }">
          <a-space>
            <StatusBadge :status="record.ready ? 'Ready' : 'NotReady'" />
            <a-tag v-if="record.tainted" size="small">
              {{ t("fleetOps.tenantIsolated") }}
            </a-tag>
          </a-space>
        </template>
      </DataTable>
      <div class="adapter-note">
        VAST · {{ fleet.adapter.mode }}
        <span>{{ fleet.adapter.productionEnabled ? "LIVE" : "MOCK" }}</span>
      </div>
    </section>
    <a-drawer
      v-model:visible="transitionOpen"
      :title="t('console.manageOwnership')"
      :width="640"
      :mask-closable="!submitting"
      :closable="!submitting"
      :esc-to-close="!submitting"
      unmount-on-close
    >
      <p class="hint" style="margin: 0 0 24px">
        {{ t("console.ownershipHelp") }}
      </p>
      <div v-if="selectedNode" class="transition-node">
        <icon-computer :size="24" />
        <strong>{{ selectedNode.nodeId }}</strong>
        <a-tag :color="ownerColor[selectedNode.owner]">
          {{ selectedNode.owner }}
        </a-tag>
      </div>
      <a-form :model="form" layout="vertical">
        <a-form-item :label="t('fleetOps.targetOwner')" field="target">
          <a-radio-group
            v-model="form.target"
            type="button"
            class="owner-options"
          >
            <a-radio
              v-for="owner in ['ARISE', 'VAST', 'DIRECT', 'MAINTENANCE']"
              :key="owner"
              :value="owner"
            >
              {{ owner }}
            </a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item
          v-if="form.target === 'DIRECT'"
          :label="t('console.workspace')"
          required
          field="tenant"
        >
          <a-select
            :scrollbar="selectScrollbar"
            v-model="form.tenant"
            :options="customerTenants"
            :placeholder="t('console.workspace')"
          />
        </a-form-item>
        <a-form-item :label="t('fleetOps.approver')" required field="approver">
          <a-input
            v-model="form.approver"
            :placeholder="t('fleetOps.approver')"
            :input-attrs="{
              'aria-label': t('fleetOps.approver'),
            }"
          />
        </a-form-item>
      </a-form>
      <section class="gate-panel">
        <div class="gate-heading">
          <h3>{{ t("fleetOps.gateEval") }}</h3>
          <StatusBadge
            :status="gate?.allowed ? 'Ready' : 'Pending'"
            :label="
              t(gate?.allowed ? 'fleet.gateAllowed' : 'fleet.gateBlocked')
            "
          />
        </div>
        <div
          v-for="(reason, i) in gate?.reasons ?? []"
          :key="i"
          class="reason"
          :class="{ blocked: reason.startsWith('BLOCKED') }"
        >
          <icon-close-circle v-if="reason.startsWith('BLOCKED')" />
          <icon-check-circle v-else />
          <span>{{ reason }}</span>
        </div>
        <p class="hint">{{ t("console.auditHelp") }}</p>
      </section>
      <p class="section-note">{{ t("fleetOps.desiredOnly") }}</p>
      <a-alert v-if="transitionError" type="error">
        {{ transitionError }}
      </a-alert>
      <template #footer>
        <a-space>
          <a-button :disabled="submitting" @click="transitionOpen = false">
            {{ t("common.cancel") }}
          </a-button>
          <a-button
            type="primary"
            :loading="submitting"
            :disabled="
              !gate?.allowed ||
              form.approver.trim().length < 3 ||
              (form.target === 'DIRECT' && !form.tenant)
            "
            @click="submit"
          >
            {{ t("fleetOps.submitTransition") }}
          </a-button>
        </a-space>
      </template>
    </a-drawer>
    <a-drawer
      v-model:visible="detail.visible"
      :width="620"
      :footer="false"
      unmount-on-close
      :title="detail.node?.nodeId ?? ''"
    >
      <a-tabs lazy-load destroy-on-hide v-model:active-key="detail.tab">
        <a-tab-pane key="spec" :title="t('fleetOps.hwSpec')">
          <div class="detail-summary">
            <StatusBadge :status="detail.node?.ready ? 'Ready' : 'NotReady'" />
            <a-tag :color="ownerColor[detail.node?.owner || 'UNKNOWN']">
              {{ detail.node?.owner }}
            </a-tag>
            <span class="muted">{{ detail.node?.phase }}</span>
          </div>
          <div v-for="[k, v] in specRows" :key="k" class="spec-line">
            <span>{{ k }}</span>
            <strong>{{ v }}</strong>
          </div>
          <div class="spec-line">
            <span>{{ t("console.allocated") }}</span>
            <strong>
              GPU {{ detail.node?.gpuUsed }}/{{ detail.node?.gpuTotal }} · vCPU
              {{ detail.node?.vcpuUsed }}/{{ detail.node?.vcpuTotal }} · RAM
              {{ detail.node?.memGiUsed }}/{{ detail.node?.memGiTotal }}
              GiB
            </strong>
          </div>
          <p class="hint">{{ t("fleetOps.specSource") }}</p>
        </a-tab-pane>
        <a-tab-pane key="gates" :title="t('fleetOps.gates')">
          <div
            v-for="target in ['ARISE', 'VAST', 'DIRECT', 'MAINTENANCE']"
            :key="target"
            class="detail-gate"
          >
            <h3>→ {{ target }}</h3>
            <div
              v-for="(reason, i) in detail.node?.gates?.[target]?.reasons ?? []"
              :key="i"
              class="reason"
              :class="{ blocked: reason.startsWith('BLOCKED') }"
            >
              {{ reason }}
            </div>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-drawer>
  </div>
</template>
<style scoped>
.fleet-metrics {
  margin-bottom: 0;
}
.fleet-section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0;
  gap: 20px;
}
.fleet-section-heading h2 {
  font-size: var(--text-lg);
  font-weight: 600;
  margin: 0;
}
.fleet-section-heading :deep(.arco-input-wrapper) {
  max-width: 285px;
}
.fleet-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 22px;
}
.fleet-node {
  padding: 24px 25px 0;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
}
.fleet-node > header {
  display: flex;
  gap: 12px;
  align-items: center;
}
.fleet-node > header > div {
  flex: 1;
  min-width: 0;
}
.fleet-node .resource-link {
  font-size: var(--text-lg);
  font-weight: 600;
}
.fleet-node small {
  display: block;
  color: var(--muted);
  font-size: var(--text-xs);
  margin-top: 5px;
}
.node-tags {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 23px 0;
}
.node-tags > span {
  font-size: var(--text-xs);
  color: var(--muted);
}
.node-gpu {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 11px;
}
.node-gpu > span {
  font-size: var(--text-sm);
}
.node-gpu > span > span {
  font-size: var(--text-xs);
  margin-left: 5px;
}
.node-gpu strong {
  font-size: 1.375rem;
  font-weight: 600;
  letter-spacing: -0.4px;
}
.node-gpu strong > span {
  font-size: var(--text-base);
  color: var(--muted);
  font-weight: 400;
}
.node-gpu-slots {
  display: flex;
  gap: 7px;
  height: 24px;
}
.node-gpu-slots i {
  flex: 1;
  background: var(--accent-soft);
  border: 1px solid color-mix(in srgb, var(--accent) 10%, var(--line));
  border-radius: 4px;
}
.node-gpu-slots i.allocated {
  background: var(--accent);
  border-color: var(--accent);
}
.node-resources {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  margin: 23px 0 24px;
}
.node-resources > div {
  border-left: 1px solid var(--line);
  padding-left: 20px;
}
.node-resources > div:first-child {
  border: 0;
  padding: 0;
}
.node-resources > div > span {
  display: block;
  font-size: var(--text-xs);
  color: var(--muted);
  margin-bottom: 10px;
}
.node-resources strong {
  font-size: var(--text-base);
  font-weight: 500;
}
.node-resources strong span {
  color: var(--muted);
  font-size: var(--text-sm);
  font-weight: 400;
}
.fleet-node > footer {
  border-top: 1px solid var(--line);
  margin: 0 -25px;
  padding: 13px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--surface-soft);
  gap: 12px;
}
.infra-panel {
  margin-top: 0;
  overflow: hidden;
}
.adapter-note {
  padding: 16px 24px;
  border-top: 1px solid var(--line);
  font-size: var(--text-xs);
  color: var(--muted);
}
.adapter-note span {
  font-size: var(--text-xs);
  padding: 3px 5px;
  border: 1px solid var(--line);
  border-radius: 4px;
  margin-left: 8px;
}
.transition-node {
  display: flex;
  align-items: center;
  gap: 12px;
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 18px;
  margin-bottom: 28px;
  background: var(--surface-soft);
  color: var(--accent);
}
.transition-node strong {
  flex: 1;
  color: var(--ink);
  font-size: var(--text-lg);
}
.gate-panel {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 20px;
}
.gate-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-bottom: 18px;
}
.gate-heading h3 {
  font-size: var(--text-base);
  margin: 0;
}
.reason {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: var(--text-sm);
  line-height: 1.8;
  color: var(--muted);
  padding: 5px 0;
  overflow-wrap: anywhere;
}
.reason svg {
  flex: none;
  margin-top: 4px;
  color: var(--success);
}
.reason.blocked,
.reason.blocked svg {
  color: var(--warning);
}
.gate-panel .hint {
  font-size: var(--text-xs);
  border-top: 1px solid var(--line);
  padding-top: 14px;
  margin-bottom: 0;
}
.detail-summary {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 6px 0 20px;
  font-size: var(--text-sm);
}
.spec-line {
  display: grid;
  grid-template-columns: 95px 1fr;
  gap: 15px;
  border-bottom: 1px solid var(--line);
  padding: 16px 0;
  font-size: var(--text-sm);
  line-height: 1.8;
}
.spec-line > span {
  color: var(--muted);
}
.spec-line strong {
  font-weight: 400;
  overflow-wrap: anywhere;
}
.detail-gate {
  padding: 10px 0 20px;
  border-bottom: 1px solid var(--line);
}
.detail-gate h3 {
  font-size: var(--text-base);
}
.owner-options {
  display: flex;
  flex-wrap: wrap;
}
@media (max-width: 1050px) {
  .node-resources > div {
    padding-left: 12px;
  }
  .fleet-node {
    padding: 22px 20px 0;
  }
  .fleet-node > footer {
    margin: 0 -20px;
    padding: 13px 16px;
  }
  .fleet-node > footer :deep(.arco-btn) {
    font-size: var(--text-xs);
  }
  .node-resources strong {
    font-size: var(--text-sm);
  }
}
@media (max-width: 767px) {
  .fleet-grid {
    grid-template-columns: 1fr;
    gap: 18px;
  }
  .fleet-section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 14px;
  }
  .fleet-section-heading :deep(.arco-input-wrapper) {
    max-width: none;
  }
  .fleet-node > footer :deep(.arco-btn) {
    font-size: var(--text-sm);
  }
  .node-resources > div {
    padding-left: 20px;
  }
  .node-resources strong {
    font-size: var(--text-base);
  }
  .spec-line {
    grid-template-columns: 75px 1fr;
  }
}
</style>
