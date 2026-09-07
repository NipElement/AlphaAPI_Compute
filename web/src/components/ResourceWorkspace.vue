<script setup lang="ts">
import DataTable from "@/components/DataTable.vue";
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { Message } from "@arco-design/web-vue";
import { papi } from "@/api";
import type { Flavors, WorkloadRow } from "@/api/types";
import { useUiStore } from "@/stores/ui";
import { useOverview } from "@/composables/useOverview";
import { quantity } from "@/utils/quantity";
import PageHeading from "./PageHeading.vue";
import EmptyState from "./EmptyState.vue";
import StatusBadge from "./StatusBadge.vue";
import ResourceCreateDrawer from "./ResourceCreateDrawer.vue";
import { WORKLOAD_META, type WorkloadKind } from "@/utils/workloads";
import WorkloadDrawer from "./WorkloadDrawer.vue";
import ConnectionGuide from "./ConnectionGuide.vue";
const props = defineProps<{
  kind: WorkloadKind;
}>();
const { t } = useI18n(),
  ui = useUiStore(),
  route = useRoute(),
  router = useRouter();
const {
  data: overview,
  loading,
  error: loadError,
  load,
} = useOverview(() => ui.tenant);
const meta = computed(() => WORKLOAD_META[props.kind]);
const rows = computed(() => overview.value?.[props.kind] ?? []);
const query = ref(
    typeof route.query.search === "string" ? route.query.search : "",
  ),
  filter = ref("all");
function status(row: WorkloadRow) {
  return props.kind === "services"
    ? Number(row.replicas) > 0 && Number(row.ready) >= Number(row.replicas)
      ? "Ready"
      : "Pending"
    : row.phase;
}
function category(row: WorkloadRow) {
  const phase = status(row);
  return ["Running", "Ready", "Bound"].includes(phase ?? "")
    ? "running"
    : ["Completed", "Succeeded", "Failed", "Error", "Released"].includes(
          phase ?? "",
        )
      ? "finished"
      : "pending";
}
const filtered = computed(() =>
  rows.value.filter(
    (row) =>
      (filter.value === "all" || category(row) === filter.value) &&
      `${row.name} ${row.node ?? ""} ${row.endpoint ?? ""} ${row.class ?? ""}`
        .toLowerCase()
        .includes(query.value.toLowerCase().trim()),
  ),
);
const counts = computed(() => ({
  all: rows.value.length,
  running: rows.value.filter((r) => category(r) === "running").length,
  pending: rows.value.filter((r) => category(r) === "pending").length,
  finished: rows.value.filter((r) => category(r) === "finished").length,
}));
const catalog = ref<Flavors | null>(null),
  catalogError = ref(""),
  catalogLoading = ref(false);
let alive = true;
let poll: number | undefined;
onMounted(() => {
  poll = window.setInterval(() => {
    if (!document.hidden && !loading.value && !loadError.value) load(true);
  }, 10000);
});
onUnmounted(() => {
  alive = false;
  clearInterval(poll);
});
const createOpen = ref(false);
const remainingGpu = computed(() => {
  const entries = overview.value?.quota;
  const q =
    entries?.["requests.nvidia.com/gpu"] ??
    entries?.["requests.arise.dev/fake-gpu"];
  return q ? Math.max(0, quantity(q.hard) - quantity(q.used)) : null;
});
async function loadCatalog() {
  if (props.kind === "volumes") return;
  catalogLoading.value = true;
  catalogError.value = "";
  try {
    const value = await papi.flavors();
    if (!alive) return;
    catalog.value = value;
  } catch (e) {
    if (alive) catalogError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (alive) catalogLoading.value = false;
  }
}
async function refresh() {
  await Promise.allSettled([load(), loadCatalog()]);
}
onMounted(loadCatalog);
watch(
  () => route.query.create,
  (value) => {
    if (value === "1") createOpen.value = true;
  },
  { immediate: true },
);
watch(createOpen, (value) => {
  if (!value && route.query.create) {
    const query = { ...route.query };
    delete query.create;
    delete query.image;
    router.replace({ query });
  }
});
function openCreate() {
  createOpen.value = true;
}
async function created(name: string) {
  query.value = "";
  filter.value = "all";
  Message.success(t("console.created", { name }));
  await load();
}
const selected = ref<WorkloadRow | null>(null),
  detailOpen = ref(false),
  connectOpen = ref(false);
