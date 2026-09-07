<script setup lang="ts">
import DataTable from "@/components/DataTable.vue";
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { metrics } from "@/api";
import { useUiStore } from "@/stores/ui";
import type { AlertRow } from "@/api/types";
import { formatTime } from "@/utils/clipboard";
import PageHeading from "@/components/PageHeading.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
const { t } = useI18n(),
  ui = useUiStore();
const alerts = ref<AlertRow[]>([]),
  loading = ref(true),
  loadError = ref(""),
  query = ref("");
async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    alerts.value = await metrics.alerts();
  } catch (e) {
    alerts.value = [];
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
const rows = computed(() =>
  alerts.value
    .map((a, i) => ({
      key: String(i),
      sev: a.labels.severity || "—",
      name: a.labels.alertname || "—",
      node: a.labels.node || "—",
      since: a.startsAt,
    }))
    .filter((a) =>
      `${a.name} ${a.node} ${a.sev}`
        .toLowerCase()
        .includes(query.value.toLowerCase().trim()),
    ),
);
const columns = computed(() => [
  { title: t("alerts.severity"), slotName: "sev", width: 130 },
  { title: t("alerts.name"), slotName: "name" },
  { title: t("alerts.object"), dataIndex: "node" },
  { title: t("alerts.since"), slotName: "since" },
]);
</script>
<template>
  <div class="page">
    <PageHeading
      :title="t('nav.alerts')"
      :description="t('console.alertsDesc')"
    >
      <template #actions>
        <a-button :loading="loading" @click="load">
          <template #icon><icon-refresh /></template>
          {{ t("common.refresh") }}
        </a-button>
      </template>
    </PageHeading>
    <a-alert v-if="loadError" type="error" class="page-error">
      <strong>{{ t("alerts.loadFailed") }}</strong>
      <p>{{ loadError }}</p>
    </a-alert>
    <section class="panel alerts-panel">
      <div class="panel-heading">
        <h2>
          {{ t("alerts.firing") }}
          <span v-if="!loadError && !loading" class="alert-count">
            {{ alerts.length }}
          </span>
        </h2>
        <a-input
          v-model="query"
          allow-clear
          :placeholder="t('console.searchEvents')"
          :input-attrs="{ 'aria-label': t('console.searchEvents') }"
        >
          <template #prefix><icon-search /></template>
        </a-input>
      </div>
      <a-spin :loading="loading" style="display: block">
        <DataTable
          v-if="alerts.length && !loadError"
          :columns="columns"
          :data="rows"
          :pagination="{ pageSize: 15, hideOnSinglePage: true }"
          row-key="key"
          :bordered="false"
        >
          <template #sev="{ record }">
            <StatusBadge :status="record.sev" />
          </template>
          <template #name="{ record }">
            <strong class="alert-name">{{ record.name }}</strong>
          </template>
          <template #since="{ record }">
            <time :title="record.since">
              {{ formatTime(record.since, ui.locale) }}
            </time>
          </template>
          <template #empty>
            <EmptyState
              :title="t('console.emptyFiltered')"
              :description="t('console.emptyFilteredDesc')"
              compact
            >
              <a-button @click="query = ''">
                {{ t("console.clearFilters") }}
              </a-button>
            </EmptyState>
          </template>
        </DataTable>
        <div v-else-if="!loading && !loadError" class="all-clear">
          <div class="all-clear-symbol"><icon-check :size="27" /></div>
          <span class="eyebrow">{{ t("alerts.firing") }} · 0</span>
          <h2>{{ t("console.allClear") }}</h2>
          <p>{{ t("console.allClearDesc") }}</p>
          <a-button @click="load">
            <template #icon><icon-refresh /></template>
            {{ t("common.refresh") }}
          </a-button>
        </div>
        <EmptyState
          v-else-if="!loading"
          :title="t('common.unavailable')"
          icon="icon-wifi"
        >
          <a-button @click="load">{{ t("common.refresh") }}</a-button>
        </EmptyState>
        <div v-else class="alert-loading" />
      </a-spin>
      <div class="alert-footer">
        <icon-info-circle />
        {{ t("console.localTime") }}
      </div>
    </section>
  </div>
</template>
<style scoped>
.alerts-panel {
  overflow: hidden;
}
.alerts-panel > .panel-heading h2 {
  display: flex;
  gap: 10px;
  align-items: center;
}
.alert-count {
  font-size: var(--text-xs);
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 5px;
  color: var(--muted);
  font-weight: 400;
}
.alerts-panel > .panel-heading :deep(.arco-input-wrapper) {
  max-width: 280px;
}
.all-clear {
  padding: 60px 24px 65px;
  text-align: center;
  border-top: 1px solid var(--line);
}
.all-clear-symbol {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: var(--success-soft);
  color: var(--success);
  display: grid;
  place-items: center;
  margin: 0 auto 24px;
  box-shadow: 0 0 0 10px color-mix(in srgb, var(--success) 4%, var(--surface));
}
.all-clear > .eyebrow {
  font-size: var(--text-xs);
  color: var(--success);
  display: block;
  margin-top: 27px;
}
.all-clear h2 {
  font-size: 1.3125rem;
  font-weight: 600;
  margin: 13px 0 12px;
}
.all-clear p {
  font-size: var(--text-sm);
  color: var(--muted);
  line-height: 1.8;
  margin: 0 auto 25px;
  max-width: 360px;
}
.alert-footer {
  padding: 16px 24px;
  border-top: 1px solid var(--line);
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: var(--text-xs);
  color: var(--muted);
}
.alert-name {
  font-size: var(--text-sm);
  font-weight: 500;
}
.alerts-panel time {
  font-size: var(--text-xs);
  color: var(--muted);
}
.alert-loading {
  min-height: 300px;
}
@media (max-width: 600px) {
  .alerts-panel > .panel-heading {
    flex-direction: column;
    align-items: flex-start;
    gap: 16px;
  }
  .alerts-panel > .panel-heading :deep(.arco-input-wrapper) {
    max-width: none;
  }
  .all-clear {
    padding: 55px 20px;
  }
  .all-clear h2 {
    font-size: 1.125rem;
  }
}
</style>
