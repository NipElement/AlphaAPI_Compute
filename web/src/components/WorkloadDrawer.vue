<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { papi } from '@/api'
import type { Instance, EventRow } from '@/api/types'

const props = defineProps<{ ns: string; workload: string | null; title: string }>()
const visible = defineModel<boolean>('visible', { default: false })

const { t } = useI18n()
const tab = ref('instances')
const instances = ref<Instance[]>([])
const events = ref<EventRow[]>([])
const logText = ref('')
const loading = ref(false)

async function loadInstances() {
  if (!props.workload) return
  instances.value = (await papi.instances(props.ns, props.workload)).instances
}
async function loadEvents() {
  if (!props.workload) return
  events.value = (await papi.events(props.ns, props.workload)).events
}
async function loadLogs() {
  if (!props.workload) return
  const inst = instances.value[0] || (await papi.instances(props.ns, props.workload)).instances[0]
  if (!inst) { logText.value = t('drawer.noInstance'); return }
  logText.value = (await papi.logs(props.ns, inst.name)).log || t('common.empty')
}

async function refresh() {
  loading.value = true
  try {
    if (tab.value === 'instances') await loadInstances()
    else if (tab.value === 'events') await loadEvents()
    else if (tab.value === 'logs') { await loadInstances(); await loadLogs() }
  } finally {
    loading.value = false
  }
}

watch([visible, tab], () => { if (visible.value) refresh() })

const instColumns = [
  { title: t('drawer.instance'), dataIndex: 'name' },
  { title: t('common.phase'), dataIndex: 'phase', slotName: 'phase' },
  { title: t('fleet.node'), dataIndex: 'node' },
  { title: t('drawer.spec'), dataIndex: 'spec', slotName: 'spec' },
]
const evtColumns = [
  { title: t('drawer.type'), dataIndex: 'type', slotName: 'type' },
  { title: t('drawer.reason'), dataIndex: 'reason' },
  { title: t('drawer.detail'), dataIndex: 'message' },
]
</script>

<template>
  <a-drawer v-model:visible="visible" :title="title" :width="620" :footer="false" unmount-on-close>
    <a-tabs v-model:active-key="tab">
      <a-tab-pane key="instances" :title="t('drawer.instances')">
        <a-spin :loading="loading" style="display: block">
          <a-table :columns="instColumns" :data="instances" :pagination="false" row-key="name" size="small">
            <template #phase="{ record }">
              <a-tag :color="record.phase === 'Running' ? 'green' : record.phase === 'Succeeded' ? 'gray' : 'orange'">
                {{ record.phase }}
              </a-tag>
            </template>
            <template #spec="{ record }">
              {{ record.gpu }}×GPU · {{ record.vcpu }} vCPU · {{ record.memGi }} GiB
            </template>
          </a-table>
        </a-spin>
      </a-tab-pane>
      <a-tab-pane key="logs" :title="t('drawer.logs')">
        <a-spin :loading="loading" style="display: block; width: 100%">
          <pre class="log">{{ logText }}</pre>
        </a-spin>
      </a-tab-pane>
      <a-tab-pane key="events" :title="t('drawer.events')">
        <a-spin :loading="loading" style="display: block">
          <a-table :columns="evtColumns" :data="events.map((e, i) => ({ ...e, _k: i }))"
            :pagination="false" row-key="_k" size="small">
            <template #type="{ record }">
              <a-tag :color="record.type === 'Warning' ? 'orange' : 'gray'">{{ record.type }}</a-tag>
            </template>
          </a-table>
        </a-spin>
      </a-tab-pane>
    </a-tabs>
  </a-drawer>
</template>

<style scoped>
.log {
  margin: 0; padding: 12px; border-radius: 8px; max-height: 60vh; overflow: auto;
  background: var(--color-fill-2); color: var(--color-text-1);
  font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap;
}
</style>