function inspect(row: WorkloadRow) {
  selected.value = row;
  detailOpen.value = true;
}
function connect(row: WorkloadRow) {
  selected.value = row;
  connectOpen.value = true;
}
const deletion = reactive({
  visible: false,
  row: null as WorkloadRow | null,
  error: "",
});
function confirmRemove(row: WorkloadRow) {
  deletion.row = row;
  deletion.error = "";
  deletion.visible = true;
}
async function remove() {
  if (!deletion.row) return false;
  try {
    await papi.remove(ui.tenant, props.kind, deletion.row.name);
    if (alive) {
      Message.success(t("console.deleted", { name: deletion.row.name }));
      await load();
    }
    return true;
  } catch (e) {
    deletion.error = e instanceof Error ? e.message : String(e);
    return false;
  }
}
const rotate = reactive({ visible: false, name: "", key: "", error: "" });
function openRotate(row: WorkloadRow) {
  rotate.name = row.name;
  rotate.key = "";
  rotate.error = "";
  rotate.visible = true;
}
async function doRotate() {
  if (!rotate.key.trim()) {
    rotate.error = t("workbench.sshKeyRequired");
    return false;
  }
  try {
    await papi.rotateDevmachineKey(ui.tenant, rotate.name, rotate.key.trim());
    Message.success(t("workbench.keyRotated", { name: rotate.name }));
    rotate.key = "";
    return true;
  } catch (e) {
    rotate.error = e instanceof Error ? e.message : String(e);
    return false;
  }
}
const columns = computed(() => [
  {
    title: t("common.name"),
    slotName: "name",
    width: props.kind === "services" ? 230 : undefined,
  },
  { title: t("common.status"), slotName: "status", width: 150 },
  ...(props.kind === "devmachines"
    ? [
        { title: t("fleet.node"), dataIndex: "node", slotName: "node" },
        { title: "SSH", slotName: "access" },
      ]
    : props.kind === "jobs"
      ? [
          { title: t("console.instances"), slotName: "replicas" },
          { title: t("console.queue"), dataIndex: "queue" },
        ]
      : props.kind === "services"
        ? [
            { title: t("console.instances"), slotName: "replicas" },
            { title: t("console.privateEndpoint"), slotName: "endpoint" },
          ]
        : [
            { title: t("volumes.capacity"), dataIndex: "size" },
            { title: t("console.volumePolicy"), slotName: "policy" },
          ]),
  {
    title: t("console.actions"),
    slotName: "actions",
    width: 160,
    align: "right" as const,
  },
]);
</script>
<template>
  <div class="page resource-workspace">
    <PageHeading
      :title="t(`nav.${meta.nav}`)"
      :description="t(`console.${meta.desc}`)"
    >
      <template #actions>
        <a-button
          :loading="loading"
          :aria-label="t('common.refresh')"
          @click="refresh"
        >
          <icon-refresh />
        </a-button>
        <a-button
          type="primary"
          data-action="create-resource"
          @click="openCreate"
        >
          <template #icon><icon-plus /></template>
          {{ t(`console.${meta.create}`) }}
        </a-button>
      </template>
    </PageHeading>
    <a-alert v-if="loadError" type="error" class="page-error">
      {{ loadError }}
      <a-button size="mini" :loading="loading" @click="refresh">
        {{ t("common.refresh") }}
      </a-button>
    </a-alert>
    <section class="panel resource-panel">
      <div class="toolbar">
        <div class="filter-tabs">
          <button
            v-for="key in ['all', 'running', 'pending', 'finished'] as const"
            :key="key"
            type="button"
            :class="{ active: filter === key }"
            :aria-pressed="filter === key"
            @click="filter = key"
          >
            {{
              t(
                key === "all"
                  ? "console.allResources"
                  : key === "running" && kind === "volumes"
                    ? "console.available"
                    : `console.${key}`,
              )
            }}
            <span>{{ counts[key] }}</span>
          </button>
        </div>
        <a-input
          v-model="query"
          class="toolbar-search"
          allow-clear
          :placeholder="t('console.searchResources')"
          :input-attrs="{ 'aria-label': t('console.searchResources') }"
        >
          <template #prefix><icon-search /></template>
        </a-input>
      </div>
      <DataTable
        v-if="rows.length || loading"
        :columns="columns"
        :data="filtered"
        :loading="loading"
        :pagination="{ pageSize: 10, hideOnSinglePage: true }"
        row-key="name"
        :bordered="false"
      >
        <template #name="{ record }">
          <div class="resource-name">
            <span class="resource-symbol"><component :is="meta.icon" /></span>
            <div>
              <button
                type="button"
                class="resource-link"
                @click="inspect(record)"
              >
                {{ record.name }}
              </button>
              <span class="resource-sub">{{ ui.tenant }}</span>
            </div>
          </div>
        </template>
        <template #status="{ record }">
          <StatusBadge :status="status(record)" />
        </template>
        <template #node="{ record }">
          <span :class="{ muted: !record.node }">
            {{ record.node || t("console.noNode") }}
          </span>
        </template>
        <template #access="{ record }">
          <span :class="record.ssh ? 'ssh-enabled' : 'muted'">
            <icon-lock v-if="record.ssh" />
            {{ record.ssh ? "SSH" : t("console.noSsh") }}
          </span>
        </template>
        <template #replicas="{ record }">
          <span class="mono">
            {{
              kind === "services" ? (record.ready ?? 0) : (record.running ?? 0)
            }}
            <span class="muted">/ {{ record.replicas ?? 0 }}</span>
          </span>
        </template>
        <template #endpoint="{ record }">
          <code class="endpoint" :title="record.endpoint">
            {{ record.endpoint || "—" }}
          </code>
        </template>
        <template #policy="{ record }">
          <span>
            {{
              t(
                record.class === "arise-longterm"
                  ? "console.retained"
                  : "console.disposable",
              )
            }}
          </span>
          <span class="resource-sub">{{ record.class }}</span>
        </template>
        <template #actions="{ record }">
          <div class="row-actions">
            <a-button
              v-if="
                kind === 'services' || (kind === 'devmachines' && record.ssh)
              "
              size="small"
              @click="connect(record)"
            >
              {{ t("console.connect") }}
              <icon-arrow-rise />
            </a-button>
            <a-button v-else size="small" type="text" @click="inspect(record)">
              {{ t("console.inspect") }}
            </a-button>
            <a-dropdown trigger="click" position="br">
              <a-button
                type="text"
                size="small"
                :aria-label="`${t('console.actions')}: ${record.name}`"
              >
                <icon-more />
              </a-button>
              <template #content>
                <a-doption @click="inspect(record)">
                  {{ t("console.details") }}
                </a-doption>
                <a-doption
                  v-if="kind === 'devmachines'"
                  :disabled="!record.ssh"
                  @click="openRotate(record)"
                >
                  {{ t("console.key") }}
                  <span v-if="!record.ssh">· {{ t("console.noSsh") }}</span>
                </a-doption>
                <a-doption class="danger-option" @click="confirmRemove(record)">
                  <template #icon><icon-delete /></template>
                  {{ t("common.delete") }}
                </a-doption>
              </template>
            </a-dropdown>
          </div>
        </template>
        <template #empty>
          <EmptyState
            :title="t('console.emptyFiltered')"
            :description="t('console.emptyFilteredDesc')"
            compact
          >
            <a-button
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
      <EmptyState
        v-else-if="!loadError"
        :title="t(`console.${meta.empty}`)"
        :description="t(`console.${meta.empty}Desc`)"
        :icon="meta.icon"
      >
        <a-button type="primary" @click="openCreate">
          <template #icon><icon-plus /></template>
          {{ t(`console.${meta.create}`) }}
        </a-button>
      </EmptyState>
      <EmptyState v-else :title="t('common.unavailable')" icon="icon-wifi">
        <a-button @click="refresh">{{ t("common.refresh") }}</a-button>
      </EmptyState>
      <div class="resource-footer">
        <span>
          <component :is="meta.icon" />
          {{ t(`nav.${meta.nav}`) }}
          <span class="mono">{{ filtered.length }}</span>
          / {{ rows.length }}
        </span>
        <span>{{ ui.tenant }}</span>
      </div>
    </section>
    <div class="resource-guide">
      <span class="guide-icon"><icon-info-circle /></span>
      <p>
        {{
          kind === "devmachines"
            ? t("console.storageHint")
            : kind === "jobs"
              ? t("jobs.gangNote")
              : kind === "services"
                ? t("console.internalOnly")
                : t("console.retainedHelp")
        }}
      </p>
      <router-link v-if="kind !== 'volumes'" to="/quota">
        {{ t("nav.quota") }}
        <icon-arrow-right />
      </router-link>
    </div>
    <ResourceCreateDrawer
      v-model:visible="createOpen"
      :kind="kind"
      :catalog="catalog"
      :catalog-loading="catalogLoading"
      :catalog-error="catalogError"
      :remaining-gpu="remainingGpu"
      @retry="loadCatalog"
      @created="created"
    />
    <WorkloadDrawer
      v-if="kind !== 'volumes'"
      v-model:visible="detailOpen"
      :ns="ui.tenant"
      :workload="selected?.name ?? null"
      :title="selected?.name ?? ''"
    />
    <a-drawer
      v-else
      v-model:visible="detailOpen"
      :title="selected?.name"
      :width="500"
      :footer="false"
    >
      <div class="key-value">
        <span>{{ t("common.status") }}</span>
        <StatusBadge :status="selected?.phase" />
      </div>
      <div class="key-value">
        <span>{{ t("volumes.capacity") }}</span>
        <strong>{{ selected?.size }}</strong>
      </div>
      <div class="key-value">
        <span>{{ t("console.volumePolicy") }}</span>
        <strong>{{ selected?.class }}</strong>
      </div>
      <p class="section-note">
        {{
          t(
            selected?.class === "arise-longterm"
              ? "console.retainedHelp"
              : "console.disposableHelp",
          )
        }}
      </p>
      <p v-if="selected?.phase === 'Pending'" class="hint">
        {{ t("volumes.pendingHelp") }}
      </p>
    </a-drawer>
    <ConnectionGuide
      v-model:visible="connectOpen"
      :resource="selected"
      :ns="ui.tenant"
      :kind="kind"
    />
    <a-modal
      v-model:visible="deletion.visible"
      :title="t('console.deleteTitle')"
      :ok-text="t('console.confirmDelete')"
      :ok-button-props="{ status: 'danger' }"
      :on-before-ok="remove"
    >
      <p>{{ t("console.deleteQuestion", { name: deletion.row?.name }) }}</p>
      <p class="hint">
        {{
          t(
            kind === "volumes"
              ? "console.deleteVolumeWarning"
              : "console.deleteWorkload",
          )
        }}
      </p>
      <p v-if="kind === 'devmachines'" class="section-note">
        {{ t("console.keepData") }}
      </p>
      <p v-if="kind === 'volumes'" class="section-note">
        {{
          t(
            deletion.row?.class === "arise-longterm"
              ? "console.retainedHelp"
              : "console.disposableHelp",
          )
        }}
      </p>
      <a-alert v-if="deletion.error" type="error">{{ deletion.error }}</a-alert>
    </a-modal>
    <a-modal
      v-model:visible="rotate.visible"
      :title="t('workbench.rotateKeyTitle', { name: rotate.name })"
      :on-before-ok="doRotate"
      @cancel="rotate.key = ''"
    >
      <p class="hint">{{ t("workbench.rotateKeyHint") }}</p>
      <a-textarea
        v-model="rotate.key"
        placeholder="ssh-ed25519 AAAA…"
        :auto-size="{ minRows: 4 }"
        :textarea-attrs="{ 'aria-label': t('console.sshLabel') }"
      />
      <a-alert v-if="rotate.error" type="error" style="margin-top: 14px">
        {{ rotate.error }}
      </a-alert>
    </a-modal>
  </div>
