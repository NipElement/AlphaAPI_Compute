<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { papi } from '@/api'
import { ApiError } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import type { WorkloadRow } from '@/api/types'

const { t } = useI18n()
const ui = useUiStore()

const rows = ref<WorkloadRow[]>([])
const loading = ref(false)
const creating = ref(false)
const form = reactive({ name: '', sizeGi: 20, class: 'arise-shared' })

async function load() {
  loading.value = true
  const reqTenant = ui.tenant                 // drop stale responses (tenant race)
  try {
    const data = await papi.overview(reqTenant)
    if (reqTenant !== ui.tenant) return
    rows.value = data.volumes
  } catch (e) { Message.error(e instanceof ApiError ? e.message : String(e)) }
  finally { loading.value = false }
}
async function create() {
  if (!form.name.trim()) { Message.warning(t('workbench.nameRequired')); return }
  creating.value = true
  try {
    await papi.createVolume(ui.tenant, { name: form.name.trim(), sizeGi: form.sizeGi, class: form.class })
    Message.success(t('workbench.created', { name: form.name.trim() }))
    form.name = ''
    await load()
  } catch (e) { Message.error(e instanceof ApiError ? e.message : String(e)) }
  finally { creating.value = false }
}
async function remove(name: string) {
  try { await papi.remove(ui.tenant, 'volumes', name); Message.success(t('workbench.deleted', { name })); await load() }
  catch (e) { Message.error(e instanceof ApiError ? e.message : String(e)) }
}
const columns = computed(() => [
  { title: t('common.name'), dataIndex: 'name', slotName: 'name' },
  { title: t('volumes.class'), dataIndex: 'class' },
  { title: t('volumes.capacity'), dataIndex: 'size' },
  { title: t('common.status'), dataIndex: 'phase', slotName: 'phase' },
  { title: '', dataIndex: 'op', slotName: 'op', width: 90, align: 'right' as const },
])
onMounted(load)
watch(() => ui.tenant, load)
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-card :bordered="false" :title="t('volumes.createVolume')">
      <a-form :model="form" layout="inline">
        <a-form-item :label="t('workbench.name')" required>
          <a-input v-model="form.name" placeholder="data-1" allow-clear style="width: 200px" />
        </a-form-item>
        <a-form-item :label="t('volumes.capacityGi')">
          <a-input-number v-model="form.sizeGi" :min="10" :max="10000" :step="10" mode="button" style="width: 160px" />
        </a-form-item>
        <a-form-item :label="t('volumes.class')">
          <a-select v-model="form.class" style="width: 220px">
            <a-option value="arise-shared">{{ t('volumes.sharedDesc') }}</a-option>
            <a-option value="arise-longterm">{{ t('volumes.longtermDesc') }}</a-option>
          </a-select>
        </a-form-item>
        <a-form-item>
          <a-button type="primary" :loading="creating" @click="create">
            <template #icon><icon-plus /></template>{{ t('common.create') }}
          </a-button>
        </a-form-item>
      </a-form>
    </a-card>

    <a-card :bordered="false">
      <template #title>{{ t('volumes.volumeList') }}</template>
      <template #extra><a-button size="small" @click="load">
        <template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button></template>
      <a-table :columns="columns" :data="rows" :loading="loading" :pagination="false" row-key="name" size="medium">
        <template #name="{ record }"><strong>{{ record.name }}</strong></template>
        <template #phase="{ record }">
          <a-tag :color="record.phase === 'Bound' ? 'green' : 'gray'">{{ record.phase }}</a-tag>
        </template>
        <template #op="{ record }">
          <a-popconfirm :content="t('workbench.confirmDelete', { name: record.name })" @ok="remove(record.name)">
            <a-button status="danger" size="mini" type="text">{{ t('common.delete') }}</a-button>
          </a-popconfirm>
        </template>
      </a-table>
      <div class="hint">{{ t('volumes.note') }}</div>
    </a-card>
  </a-space>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 10px; }
</style>
