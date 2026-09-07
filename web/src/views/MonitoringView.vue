<script setup lang="ts">
import { selectScrollbar } from "@/utils/accessibility";
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { metrics } from "@/api";
import { downloadText, formatTime } from "@/utils/clipboard";
import {
  localDateInput,
  rangeError,
  rangeStep,
  RANGE_PRESETS,
  type TimeWindow,
} from "@/utils/monitoring";
import LineChart from "@/components/LineChart.vue";
import PageHeading from "@/components/PageHeading.vue";
import EmptyState from "@/components/EmptyState.vue";
const { t, locale } = useI18n();
const selectedRange = ref<number | "custom">(24);
const activeWindow = ref<TimeWindow | null>(null);
const customWindow = ref<TimeWindow | null>(null);
const customOpen = ref(false),
  draftStart = ref(""),
  draftEnd = ref(""),
  draftError = ref("");
const refreshEnabled = ref(true),
  loading = ref(false),
  updatedAt = ref<number | null>(null),
  loadError = ref("");
const gpuSeries = ref<Array<[number, number]>>([]),
  contractSeries = ref<Array<[number, number]>>([]);
const capacity = ref<Array<{ node: string; value: number }>>([]);
let timer: number | undefined,
  generation = 0,
  pending: AbortController | undefined;
const rangeLabel = (h: number) =>
  h < 48
    ? t("monitoring.lastHours", { n: h })
    : t("monitoring.lastDays", { n: h / 24 });
const currentRangeLabel = computed(() =>
  selectedRange.value === "custom"
    ? t("monitoring.custom")
    : rangeLabel(selectedRange.value),
);
const sampleStep = computed(() =>
  activeWindow.value
    ? rangeStep(activeWindow.value.end - activeWindow.value.start)
    : 30,
);
const hasSamples = computed(
  () =>
    gpuSeries.value.some((p) => Number.isFinite(p[1])) ||
    contractSeries.value.some((p) => Number.isFinite(p[1])),
);
const partialHistory = computed(() => {
  if (!activeWindow.value) return false;
  const first = gpuSeries.value.find((p) => Number.isFinite(p[1]));
  return !!first && first[0] - activeWindow.value.start > sampleStep.value * 2;
});
const fmt = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString(
    locale.value === "zh" ? "zh-CN" : "en-US",
    {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    },
  );
