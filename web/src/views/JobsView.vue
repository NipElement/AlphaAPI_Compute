<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { papi } from '@/api'
import { ApiError } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import type { Flavors, Overview } from '@/api/types'
import FlavorPicker from '@/components/FlavorPicker.vue'
import WorkloadDrawer from '@/components/WorkloadDrawer.vue'

const { t } = useI18n()
const ui = useUiStore()

const flavors = ref<Flavors | null>(null)
const ov = ref<Overview | null>(null)
const loading = ref(false)
const creating = ref(false)
const drawer = reactive({ visible: false, workload: null as string | null, title: '' })

const FRAMEWORKS = ['custom', 'pytorch-ddp', 'mpi', 'tensorflow-ps']
const form = reactive({
  name: '', framework: 'custom', priority: '', replicas: 2, image: '',
  script: 'import time; time.sleep(3600)', res: { vcpu: 32, memGi: 256, gpu: 1 },
})

const priorities = computed(() => flavors.value?.priorities[ui.tenant] || [])

async function loadFlavors() {
  flavors.value = await papi.flavors()
  if (flavors.value.images.length) form.image = flavors.value.images[0]
  if (priorities.value.length) form.priority = priorities.value[0]
}
async function load() {
  loading.value = true
  const reqTenant = ui.tenant                 // drop stale responses (tenant race)
  try {
    const o = await papi.overview(reqTenant)
    if (reqTenant !== ui.tenant) return
    ov.value = o
    if (!form.priority && priorities.value.length) form.priority = priorities.value[0]
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function submit() {
  if (!form.name.trim()) { Message.warning(t('workbench.nameRequired')); return }
  creating.value = true
  try {
    const r = await papi.createJob(ui.tenant, {
      name: form.name.trim(), replicas: form.replicas, ...form.res,
      image: form.image, script: form.script, framework: form.framework, priority: form.priority,
    }) as { created: string; gang: number; queue: string }
    Message.success(t('jobs.submitted', { name: r.created, gang: r.gang, queue: r.queue }))
    form.name = ''
    await load()
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    creating.value = false
  }
}

async function remove(name: string) {
  try {
    await papi.remove(ui.tenant, 'jobs', name)
    Message.success(t('workbench.deleted', { name }))
    await load()
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  }
}

function openDrawer(name: string) {
  drawer.workload = name
  drawer.title = `${t('nav.jobs')} · ${name}`
  drawer.visible = true
}

const columns = computed(() => [
  { title: t('common.name'), dataIndex: 'name', slotName: 'name' },
  { title: t('common.phase'), dataIndex: 'phase', slotName: 'phase' },
  { title: t('overview.running'), dataIndex: 'running', slotName: 'run' },
  { title: t('nav.quota'), dataIndex: 'queue' },
  { title: '', dataIndex: 'op', slotName: 'op', width: 90, align: 'right' as const },
])

onMounted(async () => { await loadFlavors(); await load() })
watch(() => ui.tenant, async () => { await loadFlavors(); await load() })
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-card :bordered="false" :title="t('nav.jobs')">
      <a-alert style="margin-bottom: 16px">{{ t('jobs.gangNote') }}</a-alert>
      <a-form :model="form" layout="vertical">
        <a-row :gutter="16">
          <a-col :span="6"><a-form-item :label="t('jobs.jobName')" required>
            <a-input v-model="form.name" placeholder="train-1" allow-clear /></a-form-item></a-col>
          <a-col :span="6"><a-form-item :label="t('jobs.framework')">
            <a-select v-model="form.framework">
              <a-option v-for="f in FRAMEWORKS" :key="f" :value="f">{{ f }}</a-option>
            </a-select></a-form-item></a-col>
          <a-col :span="6"><a-form-item :label="t('jobs.priority')">
            <a-select v-model="form.priority">
              <a-option v-for="p in priorities" :key="p" :value="p">{{ p }}</a-option>
            </a-select></a-form-item></a-col>
          <a-col :span="6"><a-form-item :label="t('jobs.replicas')">
            <a-input-number v-model="form.replicas" :min="1" :max="8" mode="button" /></a-form-item></a-col>
        </a-row>
        <a-row :gutter="16">
          <a-col :span="6"><a-form-item :label="t('workbench.image')">
            <a-select v-model="form.image">
              <a-option v-for="im in flavors?.images || []" :key="im" :value="im">{{ im }}</a-option>
            </a-select></a-form-item></a-col>
          <a-col :span="18"><a-form-item :label="t('jobs.entrypoint')">
            <a-textarea v-model="form.script" :auto-size="{ minRows: 1, maxRows: 4 }" /></a-form-item></a-col>
        </a-row>
        <a-form-item :label="t('workbench.resources')">
          <FlavorPicker v-if="flavors" v-model="form.res" :flavors="flavors.flavors" gpu-only />
        </a-form-item>
        <a-button type="primary" :loading="creating" @click="submit">
          <template #icon><icon-thunderbolt /></template>{{ t('jobs.submit') }}
        </a-button>
      </a-form>
    </a-card>

    <a-card :bordered="false">
      <template #title>{{ t('jobs.jobList') }} · {{ ov?.queue }}</template>
      <template #extra><a-button size="small" @click="load">
        <template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button></template>
      <a-table :columns="columns" :data="ov?.jobs || []" :loading="loading" :pagination="false"
        row-key="name" size="medium" :row-class="() => 'rowlink'" @row-click="(r: any) => openDrawer(r.name)">
        <template #name="{ record }"><strong>{{ record.name }}</strong></template>
        <template #phase="{ record }">
          <a-tag :color="record.phase === 'Running' ? 'green' : record.phase === 'Completed' ? 'gray' : 'orange'">{{ record.phase }}</a-tag>
        </template>
        <template #run="{ record }">{{ record.running }}/{{ record.replicas }}</template>
        <template #op="{ record }">
          <a-popconfirm :content="t('workbench.confirmDelete', { name: record.name })" @ok="remove(record.name)">
            <a-button status="danger" size="mini" type="text" @click.stop>{{ t('common.delete') }}</a-button>
          </a-popconfirm>
        </template>
      </a-table>
      <div class="hint">{{ t('workbench.rowHint') }}</div>
    </a-card>

    <WorkloadDrawer v-model:visible="drawer.visible" :ns="ui.tenant" :workload="drawer.workload" :title="drawer.title" />
  </a-space>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 10px; }
:deep(.rowlink) { cursor: pointer; }
</style>