</template>
<style scoped>
.resource-panel {
  overflow: hidden;
}
.resource-panel .toolbar {
  padding: 18px 22px;
  border-bottom: 1px solid var(--line);
}
.resource-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-top: 1px solid var(--line);
  padding: 14px 22px;
  font-size: var(--text-xs);
  color: var(--muted);
}
.resource-footer > span:first-child {
  display: flex;
  align-items: center;
  gap: 8px;
}
.resource-guide {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 4px;
}
.guide-icon {
  color: var(--muted);
  align-self: flex-start;
  padding-top: 3px;
}
.resource-guide p {
  margin: 0;
  font-size: var(--text-sm);
  line-height: 1.8;
  color: var(--muted);
  flex: 1;
}
.resource-guide a {
  display: flex;
  gap: 8px;
  align-items: center;
  text-decoration: none;
  font-size: var(--text-sm);
  color: var(--accent);
  white-space: nowrap;
}
.endpoint {
  display: block;
  font-size: var(--text-xs);
  color: var(--muted);
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ssh-enabled {
  font-size: var(--text-sm);
  color: var(--muted);
}
.danger-option {
  color: var(--danger);
}
.resource-panel :deep(.arco-table-pagination) {
  padding: 0 20px 16px;
}
@media (max-width: 767px) {
  .resource-panel .toolbar {
    padding: 12px;
    gap: 12px;
  }
  .resource-guide {
    align-items: flex-start;
  }
  .resource-guide a {
    display: none;
  }
  .resource-footer {
    padding: 14px;
  }
}
</style>