function chooseRange(value: unknown) {
  if (value === "custom") {
    openCustom();
    return;
  }
  if (!RANGE_PRESETS.some((h) => h === Number(value))) return;
  selectedRange.value = Number(value);
  customWindow.value = null;
  load();
}
function openCustom() {
  const end = Math.floor(Date.now() / 1000);
  draftStart.value = localDateInput(
    customWindow.value?.start ?? activeWindow.value?.start ?? end - 86400,
  );
  draftEnd.value = localDateInput(customWindow.value?.end ?? end);
  draftError.value = "";
  customOpen.value = true;
}
function applyCustom() {
  const start = new Date(draftStart.value).getTime() / 1000;
  const end = new Date(draftEnd.value).getTime() / 1000;
  const error = rangeError(start, end);
  if (error) {
    draftError.value = t(`monitoring.rangeError.${error}`);
    return false;
  }
  customWindow.value = { start, end };
  selectedRange.value = "custom";
  load();
  return true;
}
async function load(silent = false) {
  const ticket = ++generation;
  pending?.abort();
  pending = new AbortController();
  const signal = pending.signal;
  loading.value = true;
  loadError.value = "";
  const end = Math.floor(Date.now() / 1000);
  const window = customWindow.value ?? {
    start: end - Number(selectedRange.value) * 3600,
    end,
  };
  const step = rangeStep(window.end - window.start);
  // Clear previous-window values before querying so the new label cannot
  // describe stale data. Explicit errors retain the last successful timestamp.
  if (!silent) {
    gpuSeries.value = [];
    contractSeries.value = [];
    capacity.value = [];
  }
  activeWindow.value = window;
  async function series(q: string) {
    const result = await metrics.queryRange(
      q,
      window.start,
      window.end,
      step,
      signal,
    );
    return (
      result.data.result[0]?.values.map(
        (v) => [Number(v[0]), Number(v[1])] as [number, number],
      ) ?? []
    ).filter(([at]) => Number.isFinite(at));
  }
  try {
    const [gpu, contracts, nodes] = await Promise.all([
      series(
        'sum(kube_node_status_allocatable{resource="nvidia_com_gpu"}) or sum(arise_fake_gpu_healthy)',
      ),
      series("sum(arise_active_contracts)"),
      metrics.query(
        'max by (node) (kube_node_status_allocatable{resource="nvidia_com_gpu"}) or max by (node) (arise_fake_gpu_capacity)',
        signal,
      ),
    ]);
    if (ticket !== generation) return;
    gpuSeries.value = gpu;
    contractSeries.value = contracts;
    capacity.value = nodes.data.result
      .map((r) => ({ node: r.metric.node, value: Number(r.value[1]) }))
      .filter((r) => Number.isFinite(r.value))
      .sort((a, b) => a.node.localeCompare(b.node));
    updatedAt.value = Date.now();
  } catch (e) {
    if (ticket === generation && !signal.aborted)
      loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (ticket === generation) loading.value = false;
  }
}
function exportMetrics() {
  const rows = [["timestamp_utc", "metric", "value"]];
  for (const [name, values] of [
    ["allocatable_gpu", gpuSeries.value],
    ["active_contracts", contractSeries.value],
  ] as const) {
    for (const [timestamp, value] of values)
      rows.push([
        new Date(timestamp * 1000).toISOString(),
        name,
        Number.isFinite(value) ? String(value) : "",
      ]);
  }
  downloadText(
    "compute-metrics.csv",
    rows.map((r) => r.join(",")).join("\r\n") + "\r\n",
    "text/csv;charset=utf-8",
  );
}
onMounted(() => {
  load();
  timer = window.setInterval(() => {
    if (
      refreshEnabled.value &&
      selectedRange.value !== "custom" &&
      !document.hidden &&
      !loading.value
    )
      load(true);
  }, 30000);
});
onUnmounted(() => {
  generation++;
  pending?.abort();
  clearInterval(timer);
});
</script>
<template>
  <div class="page monitoring-page">
    <PageHeading
      :title="t('nav.monitoring')"
      :description="t('console.monitoringDesc')"
    >
      <template #actions>
        <a-button :disabled="!hasSamples || loading" @click="exportMetrics">
          <template #icon><icon-download /></template>
          {{ t("monitoring.export") }}
        </a-button>
      </template>
    </PageHeading>
    <section
      class="panel monitoring-controls"
      :aria-label="t('monitoring.period')"
    >
      <div class="monitoring-range">
        <label id="monitoring-range-label">{{ t("monitoring.period") }}</label>
        <a-select
          :scrollbar="selectScrollbar"
          class="monitoring-range-select"
          :model-value="selectedRange"
          :placeholder="t('monitoring.period')"
          @change="chooseRange"
        >
          <a-option v-for="h in RANGE_PRESETS" :key="h" :value="h">
            {{ rangeLabel(h) }}
          </a-option>
          <a-option value="custom">{{ t("monitoring.custom") }}</a-option>
        </a-select>
        <a-button v-if="selectedRange === 'custom'" @click="openCustom">
          {{ t("monitoring.editRange") }}
        </a-button>
      </div>
      <div class="monitoring-refresh">
        <a-button
          v-if="selectedRange !== 'custom'"
          class="refresh-toggle"
          :aria-pressed="refreshEnabled"
          @click="refreshEnabled = !refreshEnabled"
        >
          <template #icon>
            <icon-pause v-if="refreshEnabled" />
            <icon-play-arrow v-else />
          </template>
          {{ t(refreshEnabled ? "monitoring.pause" : "monitoring.resume") }}
        </a-button>
        <a-button
          :loading="loading"
          :aria-label="t('common.refresh')"
          @click="load()"
        >
          <template #icon><icon-refresh /></template>
          {{ t("common.refresh") }}
        </a-button>
      </div>
      <div class="monitoring-window" v-if="activeWindow">
        <span>{{ fmt(activeWindow.start) }} — {{ fmt(activeWindow.end) }}</span>
        <span>{{ t("console.localTime") }}</span>
      </div>
      <div class="monitoring-status" role="status">
        <span v-if="loading">{{ t("monitoring.loading") }}</span>
        <span v-else-if="updatedAt">
          {{ t("console.lastUpdated") }}
          {{ formatTime(new Date(updatedAt).toISOString(), locale) }}
        </span>
        <span>
          {{
            t(
              selectedRange === "custom"
                ? "monitoring.fixedWindow"
                : refreshEnabled
                  ? "monitoring.refresh30"
                  : "monitoring.paused",
            )
          }}
        </span>
      </div>
    </section>
    <a-alert v-if="loadError" type="error" class="page-error" role="alert">
      {{ loadError }}
      <a-button size="small" @click="load()">
        {{ t("common.refresh") }}
      </a-button>
    </a-alert>
    <a-alert v-else-if="!loading && partialHistory" type="info">
      {{ t("monitoring.partialHistory") }}
    </a-alert>
    <p class="section-note monitoring-retention">
      <icon-info-circle />
      {{ t("monitoring.retentionNote") }}
    </p>
    <div class="chart-grid" :aria-busy="loading">
      <section
        class="panel chart-panel"
        v-for="chart in [
          {
            key: 'gpu',
            title: t('monitoring.gpuCapacity'),
            points: gpuSeries,
            unit: 'GPU',
            color: 'var(--accent)',
          },
          {
            key: 'contracts',
            title: t('overview.contracts'),
            points: contractSeries,
            unit: '',
            color: 'var(--teal)',
          },
        ]"
        :key="chart.key"
      >
        <div class="panel-heading">
          <h2>{{ chart.title }}</h2>
          <span class="series-dot" :style="{ background: chart.color }" />
        </div>
        <div class="chart-body">
          <div v-if="loading && !chart.points.length" class="chart-loading">
            <a-spin />
            <span>{{ t("monitoring.loading") }}</span>
          </div>
          <LineChart
            v-else
            :points="chart.points"
            :unit="chart.unit"
            :color="chart.color"
            :label="chart.title"
            :window="activeWindow ?? undefined"
            :step="sampleStep"
          />
        </div>
        <div class="chart-note">
          {{
            chart.key === "gpu" ? t("console.capacityNote") : currentRangeLabel
          }}
        </div>
      </section>
    </div>
    <section class="panel capacity-panel">
      <div class="panel-heading">
        <h2>{{ t("monitoring.perNodeCapacity") }}</h2>
        <span class="muted">GPU</span>
      </div>
      <div v-if="capacity.length" class="capacity-list">
        <div v-for="node in capacity" :key="node.node" class="capacity-row">
          <span class="resource-symbol"><icon-computer /></span>
          <strong>{{ node.node }}</strong>
          <div class="capacity-bar">
            <span
              :style="{
                width: `${(node.value / Math.max(1, ...capacity.map((n) => n.value))) * 100}%`,
              }"
            />
          </div>
          <span class="capacity-value">
            {{ node.value }}
            <span>GPU</span>
          </span>
        </div>
      </div>
      <EmptyState
        v-else-if="!loading"
        :title="t('monitoring.noCapacity')"
        :description="loadError || t('monitoring.emptyHelp')"
        icon="icon-bar-chart"
        compact
      />
      <div v-else class="chart-loading">
        <a-spin />
        <span>{{ t("monitoring.loading") }}</span>
      </div>
    </section>
    <a-modal
      v-model:visible="customOpen"
      :title="t('monitoring.custom')"
      :ok-text="t('monitoring.apply')"
      :on-before-ok="applyCustom"
      :width="560"
    >
      <p class="hint">{{ t("monitoring.customHelp") }}</p>
      <div class="custom-range-inputs">
        <label for="monitor-start">
          {{ t("monitoring.start") }}
          <input
            id="monitor-start"
            type="datetime-local"
            v-model="draftStart"
            :max="localDateInput(Date.now() / 1000)"
          />
        </label>
        <label for="monitor-end">
          {{ t("monitoring.end") }}
          <input
            id="monitor-end"
            type="datetime-local"
            v-model="draftEnd"
            :max="localDateInput(Date.now() / 1000)"
          />
        </label>
      </div>
      <a-alert
        v-if="draftError"
        type="error"
        role="alert"
        style="margin-top: 20px"
      >
        {{ draftError }}
      </a-alert>
    </a-modal>
  </div>
