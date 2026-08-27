<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { oapi } from '@/api'
import { ApiError } from '@/api/client'
import type { Fleet, FleetNode } from '@/api/types'

const { t } = useI18n()
const fleet = ref<Fleet | null>(null)
const loading = ref(false)
const submitting = ref(false)

const form = reactive({ nodeId: '', target: 'ARISE', approver: '' })
const detail = reactive({ visible: false, node: null as FleetNode | null, tab: 'spec' })

const ownerColor: Record<string, string> = {
  ARISE: 'arcoblue', VAST: 'orange', DIRECT: 'green', QUARANTINED: 'red', MAINTENANCE: 'purple', UNKNOWN: 'gray',
}

async function load() {
  loading.value = true
  try {
    fleet.value = await oapi.fleet()
    if (!form.nodeId && fleet.value.nodes.length) form.nodeId = fleet.value.nodes[0].nodeId
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

const selectedNode = computed(() => fleet.value?.nodes.find((n) => n.nodeId === form.nodeId) || null)
const gate = computed(() => selectedNode.value?.gates?.[form.target] || null)

async function submit() {
  submitting.value = true
  try {
    const r = await oapi.transition({
      nodeId: form.nodeId, desiredOwner: form.target, approvedBy: form.approver,
    })
    Message.success(t('fleetOps.accepted', { id: r.transitionId }))
    await load()
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    submitting.value = false
  }
}

function openDetail(node: FleetNode) {
  detail.node = node
  detail.tab = 'spec'
  detail.visible = true
}

const nodeColumns = computed(() => [
  { title: t('fleet.node'), dataIndex: 'nodeId', slotName: 'node' },
  { title: t('fleet.pair'), dataIndex: 'pair' },
  { title: t('fleet.owner'), dataIndex: 'owner', slotName: 'owner' },
  { title: t('common.phase'), dataIndex: 'phase' },
  { title: 'GPU', dataIndex: 'gpu', slotName: 'gpu' },
  { title: 'vCPU', dataIndex: 'vcpu', slotName: 'vcpu' },
  { title: t('fleetOps.contracts'), dataIndex: 'activeContracts', slotName: 'contracts' },
  { title: t('fleetOps.flags'), dataIndex: 'flags', slotName: 'flags' },
])
const infraColumns = computed(() => [
  { title: t('fleet.node'), dataIndex: 'name' },
  { title: t('fleetOps.role'), dataIndex: 'role' },
  { title: 'vCPU', dataIndex: 'simVcpu' },
  { title: t('overview.memoryGi'), dataIndex: 'simMemGi' },
  { title: t('common.status'), dataIndex: 'ready', slotName: 'ready' },
])
const specRows = computed(() => {
  const h = detail.node?.hw || {}
  return [
    ['GPU', h.gpus], ['CPU', h.cpus], [t('overview.memoryGi'), h.memory],
    [t('fleetOps.storage'), h.storage], [t('fleetOps.network'), h.network],
    [t('fleetOps.interconnect'), h.interconnect], [t('fleetOps.power'), h.power],
  ].filter(([, v]) => v)
})
onMounted(load)
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-card :bordered="false" :loading="loading">
      <template #title>{{ t('fleetOps.gpuFleet') }}</template>
      <template #extra>
        <a-space>
          <a-tag v-if="fleet" :color="fleet.adapter.productionEnabled ? 'red' : 'gray'">
            VAST: {{ fleet.adapter.mode }}{{ fleet.adapter.productionEnabled ? '' : ' · mock' }}
          </a-tag>
          <a-button size="small" @click="load"><template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button>
        </a-space>
      </template>
      <a-table :columns="nodeColumns" :data="fleet?.nodes || []" :pagination="false" row-key="nodeId"
        size="medium" :row-class="() => 'rowlink'" @row-click="(r: any) => openDetail(r)">
        <template #node="{ record }"><strong>{{ record.nodeId }}</strong></template>
        <template #owner="{ record }"><a-tag :color="ownerColor[record.owner] || 'gray'">{{ record.owner }}</a-tag></template>
        <template #gpu="{ record }">{{ record.gpuUsed }}/{{ record.gpuTotal }}</template>
        <template #vcpu="{ record }">{{ record.simVcpuUsed }}/{{ record.simVcpuTotal }}</template>
        <template #contracts="{ record }">{{ record.activeContracts || '—' }}</template>
        <template #flags="{ record }">
          <a-space>
            <a-tag v-if="!record.ready" color="red" size="small">NotReady</a-tag>
            <a-tag v-if="record.cordoned" color="orange" size="small">cordoned</a-tag>
            <a-tag v-if="record.listed" color="orange" size="small">listed</a-tag>
            <span v-if="record.ready && !record.cordoned && !record.listed" class="muted">—</span>
          </a-space>
        </template>
      </a-table>
      <div class="hint">{{ t('fleetOps.rowHint') }}</div>
    </a-card>

    <a-card :bordered="false" :title="t('fleetOps.infraPool')">
      <a-table :columns="infraColumns" :data="fleet?.infraNodes || []" :pagination="false" row-key="name" size="medium">
        <template #ready="{ record }">
          <a-space>
            <a-tag :color="record.ready ? 'green' : 'red'">{{ record.ready ? 'Ready' : 'NotReady' }}</a-tag>
            <a-tag v-if="record.tainted" color="gray" size="small">{{ t('fleetOps.tenantIsolated') }}</a-tag>
          </a-space>
        </template>
      </a-table>
    </a-card>

    <a-row :gutter="16">
      <a-col :span="12">
        <a-card :bordered="false" :title="t('fleetOps.transitionRequest')">
          <a-form :model="form" layout="vertical">
            <a-form-item :label="t('fleet.node')">
              <a-select v-model="form.nodeId">
                <a-option v-for="n in fleet?.nodes || []" :key="n.nodeId" :value="n.nodeId">{{ n.nodeId }}</a-option>
              </a-select>
            </a-form-item>
            <a-form-item :label="t('fleetOps.targetOwner')">
              <a-radio-group v-model="form.target" type="button">
                <a-radio value="ARISE">ARISE</a-radio>
                <a-radio value="VAST">VAST</a-radio>
                <a-radio value="DIRECT">DIRECT</a-radio>
                <a-radio value="MAINTENANCE">MAINTENANCE</a-radio>
              </a-radio-group>
            </a-form-item>
            <a-form-item :label="t('fleetOps.approver')">
              <a-input v-model="form.approver" placeholder="you@ariselabs.ai" />
            </a-form-item>
            <a-button type="primary" status="warning" :loading="submitting"
              :disabled="!gate?.allowed || form.approver.trim().length < 3" @click="submit">
              {{ t('fleetOps.submitTransition') }}
            </a-button>
          </a-form>
          <a-alert type="warning" style="margin-top: 12px">{{ t('fleetOps.desiredOnly') }}</a-alert>
        </a-card>
      </a-col>
      <a-col :span="12">
        <a-card :bordered="false" :title="t('fleetOps.gateEval')" style="height: 100%">
          <div v-if="gate">
            <div v-for="(r, i) in gate.reasons" :key="i" class="reason" :class="{ blocked: r.startsWith('BLOCKED') }">
              {{ r }}
            </div>
          </div>
          <a-empty v-else />
        </a-card>
      </a-col>
    </a-row>

    <a-drawer v-model:visible="detail.visible" :width="560" :footer="false" unmount-on-close
      :title="detail.node ? `${t('fleet.node')} · ${detail.node.nodeId}` : ''">
      <a-tabs v-model:active-key="detail.tab">
        <a-tab-pane key="spec" :title="t('fleetOps.hwSpec')">
          <a-descriptions :column="1" bordered size="medium">
            <a-descriptions-item v-for="[k, v] in specRows" :key="k" :label="String(k)">{{ v }}</a-descriptions-item>
            <a-descriptions-item :label="t('fleet.owner')">
              <a-tag :color="ownerColor[detail.node?.owner || 'UNKNOWN']">{{ detail.node?.owner }}</a-tag>
              ({{ t('common.phase') }} {{ detail.node?.phase }})
            </a-descriptions-item>
            <a-descriptions-item :label="t('fleetOps.utilization')">
              GPU {{ detail.node?.gpuUsed }}/{{ detail.node?.gpuTotal }} ·
              vCPU {{ detail.node?.simVcpuUsed }}/{{ detail.node?.simVcpuTotal }} ·
              {{ t('overview.memoryGi') }} {{ detail.node?.simMemUsed }}/{{ detail.node?.simMemTotal }}
            </a-descriptions-item>
          </a-descriptions>
          <div class="hint">{{ t('fleetOps.specSource') }}</div>
        </a-tab-pane>
        <a-tab-pane key="gates" :title="t('fleetOps.gates')">
          <div v-for="tgt in ['ARISE', 'VAST', 'DIRECT', 'MAINTENANCE']" :key="tgt" style="margin-bottom: 14px">
            <strong>→ {{ tgt }}</strong>
            <div v-for="(r, i) in detail.node?.gates?.[tgt]?.reasons || []" :key="i"
              class="reason" :class="{ blocked: r.startsWith('BLOCKED') }">{{ r }}</div>
          </div>
        </a-tab-pane>
      </a-tabs>
    </a-drawer>
  </a-space>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 10px; }
.muted { color: var(--color-text-3); }
.reason { font-size: 13px; color: var(--color-text-2); padding: 3px 0; }
.reason.blocked { color: rgb(var(--red-6)); font-weight: 600; }
:deep(.rowlink) { cursor: pointer; }
</style>
