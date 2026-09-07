<script setup lang="ts">
import { selectScrollbar } from "@/utils/accessibility";
import { onUnmounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { papi } from "@/api";
import type { Instance, EventRow } from "@/api/types";
import { formatTime } from "@/utils/clipboard";
import StatusBadge from "./StatusBadge.vue";
import EmptyState from "./EmptyState.vue";
import CodeBlock from "./CodeBlock.vue";
const props = defineProps<{
  ns: string;
  workload: string | null;
  title: string;
}>();
const visible = defineModel<boolean>("visible", { default: false });
const { t, locale } = useI18n();
const tab = ref("instances"),
  instances = ref<Instance[]>([]),
  events = ref<EventRow[]>([]),
  logText = ref(""),
  selectedPod = ref(""),
  loading = ref(false),
  loadError = ref("");
let generation = 0;
async function refresh() {
  const ticket = ++generation;
  loadError.value = "";
  logText.value = "";
  if (!visible.value || !props.workload) {
    loading.value = false;
    return;
  }
  const ns = props.ns,
    workload = props.workload,
    activeTab = tab.value;
  loading.value = true;
  try {
    if (activeTab === "events") {
      const data = await papi.events(ns, workload);
      if (ticket === generation)
        events.value = [...data.events].sort((a, b) =>
          b.at.localeCompare(a.at),
        );
    } else {
      const data = await papi.instances(ns, workload);
      if (ticket !== generation) return;
      instances.value = data.instances;
      if (!data.instances.some((p) => p.name === selectedPod.value))
        selectedPod.value = data.instances[0]?.name ?? "";
      if (activeTab === "logs" && selectedPod.value) {
        const data = await papi.logs(ns, selectedPod.value);
        if (ticket === generation) logText.value = data.log;
      }
    }
  } catch (e) {
    if (ticket === generation)
      loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (ticket === generation) loading.value = false;
  }
}
watch([visible, () => props.ns, () => props.workload], () => {
  instances.value = [];
  events.value = [];
  selectedPod.value = "";
  if (tab.value !== "instances") tab.value = "instances";
  else refresh();
});
watch(tab, refresh);
onUnmounted(() => {
  generation++;
});
</script>
<template>
  <a-drawer
    v-model:visible="visible"
    :title="title"
    :width="760"
    :footer="false"
    unmount-on-close
  >
    <div class="drawer-meta">
      <span>
        <icon-apps />
        {{ ns }}
      </span>
      <a-button size="small" :loading="loading" @click="refresh">
        <template #icon><icon-refresh /></template>
        {{ t("common.refresh") }}
      </a-button>
    </div>
    <a-alert v-if="loadError" type="error" style="margin-bottom: 16px">
      {{ loadError }}
    </a-alert>
    <a-tabs lazy-load destroy-on-hide v-model:active-key="tab">
      <a-tab-pane key="instances" :title="t('drawer.instances')">
        <a-spin :loading="loading" style="display: block">
          <div v-if="instances.length && !loadError" class="instance-list">
            <article
              v-for="instance in instances"
              :key="instance.name"
              class="instance-card"
            >
              <div class="instance-heading">
                <span class="resource-symbol"><icon-desktop /></span>
                <strong>{{ instance.name }}</strong>
                <StatusBadge :status="instance.phase" />
              </div>
              <div class="instance-spec">
                <span>
                  <b>{{ instance.gpu }}</b>
                  GPU
                </span>
                <span>
                  <b>{{ instance.vcpu }}</b>
                  vCPU
                </span>
                <span>
                  <b>{{ instance.memGi }}</b>
                  GiB RAM
                </span>
              </div>
              <div class="instance-info">
                <span>
                  <icon-computer />
                  {{ instance.node || t("console.noNode") }}
                </span>
                <span>
                  {{ t("console.started") }}
                  {{ formatTime(instance.started, locale) }}
                </span>
              </div>
            </article>
          </div>
          <EmptyState
            v-else-if="!loading && !loadError"
            :title="t('console.noInstances')"
            :description="t('console.noInstancesDesc')"
            icon="icon-schedule"
          >
            <a-button @click="tab = 'events'">
              {{ t("console.events") }}
              <icon-arrow-right />
            </a-button>
          </EmptyState>
        </a-spin>
      </a-tab-pane>
      <a-tab-pane key="logs" :title="t('drawer.logs')">
        <div class="logs-toolbar">
          <a-select
            :scrollbar="selectScrollbar"
            v-model="selectedPod"
            :options="instances.map((i) => ({ label: i.name, value: i.name }))"
            :placeholder="t('console.chooseInstance')"
            :aria-label="t('console.chooseInstance')"
            :disabled="loading || !instances.length"
            @change="refresh"
          />
          <p>{{ t("console.logsHelp") }}</p>
        </div>
        <a-spin :loading="loading" style="display: block">
          <CodeBlock
            v-if="logText && !loadError"
            :code="logText"
            :label="selectedPod"
          />
          <EmptyState
            v-else-if="!loading && !loadError"
            :title="
              t(instances.length ? 'console.noLogs' : 'console.noInstances')
            "
            :description="
              t(
                instances.length
                  ? 'console.logsHelp'
                  : 'console.noInstancesDesc',
              )
            "
            icon="icon-code"
          />
        </a-spin>
      </a-tab-pane>
      <a-tab-pane key="events" :title="t('drawer.events')">
        <a-spin :loading="loading" style="display: block">
          <div v-if="events.length && !loadError" class="event-timeline">
            <article
              v-for="(event, i) in events"
              :key="i"
              :class="{ warning: event.type === 'Warning' }"
            >
              <span class="event-dot" />
              <div class="event-heading">
                <strong>{{ event.reason }}</strong>
                <time :title="event.at">
                  {{ formatTime(event.at, locale) }}
                </time>
              </div>
              <p>{{ event.message }}</p>
              <span class="event-type">{{ event.type }}</span>
            </article>
          </div>
          <EmptyState
            v-else-if="!loading && !loadError"
            :title="t('console.noEvents')"
            :description="t('console.noEventsDesc')"
            icon="icon-history"
          />
        </a-spin>
      </a-tab-pane>
    </a-tabs>
  </a-drawer>
</template>
<style scoped>
.drawer-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.drawer-meta > span {
  font-size: var(--text-sm);
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 8px;
}
.instance-list {
  display: grid;
  gap: 14px;
  padding: 8px 0;
}
.instance-card {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 20px;
}
.instance-heading {
  display: flex;
  align-items: center;
  gap: 12px;
}
.instance-heading strong {
  flex: 1;
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
  min-width: 0;
}
.instance-spec {
  display: flex;
  gap: 22px;
  background: var(--surface-soft);
  border-radius: 7px;
  padding: 15px;
  margin-top: 20px;
  font-size: var(--text-xs);
  color: var(--muted);
}
.instance-spec b {
  font-weight: 600;
  font-size: var(--text-lg);
  color: var(--ink);
  margin-right: 5px;
}
.instance-info {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 16px;
  font-size: var(--text-xs);
  color: var(--muted);
}
.instance-info > span {
  display: flex;
  align-items: center;
  gap: 7px;
}
.logs-toolbar {
  padding: 8px 0 14px;
}
.logs-toolbar p {
  font-size: var(--text-xs);
  color: var(--muted);
  margin: 12px 0 0;
}
.event-timeline {
  padding: 10px 0 0 9px;
}
.event-timeline article {
  position: relative;
  border-left: 1px solid var(--line);
  padding: 0 0 26px 22px;
}
.event-dot {
  position: absolute;
  left: -4px;
  top: 4px;
  width: 7px;
  height: 7px;
  background: var(--accent);
  border: 2px solid var(--surface);
  box-shadow: 0 0 0 1px var(--accent);
  border-radius: 50%;
}
.warning .event-dot {
  background: var(--warning);
  box-shadow: 0 0 0 1px var(--warning);
}
.event-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}
.event-heading strong {
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}
.event-heading time {
  font-size: var(--text-xs);
  color: var(--muted);
  white-space: nowrap;
}
.event-timeline p {
  font-size: var(--text-sm);
  color: var(--muted);
  line-height: 1.9;
  margin: 10px 0;
  overflow-wrap: anywhere;
}
.event-type {
  font-size: var(--text-xs);
  color: var(--muted);
  background: var(--surface-soft);
  padding: 3px 6px;
  border-radius: 4px;
}
@media (max-width: 600px) {
  .instance-card {
    padding: 14px;
  }
  .instance-spec {
    gap: 13px;
    padding: 12px;
  }
  .instance-info {
    flex-direction: column;
  }
  .instance-spec b {
    font-size: var(--text-base);
  }
  .instance-heading .resource-symbol {
    display: none;
  }
  .event-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }
}
</style>
