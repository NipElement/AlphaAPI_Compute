<script setup lang="ts">
import QuotaMeter from "@/components/QuotaMeter.vue";
import DataTable from "@/components/DataTable.vue";
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { papi } from "@/api";
import { quantity, quotaRatio } from "@/utils/quantity";
import { useUiStore } from "@/stores/ui";
import type { Flavors, Overview } from "@/api/types";
import PageHeading from "@/components/PageHeading.vue";
import EmptyState from "@/components/EmptyState.vue";
const { t } = useI18n(),
  ui = useUiStore();
const ov = ref<Overview | null>(null),
  flavors = ref<Flavors | null>(null),
  loadError = ref(""),
  loading = ref(true);
async function load() {
  loading.value = true;
  loadError.value = "";
  const ns = ui.tenant;
  try {
    const [o, f] = await Promise.all([papi.overview(ns), papi.flavors()]);
    if (ns === ui.tenant) {
      ov.value = o;
      flavors.value = f;
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
const rows = computed(() =>
  Object.entries(ov.value?.quota ?? {}).map(([key, v]) => ({ key, ...v })),
);
const cards = computed(() => {
  const all = ov.value?.quota ?? {},
    groups = [
      {
        label: "GPU",
        keys: ["requests.nvidia.com/gpu", "requests.arise.dev/fake-gpu"],
        icon: "icon-thunderbolt",
        unit: "GPU",
      },
      {
        label: "vCPU",
        keys: ["requests.arise.dev/sim-vcpu", "requests.cpu"],
        icon: "icon-desktop",
        unit: "vCPU",
      },
      {
        label: t("overview.memoryGi"),
        keys: ["requests.arise.dev/sim-mem-gi", "requests.memory"],
        icon: "icon-storage",
        unit: "GiB",
      },
      ...Object.keys(all)
        .filter((k) => k.endsWith("storage"))
        .map((key) => ({
          label: key.includes("arise-longterm")
            ? t("console.retained")
            : key.includes("arise-shared")
              ? t("console.disposable")
              : t("console.dataVolume"),
          keys: [key],
          icon: "icon-folder",
          unit: "GiB",
        })),
    ];
  return groups.flatMap((g) => {
    const key = g.keys.find((k) => k in all);
    if (!key) return [];
    const q = all[key],
      divisor =
        key === "requests.memory" || key.endsWith("storage") ? 2 ** 30 : 1;
    return [
      {
        ...g,
        key,
        used: quantity(q.used) / divisor,
        hard: quantity(q.hard) / divisor,
        percent: quotaRatio(q.used, q.hard),
      },
    ];
  });
});
const format = (n: number) =>
  Number.isFinite(n)
    ? Number(n.toFixed(2)).toLocaleString(
        ui.locale === "zh" ? "zh-CN" : "en-US",
      )
    : "—";
</script>
<template>
  <div class="page">
    <PageHeading :title="t('nav.quota')" :description="t('console.quotaDesc')">
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
    <a-spin :loading="loading" style="display: block">
      <template v-if="ov && !loadError">
        <div class="quota-context">
          <span class="resource-symbol"><icon-apps /></span>
          <div>
            <strong>{{ ov.namespace }}</strong>
            <p>{{ t("console.quotaHint") }}</p>
          </div>
          <span class="eyebrow">{{ t("console.usedOf") }}</span>
        </div>
        <div class="quota-grid">
          <article v-for="card in cards" :key="card.key" class="quota-card">
            <div class="quota-card-heading">
              <span>{{ card.label }}</span>
              <component :is="card.icon" />
            </div>
            <div class="quota-card-value">
              <strong>{{ format(card.used) }}</strong>
              <span>/ {{ format(card.hard) }}</span>
            </div>
            <QuotaMeter
              :label="card.label"
              :description="`${format(card.used)} / ${format(card.hard)} ${card.unit}`"
              :percent="card.percent"
              :height="6"
              :color="card.percent >= 0.9 ? 'var(--warning)' : 'var(--accent)'"
            />
            <div class="quota-card-foot">
              <span>
                {{ t("console.remaining") }}
                {{ format(Math.max(0, card.hard - card.used)) }}
                {{ card.unit }}
              </span>
              <strong>{{ (card.percent * 100).toFixed(0) }}%</strong>
            </div>
          </article>
        </div>
        <EmptyState
          v-if="!cards.length"
          :title="t('console.noQuota')"
          compact
        />
        <section class="panel scheduling-panel">
          <div class="panel-heading">
            <h2>{{ t("quota.scheduling") }}</h2>
            <icon-schedule />
          </div>
          <div class="scheduling-grid">
            <div>
              <span>{{ t("console.queue") }}</span>
              <strong>
                <icon-thunderbolt />
                {{ ov.queue }}
              </strong>
            </div>
            <div>
              <span>{{ t("console.priorities") }}</span>
              <div class="priority-list">
                <a-tag
                  v-for="p in flavors?.priorities[ui.tenant] ?? []"
                  :key="p"
                >
                  {{ p }}
                </a-tag>
              </div>
            </div>
          </div>
          <p class="scheduling-note">
            <icon-info-circle />
            {{ t("jobs.gangNote") }}
          </p>
        </section>
        <a-collapse class="quota-advanced" :bordered="false">
          <a-collapse-item
            :header="t('quota.resourceQuota') + ' · ' + t('drawer.detail')"
            key="advanced"
          >
            <DataTable
              :data="rows"
              :pagination="false"
              row-key="key"
              :columns="[
                { title: t('console.resource'), dataIndex: 'key' },
                { title: t('console.allocated'), dataIndex: 'used' },
                { title: t('console.totalCapacity'), dataIndex: 'hard' },
              ]"
            />
            <p v-if="ov.quota['requests.arise.dev/sim-vcpu']" class="hint">
              {{ t("quota.nativeRowsNote") }}
            </p>
          </a-collapse-item>
        </a-collapse>
      </template>
    </a-spin>
  </div>
</template>
<style scoped>
.quota-context {
  display: flex;
  align-items: center;
  gap: 14px;
  margin: 0;
}
.quota-context strong {
  font-size: var(--text-base);
  font-weight: 600;
}
.quota-context p {
  font-size: var(--text-sm);
  line-height: 1.8;
  color: var(--muted);
  margin: 6px 0 0;
}
.quota-context > .eyebrow {
  margin-left: auto;
  font-size: var(--text-xs);
}
.quota-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px;
}
.quota-card {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 24px;
  background: var(--surface);
}
.quota-card-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--muted);
  font-size: var(--text-sm);
}
.quota-card-heading svg {
  color: var(--accent);
  font-size: 1.125rem;
}
.quota-card-value {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 22px 0 25px;
}
.quota-card-value strong {
  font-size: 2rem;
  font-weight: 600;
  letter-spacing: -1px;
}
.quota-card-value > span {
  font-size: var(--text-base);
  color: var(--muted);
}
.quota-card-foot {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: var(--text-xs);
  color: var(--muted);
  margin-top: 13px;
}
.quota-card-foot strong {
  font-weight: 500;
  color: var(--ink);
}
.scheduling-panel {
  margin-top: 0;
}
.scheduling-panel > .panel-heading > svg {
  color: var(--muted);
}
.scheduling-grid {
  display: grid;
  grid-template-columns: 1fr 2fr;
  padding: 0 24px 24px;
  gap: 30px;
}
.scheduling-grid > div > span {
  font-size: var(--text-xs);
  color: var(--muted);
  display: block;
  margin-bottom: 12px;
}
.scheduling-grid strong {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--accent);
}
.priority-list {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.priority-list :deep(.arco-tag) {
  font-size: var(--text-xs);
  background: var(--surface-soft);
  border: 1px solid var(--line);
  color: var(--ink);
}
.scheduling-note {
  margin: 0;
  padding: 17px 24px;
  border-top: 1px solid var(--line);
  font-size: var(--text-xs);
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 10px;
}
.quota-advanced {
  margin-top: 0;
  background: transparent !important;
}
.quota-advanced :deep(.arco-collapse-item-header) {
  background: transparent;
  font-size: var(--text-sm);
  color: var(--muted);
}
@media (max-width: 1000px) {
  .quota-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 600px) {
  .quota-grid {
    grid-template-columns: 1fr;
    gap: 14px;
  }
  .quota-context > .eyebrow {
    display: none;
  }
  .scheduling-grid {
    grid-template-columns: 1fr;
    gap: 22px;
  }
  .quota-card {
    padding: 21px;
  }
  .quota-card-value {
    margin: 17px 0 21px;
  }
  .quota-card-value strong {
    font-size: 1.75rem;
  }
  .scheduling-note {
    align-items: flex-start;
    line-height: 1.8;
  }
  .scheduling-note svg {
    flex: none;
    margin-top: 3px;
  }
}
</style>
