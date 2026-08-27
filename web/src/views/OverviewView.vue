<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { papi, oapi } from '@/api'
import { ApiError } from '@/api/client'
import type { Overview, Fleet } from '@/api/types'

const { t } = useI18n()
const authStore = useAuthStore()
const ui = useUiStore()

const loading = ref(true)
const ov = ref<Overview | null>(null)
const fleet = ref<Fleet | null>(null)

const sum = (arr: number[]) => arr.reduce((a, b) => a + b, 0)

const fleetTotals = computed(() => {
  const g = fleet.value?.nodes ?? []
  const i = fleet.value?.infraNodes ?? []
  return {
    gpuUsed: sum(g.map((n) => n.gpuUsed)),
    gpuTotal: sum(g.map((n) => n.gpuTotal)),
    vcpuUsed: sum(g.map((n) => n.simVcpuUsed)),
    vcpuTotal: sum(g.map((n) => n.simVcpuTotal)) + sum(i.map((n) => Number(n.simVcpu) || 0)),
    memUsed: sum(g.map((n) => n.simMemUsed)),
    memTotal: sum(g.map((n) => n.simMemTotal)) + sum(i.map((n) => Number(n.simMemGi) || 0)),
    contracts: sum(g.map((n) => n.activeContracts)),
    alerts: fleet.value?.alerts?.length ?? 0,
  }
})

function quota(k: string) {
  const q = ov.value?.quota?.[k]
  return { used: Number(q?.used ?? 0) || 0, hard: q?.hard ?? '—' }
}

async function load() {
  loading.value = true
  const reqTenant = ui.tenant                 // drop stale responses (tenant race)
  try {
    if (authStore.isAdmin()) {
      const [f, o] = await Promise.all([oapi.fleet(), papi.overview(reqTenant)])
      if (reqTenant !== ui.tenant) return
      fleet.value = f
      ov.value = o
    } else {
      const o = await papi.overview(reqTenant)
      if (reqTenant !== ui.tenant) return
      ov.value = o
    }
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => ui.tenant, load)

const ownerColor: Record<string, string> = {
  ARISE: 'arcoblue', VAST: 'orange', DIRECT: 'green', QUARANTINED: 'red', MAINTENANCE: 'purple', UNKNOWN: 'gray',
}
const nodeColumns = computed(() => [
  { title: t('fleet.node'), dataIndex: 'nodeId', slotName: 'node' },
  { title: t('fleet.pair'), dataIndex: 'pair' },
  { title: t('fleet.owner'), dataIndex: 'owner', slotName: 'owner' },
  { title: 'GPU', dataIndex: 'gpu', slotName: 'gpu' },
  { title: 'vCPU', dataIndex: 'vcpu', slotName: 'vcpu' },
  { title: t('overview.contracts'), dataIndex: 'activeContracts' },
])
</script>

<template>
  <a-spin :loading="loading" style="display: block">
    <!-- admin: fleet-wide -->
    <template v-if="authStore.isAdmin()">
      <a-grid :cols="{ xs: 2, sm: 3, md: 6 }" :col-gap="12" :row-gap="12">
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('overview.gpuAllocated')"
            :value="fleetTotals.gpuUsed" :suffix="`/ ${fleetTotals.gpuTotal}`" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic title="vCPU"
            :value="fleetTotals.vcpuUsed" :suffix="`/ ${fleetTotals.vcpuTotal}`" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('overview.memoryTiB')" :precision="1"
            :value="fleetTotals.memUsed / 1024" :suffix="`/ ${(fleetTotals.memTotal / 1024).toFixed(0)}`" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('overview.nvmeCache')" :value="122.9" :precision="1" suffix="TB" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('overview.contracts')" :value="fleetTotals.contracts" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('nav.alerts')" :value="fleetTotals.alerts"
            :value-style="fleetTotals.alerts ? { color: 'rgb(var(--red-6))' } : {}" /></a-card>
        </a-grid-item>
      </a-grid>

      <a-card :title="t('overview.nodeOwnership')" :bordered="false" style="margin-top: 14px">
        <a-table :columns="nodeColumns" :data="fleet?.nodes ?? []" :pagination="false" row-key="nodeId" size="small">
          <template #node="{ record }"><strong>{{ record.nodeId }}</strong></template>
          <template #owner="{ record }"><a-tag :color="ownerColor[record.owner] || 'gray'">{{ record.owner }}</a-tag></template>
          <template #gpu="{ record }">{{ record.gpuUsed }} / {{ record.gpuTotal }}</template>
          <template #vcpu="{ record }">{{ record.simVcpuUsed }} / {{ record.simVcpuTotal }}</template>
        </a-table>
      </a-card>
    </template>

    <!-- tenant user: quota-scoped -->
    <template v-else>
      <a-grid :cols="{ xs: 2, sm: 3, md: 6 }" :col-gap="12" :row-gap="12">
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('overview.gpuB300')"
            :value="quota('requests.arise.dev/fake-gpu').used" :suffix="`/ ${quota('requests.arise.dev/fake-gpu').hard}`" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic title="vCPU"
            :value="quota('requests.arise.dev/sim-vcpu').used" :suffix="`/ ${quota('requests.arise.dev/sim-vcpu').hard}`" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('overview.memoryGi')"
            :value="quota('requests.arise.dev/sim-mem-gi').used" :suffix="`/ ${quota('requests.arise.dev/sim-mem-gi').hard}`" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('nav.jobs')" :value="(ov?.jobs ?? []).length" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('nav.devMachines')" :value="(ov?.devmachines ?? []).length" /></a-card>
        </a-grid-item>
        <a-grid-item>
          <a-card :bordered="false"><a-statistic :title="t('nav.volumes')" :value="(ov?.volumes ?? []).length" /></a-card>
        </a-grid-item>
      </a-grid>

      <a-card :bordered="false" style="margin-top: 14px"
        :title="`${t('nav.jobs')} · ${ov?.queue ?? ''}`">
        <a-table :data="ov?.jobs ?? []" :pagination="false" row-key="name" size="small"
          :columns="[
            { title: t('common.name'), dataIndex: 'name' },
            { title: t('common.phase'), dataIndex: 'phase', slotName: 'phase' },
            { title: t('overview.running'), dataIndex: 'running', slotName: 'run' },
          ]">
          <template #phase="{ record }">
            <a-tag :color="record.phase === 'Running' ? 'green' : 'orange'">{{ record.phase }}</a-tag>
          </template>
          <template #run="{ record }">{{ record.running }}/{{ record.replicas }}</template>
        </a-table>
      </a-card>
    </template>
  </a-spin>
</template>
