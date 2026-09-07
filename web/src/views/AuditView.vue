<script setup lang="ts">
import Pagination from "@/components/Pagination.vue";
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { oapi } from "@/api";
import { useUiStore } from "@/stores/ui";
import type { EventRow } from "@/api/types";
import { formatTime } from "@/utils/clipboard";
import PageHeading from "@/components/PageHeading.vue";
import EmptyState from "@/components/EmptyState.vue";
const { t } = useI18n(),
  ui = useUiStore();
const events = ref<EventRow[]>([]),
  loading = ref(true),
  loadError = ref(""),
  query = ref(""),
  filter = ref("all"),
  page = ref(1);
const rows = computed(() =>
  events.value.filter(
    (e) =>
      (filter.value === "all" || e.type === "Warning") &&
      `${e.reason} ${e.object} ${e.message}`
        .toLowerCase()
        .includes(query.value.toLowerCase().trim()),
  ),
);
watch([query, filter], () => (page.value = 1));
async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const data = await oapi.fleet();
    if (data.errors?.events) throw new Error(t("common.unavailable"));
    events.value = [...data.events].sort((a, b) => b.at.localeCompare(a.at));
    page.value = 1;
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
    events.value = [];
  } finally {
    loading.value = false;
  }
}
onMounted(load);
</script>
<template>
  <div class="page">
    <PageHeading :title="t('nav.audit')" :description="t('console.auditDesc')">
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
    <section class="panel audit-panel">
      <div class="panel-heading">
        <h2>{{ t("audit.trail") }}</h2>
        <span class="muted">{{ t("console.localTime") }}</span>
      </div>
      <div class="toolbar">
        <div class="filter-tabs">
          <button
            type="button"
            :class="{ active: filter === 'all' }"
            :aria-pressed="filter === 'all'"
            @click="filter = 'all'"
          >
            {{ t("console.events") }}
            <span>{{ events.length }}</span>
          </button>
          <button
            type="button"
            :class="{ active: filter === 'warning' }"
            :aria-pressed="filter === 'warning'"
            @click="filter = 'warning'"
          >
            {{ t("console.needsAttention") }}
            <span>{{ events.filter((e) => e.type === "Warning").length }}</span>
          </button>
        </div>
        <a-input
          v-model="query"
          class="toolbar-search"
          allow-clear
          :placeholder="t('console.searchEvents')"
          :input-attrs="{ 'aria-label': t('console.searchEvents') }"
        >
          <template #prefix><icon-search /></template>
        </a-input>
      </div>
      <a-spin :loading="loading" style="display: block">
        <div v-if="rows.length" class="audit-timeline">
          <article
            v-for="(event, i) in rows.slice((page - 1) * 15, page * 15)"
            :key="`${page}:${i}`"
          >
            <time :title="event.at">{{ formatTime(event.at, ui.locale) }}</time>
            <span
              class="audit-dot"
              :class="{ warning: event.type === 'Warning' }"
            >
              <icon-exclamation v-if="event.type === 'Warning'" />
              <icon-check v-else />
            </span>
            <div class="audit-event">
              <div>
                <strong>{{ event.reason }}</strong>
                <span class="event-object">{{ event.object || "—" }}</span>
                <span
                  class="event-type"
                  :class="{ warning: event.type === 'Warning' }"
                >
                  {{ event.type }}
                </span>
              </div>
              <p v-if="event.message">{{ event.message }}</p>
            </div>
          </article>
        </div>
        <EmptyState
          v-else-if="!loading"
          :title="
            loadError
              ? t('common.unavailable')
              : t(
                  query || filter !== 'all'
                    ? 'console.emptyFiltered'
                    : 'console.noEvents',
                )
          "
          :description="
            loadError ||
            t(
              query || filter !== 'all'
                ? 'console.emptyFilteredDesc'
                : 'console.noEventsDesc',
            )
          "
          icon="icon-history"
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
        <div v-else style="min-height: 220px" />
      </a-spin>
      <div class="audit-footer">
        <span>{{ rows.length }} {{ t("console.events") }}</span>
        <Pagination
          v-if="rows.length > 15"
          v-model:current="page"
          :total="rows.length"
          :page-size="15"
          size="small"
        />
      </div>
    </section>
  </div>
</template>
<style scoped>
.audit-panel {
  overflow: hidden;
}
.audit-panel > .panel-heading > .muted {
  font-size: var(--text-xs);
}
.audit-panel > .toolbar {
  padding: 0 24px 22px;
  border-bottom: 1px solid var(--line);
}
.audit-timeline {
  padding: 26px 24px 5px;
}
.audit-timeline article {
  display: grid;
  grid-template-columns: 105px 26px minmax(0, 1fr);
  gap: 16px;
  position: relative;
  padding-bottom: 25px;
}
.audit-timeline article:not(:last-child):after {
  content: "";
  position: absolute;
  left: 133px;
  top: 25px;
  bottom: 0;
  width: 1px;
  background: var(--line);
}
.audit-timeline time {
  font-size: var(--text-xs);
  color: var(--muted);
  padding-top: 5px;
  line-height: 1.8;
}
.audit-dot {
  width: 25px;
  height: 25px;
  border: 1px solid var(--line);
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: var(--text-xs);
  background: var(--surface-soft);
  color: var(--muted);
  z-index: 1;
}
.audit-dot.warning {
  color: var(--warning);
  background: var(--warning-soft);
  border-color: var(--line);
}
.audit-event {
  padding: 3px 0 16px;
  border-bottom: 1px solid var(--line);
  min-width: 0;
}
.audit-event > div {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.audit-event strong {
  font-size: var(--text-sm);
  font-weight: 500;
  overflow-wrap: anywhere;
}
.event-object {
  font-size: var(--text-xs);
  color: var(--accent);
}
.event-type {
  font-size: var(--text-xs);
  color: var(--muted);
  padding: 3px 6px;
  background: var(--surface-soft);
  border-radius: 4px;
  margin-left: auto;
}
.event-type.warning {
  color: var(--warning);
  background: var(--warning-soft);
}
.audit-event p {
  font-size: var(--text-sm);
  line-height: 1.9;
  color: var(--muted);
  margin: 10px 0 0;
  overflow-wrap: anywhere;
}
.audit-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  border-top: 1px solid var(--line);
  padding: 16px 24px;
  font-size: var(--text-xs);
  color: var(--muted);
}
@media (max-width: 767px) {
  .audit-panel > .panel-heading > .muted {
    display: none;
  }
  .audit-panel > .toolbar {
    padding: 0 16px 18px;
  }
  .audit-timeline {
    padding: 20px 16px 0;
  }
  .audit-timeline article {
    grid-template-columns: 24px minmax(0, 1fr);
    gap: 12px;
    padding-top: 24px;
  }
  .audit-timeline time {
    position: absolute;
    left: 36px;
    top: 0;
    font-size: var(--text-xs);
    padding: 0;
  }
  .audit-timeline article:not(:last-child):after {
    left: 12px;
    top: 48px;
  }
  .event-type {
    display: none;
  }
  .audit-footer {
    padding: 16px;
    flex-wrap: wrap;
  }
  .audit-event > div {
    gap: 7px;
  }
}
</style>
