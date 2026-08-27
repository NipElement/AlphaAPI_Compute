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

const form = reactive({
  name: '',
  image: '',
  res: { vcpu: 4, memGi: 16, gpu: 0 },
  volGi: 20,
  volClass: 'arise-longterm',
  sshKey: '',
})
const rotate = reactive({ visible: false, name: '', key: '', busy: false })

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
    rows.value = data.devmachines
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function create() {
  if (!form.name.trim()) { Message.warning(t('workbench.nameRequired')); return }
  creating.value = true
  try {
    const body: Record<string, unknown> = {
      name: form.name.trim(), image: form.image, ...form.res,
    }
    if (form.volGi > 0) body.volume = { sizeGi: form.volGi, class: form.volClass }
    if (form.sshKey.trim()) body.sshPublicKey = form.sshKey.trim()
    const res = await papi.createDevmachine(ui.tenant, body) as { ssh?: { service: string; port: number; user: string } | null }
    Message.success(res?.ssh
      ? t('workbench.createdSsh', { name: form.name.trim(), endpoint: `${res.ssh.user}@${res.ssh.service}:${res.ssh.port}` })
      : t('workbench.created', { name: form.name.trim() }))
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
    await papi.remove(ui.tenant, 'devmachines', name)
    Message.success(t('workbench.deleted', { name }))
    await load()
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  }
}

function openRotate(name: string) {
  rotate.name = name; rotate.key = ''; rotate.visible = true
}

async function doRotate() {
  if (!rotate.key.trim()) { Message.warning(t('workbench.sshKeyRequired')); return }
  rotate.busy = true
  try {
    await papi.rotateDevmachineKey(ui.tenant, rotate.name, rotate.key.trim())
    Message.success(t('workbench.keyRotated', { name: rotate.name }))
    rotate.visible = false
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    rotate.busy = false
  }
}

function openDrawer(name: string) {
  drawer.workload = name
  drawer.title = `${t('nav.devMachines')} · ${name}`
  drawer.visible = true
}

const columns = computed(() => [
  { title: t('common.name'), dataIndex: 'name', slotName: 'name' },
  { title: t('common.status'), dataIndex: 'phase', slotName: 'phase' },
  { title: t('fleet.node'), dataIndex: 'node' },
  { title: t('workbench.sshEndpoint'), dataIndex: 'ssh', slotName: 'ssh' },
  { title: '', dataIndex: 'op', slotName: 'op', width: 170, align: 'right' as const },
])

onMounted(async () => { await loadFlavors(); await load() })
watch(() => ui.tenant, load)
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-card :bordered="false" :title="t('nav.devMachines')">
      <a-form :model="form" layout="vertical">
        <a-row :gutter="16">
          <a-col :span="8">
            <a-form-item :label="t('workbench.name')" required>
              <a-input v-model="form.name" placeholder="dev-1" allow-clear />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item :label="t('workbench.image')">
              <a-select v-model="form.image">
                <a-option v-for="im in flavors?.images || []" :key="im" :value="im">{{ im }}</a-option>
              </a-select>
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item :label="t('workbench.resources')">
          <FlavorPicker v-if="flavors" v-model="form.res" :flavors="flavors.flavors" />
        </a-form-item>
        <a-row :gutter="16">
          <a-col :span="8">
            <a-form-item :label="t('workbench.dataVolume')">
              <a-input-number v-model="form.volGi" :min="0" :max="1000" :step="10" mode="button" />
            </a-form-item>
          </a-col>
          <a-col :span="8">
            <a-form-item :label="t('workbench.volumeClass')">
              <a-select v-model="form.volClass" :disabled="form.volGi === 0">
                <a-option value="arise-longterm">{{ t('workbench.longterm') }}</a-option>
                <a-option value="arise-shared">{{ t('workbench.shared') }}</a-option>
              </a-select>
            </a-form-item>
          </a-col>
        </a-row>
        <a-form-item :label="t('workbench.sshKey')" :help="t('workbench.sshKeyHint')">
          <a-textarea v-model="form.sshKey" :auto-size="{ minRows: 2, maxRows: 3 }" placeholder="ssh-ed25519 AAAA… you@laptop" />
        </a-form-item>
        <a-button type="primary" :loading="creating" @click="create">
          <template #icon><icon-plus /></template>{{ t('workbench.createDev') }}
        </a-button>
      </a-form>
    </a-card>

    <a-card :bordered="false">
      <template #title>{{ t('workbench.devList') }}</template>
      <template #extra>
        <a-button size="small" @click="load"><template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button>
      </template>
      <a-table :columns="columns" :data="rows" :loading="loading" :pagination="false" row-key="name" size="medium"
        :row-class="() => 'rowlink'" @row-click="(r: any) => openDrawer(r.name)">
        <template #name="{ record }"><strong>{{ record.name }}</strong></template>
        <template #phase="{ record }">
          <a-tag :color="record.phase === 'Running' ? 'green' : 'orange'">{{ record.phase }}</a-tag>
        </template>
        <template #ssh="{ record }">
          <code v-if="record.ssh" class="ep">{{ record.ssh.user }}@{{ record.ssh.service }}:{{ record.ssh.port }}</code>
          <span v-else class="hint">{{ t('workbench.noSsh') }}</span>
        </template>
        <template #op="{ record }">
          <a-button size="mini" type="text" @click.stop="openRotate(record.name)">{{ t('workbench.rotateKey') }}</a-button>
          <a-popconfirm :content="t('workbench.confirmDelete', { name: record.name })" @ok="remove(record.name)">
            <a-button status="danger" size="mini" type="text" @click.stop>{{ t('common.delete') }}</a-button>
          </a-popconfirm>
        </template>
      </a-table>
      <div class="hint">{{ t('workbench.rowHint') }} · {{ t('workbench.sshReachHint') }} · {{ t('workbench.deleteKeepsVolume') }}</div>
    </a-card>

    <WorkloadDrawer v-model:visible="drawer.visible" :ns="ui.tenant" :workload="drawer.workload" :title="drawer.title" />

    <a-modal v-model:visible="rotate.visible" :title="t('workbench.rotateKeyTitle', { name: rotate.name })"
      :ok-loading="rotate.busy" @ok="doRotate">
      <p class="hint">{{ t('workbench.rotateKeyHint') }}</p>
      <a-textarea v-model="rotate.key" :auto-size="{ minRows: 3, maxRows: 5 }" placeholder="ssh-ed25519 AAAA… you@laptop" />
    </a-modal>
  </a-space>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 10px; }
.ep { font-size: 12px; }
:deep(.rowlink) { cursor: pointer; }
</style>
