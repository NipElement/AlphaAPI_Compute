<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { papi } from '@/api'
import { ApiError } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import type { Flavors, WorkloadRow } from '@/api/types'
import FlavorPicker from '@/components/FlavorPicker.vue'
import WorkloadDrawer from '@/components/WorkloadDrawer.vue'

const { t } = useI18n()
const ui = useUiStore()

const flavors = ref<Flavors | null>(null)
const rows = ref<WorkloadRow[]>([])
const loading = ref(false)
const creating = ref(false)
const drawer = reactive({ visible: false, workload: null as string | null, title: '' })
const form = reactive({ name: '', replicas: 1, image: '', res: { vcpu: 4, memGi: 16, gpu: 0 } })

async function loadFlavors() {
  flavors.value = await papi.flavors()
  if (flavors.value.images.length) form.image = flavors.value.images[0]
}
async function load() {
  loading.value = true
  const reqTenant = ui.tenant                 // drop stale responses (tenant race)
  try {
    const data = await papi.overview(reqTenant)
    if (reqTenant !== ui.tenant) return
    rows.value = data.services
  } catch (e) { Message.error(e instanceof ApiError ? e.message : String(e)) }
  finally { loading.value = false }
}
async function deploy() {
  if (!form.name.trim()) { Message.warning(t('workbench.nameRequired')); return }
  creating.value = true
  try {
    const r = await papi.createService(ui.tenant, {
      name: form.name.trim(), replicas: form.replicas, ...form.res, image: form.image,
    }) as { endpoint: string }
    Message.success(t('services.deployed', { endpoint: r.endpoint }))
    form.name = ''
    await load()
  } catch (e) { Message.error(e instanceof ApiError ? e.message : String(e)) }
  finally { creating.value = false }
}
async function remove(name: string) {
  try { await papi.remove(ui.tenant, 'services', name); Message.success(t('workbench.deleted', { name })); await load() }
  catch (e) { Message.error(e instanceof ApiError ? e.message : String(e)) }
}
function openDrawer(name: string) {
  drawer.workload = name; drawer.title = `${t('nav.services')} · ${name}`; drawer.visible = true
}
const columns = computed(() => [
  { title: t('common.name'), dataIndex: 'name', slotName: 'name' },
  { title: t('services.ready'), dataIndex: 'ready', slotName: 'ready' },
  { title: t('services.endpoint'), dataIndex: 'endpoint', slotName: 'endpoint' },
  { title: '', dataIndex: 'op', slotName: 'op', width: 90, align: 'right' as const },
])
onMounted(async () => { await loadFlavors(); await load() })
watch(() => ui.tenant, load)
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-card :bordered="false" :title="t('nav.services')">
      <a-form :model="form" layout="vertical">
        <a-row :gutter="16">
          <a-col :span="8"><a-form-item :label="t('services.serviceName')" required>
            <a-input v-model="form.name" placeholder="infer-1" allow-clear /></a-form-item></a-col>
          <a-col :span="8"><a-form-item :label="t('services.replicas')">
            <a-input-number v-model="form.replicas" :min="1" :max="8" mode="button" /></a-form-item></a-col>
          <a-col :span="8"><a-form-item :label="t('workbench.image')">
            <a-select v-model="form.image">
              <a-option v-for="im in flavors?.images || []" :key="im" :value="im">{{ im }}</a-option>
            </a-select></a-form-item></a-col>
        </a-row>
        <a-form-item :label="t('workbench.resources')">
          <FlavorPicker v-if="flavors" v-model="form.res" :flavors="flavors.flavors" />
        </a-form-item>
        <a-button type="primary" :loading="creating" @click="deploy">
          <template #icon><icon-cloud /></template>{{ t('services.deploy') }}
        </a-button>
      </a-form>
    </a-card>

    <a-card :bordered="false">
      <template #title>{{ t('services.serviceList') }}</template>
      <template #extra><a-button size="small" @click="load">
        <template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button></template>
      <a-table :columns="columns" :data="rows" :loading="loading" :pagination="false" row-key="name"
        size="medium" :row-class="() => 'rowlink'" @row-click="(r: any) => openDrawer(r.name)">
        <template #name="{ record }"><strong>{{ record.name }}</strong></template>
        <template #ready="{ record }">
          <a-tag :color="(record.ready ?? 0) >= (record.replicas ?? 1) ? 'green' : 'orange'">
            {{ record.ready }}/{{ record.replicas }}</a-tag>
        </template>
        <template #endpoint="{ record }"><a-typography-text code>{{ record.endpoint }}</a-typography-text></template>
        <template #op="{ record }">
          <a-popconfirm :content="t('workbench.confirmDelete', { name: record.name })" @ok="remove(record.name)">
            <a-button status="danger" size="mini" type="text" @click.stop>{{ t('common.delete') }}</a-button>
          </a-popconfirm>
        </template>
      </a-table>
      <div class="hint">{{ t('services.hpaNote') }}</div>
    </a-card>

    <WorkloadDrawer v-model:visible="drawer.visible" :ns="ui.tenant" :workload="drawer.workload" :title="drawer.title" />
  </a-space>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 10px; }
:deep(.rowlink) { cursor: pointer; }
</style>
