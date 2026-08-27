<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { papi } from '@/api'
import { ApiError } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import type { Usage } from '@/api/types'

const { t } = useI18n()
const ui = useUiStore()
const u = ref<Usage | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  const reqTenant = ui.tenant
  try {
    const d = await papi.usage(reqTenant)
    if (reqTenant !== ui.tenant) return
    u.value = d
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally { loading.value = false }
}
const columns = computed(() => [
  { title: t('common.name'), dataIndex: 'name' },
  { title: t('usage.kind'), dataIndex: 'kind' },
  { title: t('usage.openedAt'), dataIndex: 'opened_at' },
  { title: t('usage.closedAt'), dataIndex: 'closed_at', slotName: 'closed' },
  { title: t('usage.hours'), dataIndex: 'seconds', slotName: 'hours', align: 'right' as const },
  { title: 'GPU', dataIndex: 'gpu', align: 'right' as const },
  { title: 'GiB', dataIndex: 'storage_gib', align: 'right' as const },
])
onMounted(load)
watch(() => ui.tenant, load)
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-card :bordered="false" :title="t('nav.usage')">
      <template #extra><a-button size="small" @click="load"><template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button></template>
      <a-row :gutter="16" v-if="u">
        <a-col :span="6"><a-statistic :title="t('usage.gpuHours')" :value="u.gpu_hours" :precision="2" /></a-col>
        <a-col :span="6"><a-statistic :title="t('usage.gpuNow')" :value="u.gpu_allocated_now" /></a-col>
        <a-col :span="6"><a-statistic :title="t('usage.storageGibHours')" :value="u.storage_gib_hours" :precision="1" /></a-col>
        <a-col :span="6"><a-statistic :title="t('usage.storageNow')" :value="u.storage_gib_now" /></a-col>
      </a-row>
      <div class="hint">{{ t('usage.note') }}<span v-if="u"> · {{ t('usage.asOf') }} {{ u.as_of }}</span></div>
    </a-card>
    <a-card :bordered="false" :title="t('usage.intervals')">
      <a-table :columns="columns" :data="u?.intervals || []" :loading="loading" :pagination="{ pageSize: 20 }" :row-key="(r: any) => `${r.name}|${r.opened_at}|${r.closed_at}`" size="small">
        <template #closed="{ record }">
          <a-tag v-if="record.open" color="green">{{ t('usage.open') }}</a-tag><span v-else>{{ record.closed_at }}</span>
        </template>
        <template #hours="{ record }">{{ (record.seconds / 3600).toFixed(2) }}</template>
      </a-table>
    </a-card>
  </a-space>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 12px; }
</style>
