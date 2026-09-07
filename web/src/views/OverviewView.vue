<script setup lang="ts">
import QuotaMeter from "@/components/QuotaMeter.vue";
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import { useUiStore } from "@/stores/ui";
import { papi, oapi } from "@/api";
import { quantity } from "@/utils/quantity";
import type { Overview, Fleet } from "@/api/types";
import PageHeading from "@/components/PageHeading.vue";
import MetricCard from "@/components/MetricCard.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
const { t } = useI18n(),
  auth = useAuthStore(),
  ui = useUiStore();
const ov = ref<Overview | null>(null),
  fleet = ref<Fleet | null>(null),
  loading = ref(true),
  error = ref(""),
  updated = ref("");
let generation = 0;
async function load() {
  const ticket = ++generation,
    ns = ui.tenant;
  loading.value = true;
  error.value = "";
  try {
    const [o, f] = await Promise.all([
      papi.overview(ns),
      auth.isAdmin() ? oapi.fleet() : Promise.resolve(null),
    ]);
    if (ticket !== generation) return;
    ov.value = o;
    fleet.value = f;
    updated.value = new Date().toLocaleTimeString(
      ui.locale === "zh" ? "zh-CN" : "en-US",
      { hour: "2-digit", minute: "2-digit", hour12: false },
    );
  } catch (e) {
    if (ticket === generation)
      error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (ticket === generation) loading.value = false;
  }
}
onMounted(load);
onUnmounted(() => {
  generation++;
});
function quota(kind: "gpu" | "cpu" | "memory") {
  const all = ov.value?.quota ?? {},
    keys = {
      gpu: ["requests.nvidia.com/gpu", "requests.arise.dev/fake-gpu"],
      cpu: ["requests.arise.dev/sim-vcpu", "requests.cpu"],
      memory: ["requests.arise.dev/sim-mem-gi", "requests.memory"],
    };
  const key = keys[kind].find((k) => k in all);
  if (!key) return { used: 0, hard: null as number | null };
  const divisor = key === "requests.memory" ? 2 ** 30 : 1;
  return {
    used: quantity(all[key].used) / divisor,
    hard: quantity(all[key].hard) / divisor,
  };
}
const totals = computed(() => {
  const nodes = fleet.value?.nodes ?? [],
    sum = (
      key:
        | "gpuUsed"
        | "gpuTotal"
        | "vcpuUsed"
        | "vcpuTotal"
        | "memGiUsed"
        | "memGiTotal",
    ) => nodes.reduce((n, node) => n + node[key], 0);
  return {
    gpu: sum("gpuUsed"),
    gpuTotal: sum("gpuTotal"),
    cpu: sum("vcpuUsed"),
    cpuTotal: sum("vcpuTotal"),
    mem: sum("memGiUsed"),
    memTotal: sum("memGiTotal"),
    ready: nodes.filter((n) => n.ready).length,
  };
});
const gpu = computed(() =>
  auth.isAdmin()
    ? { used: totals.value.gpu, hard: totals.value.gpuTotal }
    : quota("gpu"),
);
const percentage = computed(() =>
  gpu.value.hard
    ? Math.min(100, Math.max(0, (gpu.value.used / gpu.value.hard) * 100))
    : 0,
);
const allWorkloads = computed(() => [
  ...(ov.value?.devmachines ?? []).map((r) => ({
    ...r,
    route: "dev",
    kind: t("nav.devMachines"),
    icon: "icon-desktop",
  })),
  ...(ov.value?.jobs ?? []).map((r) => ({
    ...r,
    route: "jobs",
    kind: t("nav.jobs"),
    icon: "icon-thunderbolt",
  })),
  ...(ov.value?.services ?? []).map((r) => ({
    ...r,
    phase:
      Number(r.replicas) > 0 && Number(r.ready) >= Number(r.replicas)
        ? "Ready"
        : "Pending",
    route: "services",
    kind: t("nav.services"),
    icon: "icon-cloud",
  })),
]);
const launchers = [
  { route: "dev", key: "launchDev", icon: "icon-desktop", color: "blue" },
  {
    route: "jobs",
    key: "launchJob",
    icon: "icon-thunderbolt",
    color: "violet",
  },
  {
    route: "services",
    key: "launchService",
    icon: "icon-cloud",
    color: "teal",
  },
];
</script>
<template>
  <div class="page overview-page">
    <PageHeading
      :eyebrow="t('console.overviewEyebrow')"
      :title="
        t(
          auth.isAdmin()
            ? 'console.adminOverviewTitle'
            : 'console.overviewTitle',
        )
      "
      :description="
        t(auth.isAdmin() ? 'console.adminOverviewDesc' : 'console.overviewDesc')
      "
    >
      <template #actions>
        <span v-if="updated" class="updated">
          {{ t("console.lastUpdated") }} {{ updated }}
        </span>
        <a-button
          :loading="loading"
          :aria-label="t('common.refresh')"
          @click="load"
        >
          <icon-refresh />
        </a-button>
      </template>
    </PageHeading>
    <a-alert v-if="error" type="error" class="page-error">
      {{ error }}
      <a-button size="mini" @click="load">{{ t("common.refresh") }}</a-button>
    </a-alert>
    <a-skeleton v-if="loading && !ov" :animation="true">
      <a-skeleton-line :rows="8" :line-height="35" :line-spacing="20" />
    </a-skeleton>
    <template v-else-if="ov && !error">
      <div class="overview-top">
        <section class="capacity-hero">
          <div class="capacity-copy">
            <div class="capacity-eyebrow">
              <span class="tiny-chip"><icon-thunderbolt :size="12" /></span>
              {{ auth.isAdmin() ? t("console.platform") : ui.tenant }}
              <span class="capacity-separator">/</span>
              GPU
            </div>
            <p>
              {{
                t(auth.isAdmin() ? "console.availableGpu" : "console.remaining")
              }}
            </p>
            <div class="capacity-number">
              {{ gpu.hard === null ? "—" : Math.max(0, gpu.hard - gpu.used) }}
              <span>GPU</span>
            </div>
            <div class="capacity-meta">
              <span>
                <i />
                {{ t("console.allocated") }}
                <b>{{ gpu.used }}</b>
              </span>
              <span>
                {{ t("console.totalCapacity") }}
                <b>{{ gpu.hard ?? "—" }}</b>
              </span>
            </div>
            <div class="capacity-note">{{ t("console.capacityNote") }}</div>
          </div>
          <div class="capacity-viz">
            <svg
              viewBox="0 0 180 180"
              role="img"
              :aria-label="`${t('console.currentAllocation')} ${percentage.toFixed(1)}%`"
            >
              <defs>
                <linearGradient
                  id="allocation-stroke"
                  x1="0"
                  y1="0"
                  x2="1"
                  y2="1"
                >
                  <stop stop-color="#99b9ff" />
                  <stop offset="1" stop-color="#5379ed" />
                </linearGradient>
              </defs>
              <circle
                cx="90"
                cy="90"
                r="71"
                fill="none"
                stroke="var(--line)"
                stroke-width="15"
              />
              <circle
                cx="90"
                cy="90"
                r="71"
                fill="none"
                stroke="var(--accent)"
                stroke-width="15"
                stroke-linecap="round"
                :stroke-dasharray="`${(percentage / 100) * 446.1} 446.1`"
                transform="rotate(-90 90 90)"
              />
              <circle
                cx="90"
                cy="90"
                r="51"
                fill="none"
                stroke="var(--line)"
                stroke-dasharray="2 6"
              />
              <text
                x="90"
                y="88"
                text-anchor="middle"
                fill="var(--ink)"
                font-size="30"
                font-weight="600"
              >
                {{ percentage.toFixed(0) }}
                <tspan font-size="15">%</tspan>
              </text>
            </svg>
            <span class="allocation-label">
              {{ t("console.currentAllocation") }}
            </span>
          </div>
        </section>
        <section class="panel workspace-snapshot">
          <div class="panel-heading">
            <h2>{{ t("console.workspaceResources") }}</h2>
            <span class="workspace-dot"><icon-apps /></span>
          </div>
          <p class="snapshot-tenant">{{ ui.tenant }}</p>
          <div class="snapshot-grid">
            <router-link
              v-for="item in [
                {
                  key: 'devMachines',
                  route: 'dev',
                  n: ov.devmachines.length,
                  icon: 'icon-desktop',
                },
                {
                  key: 'jobs',
                  route: 'jobs',
                  n: ov.jobs.length,
                  icon: 'icon-thunderbolt',
                },
                {
                  key: 'services',
                  route: 'services',
                  n: ov.services.length,
                  icon: 'icon-cloud',
                },
                {
                  key: 'volumes',
                  route: 'volumes',
                  n: ov.volumes.length,
                  icon: 'icon-storage',
                },
              ]"
              :key="item.route"
              :to="{ name: item.route }"
            >
              <span>
                <component :is="item.icon" />
                {{ t(`nav.${item.key}`) }}
              </span>
              <strong>
                {{ item.n }}
                <icon-arrow-rise :size="12" />
              </strong>
            </router-link>
          </div>
        </section>
      </div>
      <div class="metrics-grid overview-metrics">
        <MetricCard
          label="vCPU"
          :value="auth.isAdmin() ? totals.cpu : quota('cpu').used"
          :total="auth.isAdmin() ? totals.cpuTotal : (quota('cpu').hard ?? '—')"
          :precision="2"
          :detail="
            t(
              auth.isAdmin()
                ? 'console.gpuNodeAllocation'
                : 'console.allocated',
            )
          "
          icon="icon-desktop"
        />
        <MetricCard
          :label="t('overview.memoryGi')"
          :value="auth.isAdmin() ? totals.mem : quota('memory').used"
          :total="
            auth.isAdmin() ? totals.memTotal : (quota('memory').hard ?? '—')
          "
          :precision="2"
          :detail="
            t(
              auth.isAdmin()
                ? 'console.gpuNodeAllocation'
                : 'console.allocated',
            )
          "
          icon="icon-storage"
          accent="violet"
        />
        <MetricCard
          :label="
            auth.isAdmin()
              ? t('console.readyNodes')
              : t('console.activeWorkloads')
          "
          :value="auth.isAdmin() ? totals.ready : allWorkloads.length"
          :total="auth.isAdmin() ? fleet?.nodes.length : undefined"
          :detail="auth.isAdmin() ? t('console.gpuNodes') : ui.tenant"
          icon="icon-apps"
          accent="teal"
        />
        <MetricCard
          :label="auth.isAdmin() ? t('nav.alerts') : t('nav.volumes')"
          :value="
            auth.isAdmin()
              ? fleet?.errors?.alerts
                ? null
                : (fleet?.alerts.length ?? 0)
              : ov.volumes.length
          "
          :detail="
            auth.isAdmin()
              ? t(
                  fleet?.errors?.alerts
                    ? 'common.unavailable'
                    : 'alerts.firing',
                )
              : t('console.workspaceResources')
          "
          :icon="auth.isAdmin() ? 'icon-notification' : 'icon-folder'"
          accent="amber"
        />
      </div>
      <section class="launch-section">
        <div class="section-heading">
          <div>
            <h2>{{ t("console.quickStart") }}</h2>
            <p>{{ t("console.quickStartDesc") }}</p>
          </div>
          <span class="section-counter">01 — 03</span>
        </div>
        <div class="launcher-grid">
          <router-link
            v-for="(item, i) in launchers"
            :key="item.route"
            :to="{ name: item.route, query: { create: '1' } }"
            class="launcher"
            :class="item.color"
          >
            <div class="launcher-top">
              <span class="launcher-icon">
                <component :is="item.icon" :size="21" />
              </span>
              <span class="launcher-number">0{{ i + 1 }}</span>
            </div>
            <h3>
              {{ t(`console.${item.key}`) }}
              <icon-arrow-rise />
            </h3>
            <p>{{ t(`console.${item.key}Desc`) }}</p>
          </router-link>
        </div>
      </section>
      <section v-if="auth.isAdmin() && fleet" class="panel node-panel">
        <div class="panel-heading">
          <div>
            <h2>{{ t("console.fleetCapacity") }}</h2>
            <p>{{ t("console.fleetCapacityDesc") }}</p>
          </div>
          <router-link to="/fleet" class="view-link">
            {{ t("console.viewAll") }}
            <icon-arrow-right />
          </router-link>
        </div>
        <div class="node-grid">
          <router-link
            v-for="node in fleet.nodes"
            :key="node.nodeId"
            :to="{ name: 'fleet', query: { node: node.nodeId } }"
            class="node-tile"
          >
            <div class="node-tile-top">
              <span class="node-symbol"><icon-computer /></span>
              <strong>{{ node.nodeId }}</strong>
              <StatusBadge :status="node.ready ? 'Ready' : 'NotReady'" />
            </div>
            <div class="node-owner">
              {{ node.owner }}
              <span>· {{ node.pair }}</span>
            </div>
            <div
              class="gpu-blocks"
              :aria-label="`GPU ${node.gpuUsed} / ${node.gpuTotal}`"
            >
              <i
                v-for="n in node.gpuTotal"
                :key="n"
                :class="{ filled: n <= node.gpuUsed }"
              />
            </div>
            <div class="node-allocation">
              <span>{{ t("console.allocated") }}</span>
              <strong>
                {{ node.gpuUsed }}
                <span>/ {{ node.gpuTotal }} GPU</span>
              </strong>
            </div>
          </router-link>
        </div>
      </section>
      <div class="overview-bottom">
        <section class="panel workloads-panel">
          <div class="panel-heading">
            <h2>{{ t("console.resourceActivity") }}</h2>
            <span class="count-label">{{ allWorkloads.length }}</span>
          </div>
          <div v-if="allWorkloads.length" class="workload-items">
            <router-link
              v-for="item in allWorkloads.slice(0, 5)"
              :key="`${item.route}:${item.name}`"
              :to="{ name: item.route, query: { search: item.name } }"
            >
              <span class="resource-symbol"><component :is="item.icon" /></span>
              <div>
                <strong>{{ item.name }}</strong>
                <small>{{ item.kind }}</small>
              </div>
              <StatusBadge :status="item.phase" />
              <icon-arrow-rise />
            </router-link>
          </div>
          <EmptyState
            v-else
            :title="t('console.noWorkloads')"
            :description="t('console.noWorkloadsDesc')"
            compact
            icon="icon-code"
          >
            <router-link to="/dev?create=1" class="view-link">
              {{ t("console.createDev") }}
              <icon-arrow-right />
            </router-link>
          </EmptyState>
        </section>
        <section class="panel quota-panel">
          <div class="panel-heading">
            <h2>{{ t("console.workspaceUsage") }}</h2>
            <router-link
              to="/quota"
              class="view-link"
              :aria-label="t('nav.quota')"
            >
              <icon-arrow-rise />
            </router-link>
          </div>
          <div class="quota-bars">
            <div
              v-for="key in ['gpu', 'cpu', 'memory'] as const"
              :key="key"
              class="quota-item"
            >
              <div>
                <span>
                  {{ key === "memory" ? "RAM · GiB" : key.toUpperCase() }}
                </span>
                <strong>
                  {{ Number(quota(key).used.toFixed(2)) }}
                  <span>/ {{ quota(key).hard ?? "—" }}</span>
                </strong>
              </div>
              <QuotaMeter
                :label="key === 'memory' ? 'RAM GiB' : key.toUpperCase()"
                :description="`${quota(key).used} / ${quota(key).hard ?? '—'}`"
                :percent="
                  quota(key).hard
                    ? Math.min(1, quota(key).used / quota(key).hard!)
                    : 0
                "
                :height="6"
              />
            </div>
            <p class="hint">{{ t("console.quotaHint") }}</p>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>
