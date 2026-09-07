<script setup lang="ts">
import DataTable from "@/components/DataTable.vue";
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { papi } from "@/api";
import { useUiStore } from "@/stores/ui";
import type { Usage } from "@/api/types";
import { formatTime, downloadText } from "@/utils/clipboard";
import PageHeading from "@/components/PageHeading.vue";
import MetricCard from "@/components/MetricCard.vue";
import EmptyState from "@/components/EmptyState.vue";
import StatusBadge from "@/components/StatusBadge.vue";
const { t } = useI18n(),
  ui = useUiStore();
const u = ref<Usage | null>(null),
  loadError = ref(""),
  loading = ref(true),
  query = ref(""),
  filter = ref("all");
async function load() {
  loading.value = true;
  loadError.value = "";
  const ns = ui.tenant;
  try {
    const value = await papi.usage(ns);
    if (ns === ui.tenant) u.value = value;
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
const rows = computed(() =>
  (u.value?.intervals ?? []).filter(
    (row) =>
      row.name.toLowerCase().includes(query.value.toLowerCase().trim()) &&
      (filter.value === "all" ||
        (filter.value === "open" ? row.open : !row.open)),
  ),
);
const columns = computed(() => [
  { title: t("common.name"), slotName: "name" },
  { title: t("usage.openedAt"), slotName: "opened" },
  { title: t("usage.closedAt"), slotName: "closed" },
  { title: t("usage.hours"), slotName: "hours", align: "right" as const },
  { title: "GPU", dataIndex: "gpu", align: "right" as const },
  { title: "GiB", dataIndex: "storage_gib", align: "right" as const },
]);
function exportCsv() {
  const cell = (v: unknown) => {
    let text = String(v ?? "");
    if (typeof v === "string" && /^[\s]*[=+@-]/.test(text)) text = "'" + text;
    return '"' + text.replace(/"/g, '""') + '"';
  };
  const contents = [
    [
      "name",
      "kind",
      "opened_at",
      "closed_at",
      "hours",
      "gpu",
      "storage_gib",
      "open",
    ],
    ...rows.value.map((r) => [
      r.name,
      r.kind,
      r.opened_at,
      r.closed_at,
      Number((r.seconds / 3600).toFixed(6)),
      r.gpu,
      r.storage_gib,
      r.open,
    ]),
  ]
    .map((row) => row.map(cell).join(","))
    .join("\r\n");
  downloadText(
    `arise-usage-${ui.tenant}-${new Date().toISOString().slice(0, 10)}.csv`,
    "\ufeff" + contents,
    "text/csv;charset=utf-8",
  );
}
</script>
<template>
  <div class="page">
    <PageHeading :title="t('nav.usage')" :description="t('console.usageDesc')">
      <template #actions>
        <a-button
          :loading="loading"
          :aria-label="t('common.refresh')"
          @click="load"
        >
          <icon-refresh />
        </a-button>
        <a-button :disabled="!rows.length || !!loadError" @click="exportCsv">
          <template #icon><icon-download /></template>
          {{ t("console.exportCsv") }}
        </a-button>
      </template>
    </PageHeading>
    <a-alert v-if="loadError" type="error" class="page-error">
      {{ loadError }}
    </a-alert>
    <div v-if="u && !loadError" class="metrics-grid usage-metrics">
      <MetricCard
        :label="t('usage.gpuHours')"
        :value="u.gpu_hours"
        :precision="2"
        icon="icon-thunderbolt"
        detail="GPU · h"
      />
      <MetricCard
        :label="t('usage.gpuNow')"
        :value="u.gpu_allocated_now"
        icon="icon-desktop"
        accent="violet"
        :detail="t('console.currentAllocation')"
      />
      <MetricCard
        :label="t('usage.storageGibHours')"
        :value="u.storage_gib_hours"
        :precision="1"
        icon="icon-storage"
        accent="teal"
        detail="GiB · h"
      />
      <MetricCard
        :label="t('usage.storageNow')"
        :value="u.storage_gib_now"
        icon="icon-folder"
        accent="amber"
        :detail="t('console.currentAllocation')"
      />
    </div>
    <div class="usage-note">
      <icon-info-circle />
      <span>{{ t("console.usageHint") }}</span>
      <time v-if="u" :title="u.as_of">
        {{ t("usage.asOf") }} {{ formatTime(u.as_of, ui.locale) }}
      </time>
    </div>
    <section class="panel usage-panel">
      <div class="panel-heading">
        <h2>{{ t("console.allocations") }}</h2>
        <span class="muted">{{ t("console.localTime") }}</span>
      </div>
      <div class="toolbar">
        <div class="filter-tabs">
          <button
            v-for="key in ['all', 'open', 'closed']"
            :key="key"
            type="button"
            :aria-pressed="filter === key"
            :class="{ active: filter === key }"
            @click="filter = key"
          >
            {{
              t(
                key === "all"
                  ? "console.allResources"
                  : key === "open"
                    ? "console.activeAllocations"
                    : "console.finished",
              )
            }}
          </button>
        </div>
        <a-input
          v-model="query"
          class="toolbar-search"
          allow-clear
          :placeholder="t('console.searchUsage')"
          :input-attrs="{ 'aria-label': t('console.searchUsage') }"
        >
          <template #prefix><icon-search /></template>
        </a-input>
      </div>
      <DataTable
        :columns="columns"
        :data="loadError ? [] : rows"
        :loading="loading"
        :pagination="{ pageSize: 15, hideOnSinglePage: true }"
        :row-key="
          (r: any) => `${r.name}|${r.opened_at}|${r.closed_at}|${r.kind}`
        "
        :bordered="false"
      >
        <template #name="{ record }">
          <strong class="usage-name">{{ record.name }}</strong>
          <span class="resource-sub">{{ record.kind }}</span>
        </template>
        <template #opened="{ record }">
          <time :title="record.opened_at">
            {{ formatTime(record.opened_at, ui.locale) }}
          </time>
        </template>
        <template #closed="{ record }">
          <StatusBadge
            v-if="record.open"
            status="Running"
            :label="t('usage.open')"
          />
          <time v-else :title="record.closed_at">
            {{ formatTime(record.closed_at, ui.locale) }}
          </time>
        </template>
        <template #hours="{ record }">
          <span class="mono">{{ (record.seconds / 3600).toFixed(2) }}</span>
        </template>
        <template #empty>
          <EmptyState
            :title="
              loadError
                ? t('common.unavailable')
                : t(
                    query || filter !== 'all'
                      ? 'console.emptyFiltered'
                      : 'console.noUsage',
                  )
            "
            :description="
              loadError
                ? loadError
                : t(
                    query || filter !== 'all'
                      ? 'console.emptyFilteredDesc'
                      : 'console.noUsageDesc',
                  )
            "
            icon="icon-file"
          >
            <a-button
              v-if="query || filter !== 'all'"
              @click="
                query = '';
                filter = 'all';
              "
            >
              {{ t("console.clearFilters") }}
            </a-button>
          </EmptyState>
        </template>
      </DataTable>
      <div v-if="u" class="usage-footer">
        {{ t("usage.intervals") }} · {{ rows.length }} / {{ u.interval_count }}
      </div>
    </section>
  </div>
</template>
<style scoped>
.usage-metrics {
  margin-bottom: 0;
}
.usage-note {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  margin: 0 2px;
  font-size: var(--text-xs);
  color: var(--muted);
  line-height: 1.8;
}
.usage-note > svg {
  flex: none;
  margin-top: 3px;
}
.usage-note > time {
  margin-left: auto;
  white-space: nowrap;
}
.usage-panel {
  overflow: hidden;
}
.usage-panel > .panel-heading > .muted {
  font-size: var(--text-xs);
}
.usage-panel > .toolbar {
  padding: 0 24px 18px;
}
.usage-name {
  font-size: var(--text-sm);
  font-weight: 500;
  overflow-wrap: anywhere;
}
.usage-panel time {
  font-size: var(--text-xs);
  color: var(--muted);
}
.usage-footer {
  border-top: 1px solid var(--line);
  padding: 15px 24px;
  font-size: var(--text-xs);
  color: var(--muted);
}
@media (max-width: 767px) {
  .usage-note {
    flex-wrap: wrap;
  }
  .usage-note > span {
    flex: 1;
  }
  .usage-note > time {
    width: 100%;
    margin-left: 22px;
  }
  .usage-panel > .panel-heading > .muted {
    display: none;
  }
  .usage-panel > .toolbar {
    padding: 0 16px 16px;
  }
}
</style>