</template>
<style scoped>
.monitoring-controls {
  display: flex;
  align-items: center;
  gap: 18px;
  flex-wrap: wrap;
  padding: 20px 24px;
  overflow: visible;
}
.monitoring-range,
.monitoring-refresh {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.monitoring-range label {
  font-size: var(--text-sm);
  font-weight: 600;
}
.monitoring-range :deep(.monitoring-range-select) {
  width: 200px;
}
.monitoring-refresh {
  margin-left: auto;
}
.monitoring-window,
.monitoring-status {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  font-size: var(--text-sm);
  color: var(--muted);
  width: 100%;
}
.monitoring-status {
  padding-top: 14px;
  border-top: 1px solid var(--line);
}
.monitoring-retention {
  margin: -4px 0;
}
.chart-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
}
.chart-panel {
  min-width: 0;
}
.series-dot {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  border-radius: 50%;
}
.chart-body {
  padding: 0 24px 16px;
}
.chart-note {
  padding: 16px 24px;
  border-top: 1px solid var(--line);
  font-size: var(--text-sm);
  color: var(--muted);
}
.chart-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  min-height: 294px;
  color: var(--muted);
  font-size: var(--text-sm);
}
.capacity-list {
  padding: 0 24px 24px;
  display: grid;
  gap: 12px;
}
.capacity-row {
  display: grid;
  grid-template-columns: 36px minmax(80px, 180px) 1fr auto;
  align-items: center;
  gap: 16px;
  padding: 12px 0;
}
.capacity-row strong {
  font-size: var(--text-sm);
  font-weight: 500;
  overflow-wrap: anywhere;
}
.capacity-bar {
  height: 10px;
  background: var(--surface-hover);
  border-radius: 5px;
  overflow: hidden;
}
.capacity-bar > span {
  display: block;
  height: 100%;
  background: var(--accent);
  border-radius: 5px;
}
.capacity-value {
  font-size: var(--text-lg);
  font-weight: 600;
  white-space: nowrap;
}
.capacity-value span {
  font-size: var(--text-sm);
  color: var(--muted);
  font-weight: 400;
}
.custom-range-inputs {
  display: grid;
  gap: 20px;
}
.custom-range-inputs label {
  display: grid;
  gap: 8px;
  font-size: var(--text-base);
  font-weight: 500;
}
.custom-range-inputs input {
  width: 100%;
  min-width: 0;
  height: 44px;
  border: 1px solid var(--control-border);
  border-radius: 8px;
  background: var(--surface);
  color: var(--ink);
  padding: 8px 12px;
  outline-offset: 3px;
}
@media (max-width: 1199px) {
  .chart-grid {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 767px) {
  .monitoring-controls {
    padding: 18px;
  }
  .monitoring-range,
  .monitoring-refresh {
    width: 100%;
    margin-left: 0;
  }
  .monitoring-range :deep(.monitoring-range-select) {
    flex: 1;
    min-width: 150px;
  }
  .monitoring-refresh :deep(.arco-btn) {
    flex: 1;
  }
  .chart-body {
    padding: 0 12px 16px;
  }
  .capacity-list {
    padding: 0 18px 18px;
  }
  .capacity-row {
    grid-template-columns: 32px minmax(0, 1fr) auto;
    gap: 12px;
  }
  .capacity-bar {
    grid-row: 2;
    grid-column: 2 / 4;
  }
  .capacity-value {
    grid-row: 1;
    grid-column: 3;
  }
}
</style>