<style scoped>
.updated {
  font-size: var(--text-xs);
  color: var(--muted);
}
.overview-top {
  display: grid;
  grid-template-columns: minmax(0, 1.75fr) minmax(280px, 1fr);
  gap: 20px;
}
.capacity-hero {
  background: var(--hero-bg);
  border: 1px solid var(--line);
  border-radius: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 27px 32px;
  color: var(--ink);
  overflow: hidden;
  position: relative;
}
.capacity-hero:before {
  content: "";
  position: absolute;
  width: 420px;
  height: 420px;
  border: 1px solid #ffffff04;
  border-radius: 50%;
  right: -130px;
  top: -145px;
  pointer-events: none;
}
.capacity-copy {
  min-width: 0;
  overflow-wrap: anywhere;
  position: relative;
  z-index: 1;
}
.capacity-eyebrow {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-xs);
  letter-spacing: 0.6px;
  color: var(--muted);
}
.tiny-chip {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 1px solid var(--control-border);
  border-radius: 6px;
  color: var(--accent);
}
.capacity-separator {
  color: var(--muted);
  margin: 0 4px;
}
.capacity-copy > p {
  color: var(--muted);
  font-size: var(--text-sm);
  margin: 24px 0 10px;
}
.capacity-number {
  font-size: 3.3125rem;
  font-weight: 600;
  letter-spacing: -2px;
  line-height: 1.05;
}
.capacity-number > span {
  font-size: var(--text-base);
  letter-spacing: 0;
  font-weight: 400;
  color: var(--muted);
  margin-left: 12px;
}
.capacity-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  align-items: center;
  margin-top: 22px;
  font-size: var(--text-xs);
  color: var(--muted);
}
.capacity-meta b {
  color: var(--ink);
  font-weight: 500;
  margin-left: 8px;
}
.capacity-meta i {
  display: inline-block;
  width: 5px;
  height: 5px;
  background: #7599ff;
  border-radius: 50%;
  margin-right: 5px;
}
.capacity-note {
  color: var(--muted);
  font-size: var(--text-xs);
  margin-top: 17px;
}
.capacity-viz {
  width: 180px;
  text-align: center;
  flex: none;
  margin-left: 16px;
  position: relative;
}
.capacity-viz svg {
  width: 100%;
  display: block;
}
.workspace-snapshot .panel-heading {
  padding-bottom: 0;
}
.workspace-dot {
  font-size: 1.0625rem;
  color: var(--muted);
}
.snapshot-tenant {
  font-size: var(--text-xs);
  color: var(--muted);
  padding: 0 24px;
  margin: 6px 0 22px;
}
.snapshot-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0;
  padding: 0 24px 20px;
}
.snapshot-grid > a {
  padding: 12px 0;
  text-decoration: none;
  color: var(--ink);
}
.snapshot-grid > a:nth-child(-n + 2) {
  border-bottom: 1px solid var(--line);
  padding-top: 0;
  padding-bottom: 20px;
}
.snapshot-grid > a:nth-child(even) {
  padding-left: 22px;
}
.snapshot-grid > a > span {
  font-size: var(--text-xs);
  color: var(--muted);
  display: flex;
  gap: 7px;
  align-items: center;
}
.snapshot-grid strong {
  font-size: 1.6875rem;
  font-weight: 600;
  margin-top: 9px;
  display: flex;
  align-items: center;
  gap: 14px;
  line-height: 1;
}
.snapshot-grid strong > svg {
  color: var(--muted);
  opacity: 0;
  transition: opacity 0.15s;
}
.snapshot-grid a:hover strong {
  color: var(--accent);
}
.snapshot-grid a:hover svg {
  opacity: 1;
}
.overview-metrics {
  margin: 0;
}
.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 18px;
}
.section-heading h2 {
  font-size: var(--text-lg);
  margin: 0;
  font-weight: 600;
}
.section-heading p {
  font-size: var(--text-sm);
  color: var(--muted);
  margin: 7px 0 0;
}
.section-counter {
  font: var(--text-xs) / 1.5 var(--font-mono);
  color: var(--muted);
  letter-spacing: 1px;
}
.launcher-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px;
}
.launcher {
  display: block;
  padding: 22px 24px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface);
  text-decoration: none;
  color: var(--ink);
  transition:
    transform 0.18s,
    border 0.18s,
    box-shadow 0.18s;
}
.launcher:hover {
  transform: translateY(-3px);
  border-color: color-mix(in srgb, var(--accent) 40%, var(--line));
  box-shadow: 0 8px 20px #10182807;
}
.launcher-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.launcher-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
}
.violet .launcher-icon {
  background: var(--violet-soft);
  color: var(--violet);
}
.teal .launcher-icon {
  background: var(--teal-soft);
  color: var(--teal);
}
.launcher-number {
  font: var(--text-xs) / 1.5 var(--font-mono);
  color: var(--muted);
}
.launcher h3 {
  font-size: var(--text-base);
  font-weight: 600;
  margin: 23px 0 9px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.launcher h3 svg {
  color: var(--muted);
}
.launcher p {
  font-size: var(--text-sm);
  color: var(--muted);
  line-height: 1.85;
  margin: 0;
  max-width: 280px;
}
.node-panel {
  margin-top: 0;
}
.panel-heading p {
  font-size: var(--text-xs);
  color: var(--muted);
  margin: 7px 0 0;
}
.view-link {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-xs);
  color: var(--accent);
  text-decoration: none;
}
.node-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 0;
  padding: 4px 8px 24px;
}
.node-tile {
  display: block;
  min-width: 0;
  padding: 0 18px;
  text-decoration: none;
  color: var(--ink);
  border-right: 1px solid var(--line);
}
.node-tile:last-child {
  border: 0;
}
.node-tile-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.node-symbol {
  color: var(--muted);
  font-size: 1.0625rem;
}
.node-tile-top strong {
  font-size: var(--text-sm);
  font-weight: 600;
}
.node-tile-top :deep(.status-badge) {
  margin-left: auto;
  font-size: var(--text-xs);
  padding: 3px 6px;
}
.node-tile-top :deep(.status-badge i) {
  width: 4px;
  height: 4px;
}
.node-owner {
  font-size: var(--text-xs);
  color: var(--muted);
  margin: 12px 0 22px;
}
.node-owner > span {
}
.gpu-blocks {
  display: flex;
  gap: 5px;
  height: 20px;
  margin-bottom: 12px;
}
.gpu-blocks i {
  background: var(--accent-soft);
  flex: 1;
  border: 1px solid color-mix(in srgb, var(--accent) 8%, var(--line));
  border-radius: 3px;
}
.gpu-blocks i.filled {
  background: var(--accent);
  border-color: var(--accent);
}
.node-allocation {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: var(--text-xs);
  color: var(--muted);
}
.node-allocation strong {
  font-size: var(--text-sm);
  color: var(--ink);
  font-weight: 500;
}
.node-allocation strong span {
  color: var(--muted);
}
.overview-bottom {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(280px, 1fr);
  gap: 20px;
  margin-top: 0;
}
.count-label {
  font-size: var(--text-xs);
  color: var(--muted);
  background: var(--surface-soft);
  padding: 3px 7px;
  border-radius: 5px;
}
.quota-bars {
  padding: 0 24px 18px;
}
.quota-item {
  margin-bottom: 19px;
}
.quota-item > div {
  display: flex;
  justify-content: space-between;
  font-size: var(--text-xs);
  margin-bottom: 9px;
}
.quota-item strong {
  font-weight: 500;
}
.quota-item strong span {
  color: var(--muted);
}
.quota-bars .hint {
  font-size: var(--text-xs);
  margin-bottom: 0;
}
.workload-items > a {
  display: flex;
  gap: 12px;
  padding: 14px 24px;
  border-top: 1px solid var(--line);
  align-items: center;
  color: var(--ink);
  text-decoration: none;
}
.workload-items > a > div {
  flex: 1;
  min-width: 0;
}
.workload-items strong {
  display: block;
  font-size: var(--text-sm);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
}
.workload-items small {
  font-size: var(--text-xs);
  color: var(--muted);
  display: block;
  margin-top: 5px;
}
.workload-items > a > svg {
  color: var(--muted);
}
@media (max-width: 1250px) {
  .capacity-viz {
    width: 145px;
  }
  .capacity-hero {
    padding: 25px;
  }
  .node-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    row-gap: 25px;
  }
  .node-tile:nth-child(2) {
    border: 0;
  }
  .node-tile-top :deep(.status-badge) {
    font-size: var(--text-xs);
  }
  .capacity-note {
    max-width: 210px;
    line-height: 1.5;
  }
}
@media (max-width: 950px) {
  .overview-top,
  .overview-bottom {
    grid-template-columns: 1fr;
  }

  .launcher {
    padding: 18px;
  }
  .launcher p {
    font-size: var(--text-xs);
  }
  .capacity-viz {
    width: 175px;
  }
  .capacity-note {
    max-width: none;
  }
  .capacity-hero {
    padding: 26px 30px;
  }
  .overview-metrics {
    margin-top: 0;
  }
  .launcher h3 {
    font-size: var(--text-sm);
  }
}
@media (max-width: 600px) {
  .capacity-hero {
    padding: 22px;
    flex-wrap: wrap;
    gap: 20px;
  }
  .capacity-viz {
    width: 145px;
    margin-left: 0;
  }
  .capacity-number {
    font-size: 2.8125rem;
  }
  .capacity-meta {
    gap: 15px;
    font-size: var(--text-xs);
  }
  .capacity-note {
    max-width: none;
    font-size: var(--text-xs);
    line-height: 1.6;
  }
  .capacity-eyebrow {
    font-size: var(--text-xs);
  }
  .launcher-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }
  .launcher {
    padding: 18px 20px;
    position: relative;
    padding-left: 78px;
    min-height: 100px;
  }
  .launcher-top {
    position: absolute;
    left: 20px;
    top: 24px;
  }
  .launcher-number {
    display: none;
  }
  .launcher h3 {
    margin: 4px 0 8px;
  }
  .launcher p {
    max-width: none;
  }
  .node-grid {
    grid-template-columns: 1fr;
    row-gap: 22px;
    padding-bottom: 24px;
  }
  .node-tile {
    border: 0;
  }
  .node-owner {
    margin: 10px 0 14px;
  }
  .gpu-blocks {
    height: 16px;
  }
  .updated {
    display: none;
  }
  .section-counter {
    display: none;
  }
  .overview-bottom {
    gap: 18px;
  }
  .workload-items > a {
    padding: 14px 16px;
  }
  .capacity-copy {
    min-width: 0;
  }
  .capacity-copy > p {
    margin-top: 20px;
  }
  .capacity-meta {
    flex-wrap: wrap;
    gap: 8px;
  }
  .node-tile-top strong {
    font-size: var(--text-base);
  }
}
.allocation-label {
  display: block;
  font-size: var(--text-xs);
  color: var(--muted);
  margin-top: -30px;
  position: relative;
}
</style>
