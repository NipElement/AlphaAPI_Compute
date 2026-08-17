<script setup lang="ts">
import { onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { metrics } from '@/api'
import LineChart from '@/components/LineChart.vue'

const { t } = useI18n()

const gpuSeries = ref<Array<[number, number]>>([])
const contractSeries = ref<Array<[number, number]>>([])
const capacity = ref<Array<{ node: string; value: string }>>([])
let timer: number | undefined

async function series(q: string): Promise<Array<[number, number]>> {
  const end = Math.floor(Date.now() / 1000)
  const start = end - 3 * 3600
  const r = await metrics.queryRange(q, start, end, 120)
  const s = r.data.result[0]
  return s ? s.values.map((v) => [v[0], Number(v[1])]) : []
}

async function load() {
  try {
    const [g, c, cap] = await Promise.all([
      series('sum(arise_fake_gpu_healthy)'),
      series('sum(arise_active_contracts)'),
      metrics.query('arise_fake_gpu_capacity'),
    ])
    gpuSeries.value = g
    contractSeries.value = c
    capacity.value = cap.data.result
      .map((r) => ({ node: r.metric.node, value: r.value[1] }))
      .sort((a, b) => (a.node < b.node ? -1 : 1))
  } catch {
    /* transient prometheus hiccup — keep the last series, retry next tick */
  }
}

function startPolling() {
  stopPolling()
  timer = window.setInterval(load, 20000)
}
function stopPolling() {
  if (timer) { clearInterval(timer); timer = undefined }
}
// Under <keep-alive>, navigating away fires onDeactivated (NOT onUnmounted), so
// the poll must stop there or it runs forever in the background. onActivated
// restarts it (and refreshes) when the cached view is shown again.
onMounted(() => { load(); startPolling() })
onActivated(() => { load(); startPolling() })
onDeactivated(stopPolling)
onUnmounted(stopPolling)
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-row :gutter="16">
      <a-col :span="12">
        <a-card :bordered="false" :title="t('monitoring.gpuHealthy3h')">
          <LineChart :points="gpuSeries" unit=" GPU" :ymax="36" />
        </a-card>
      </a-col>
      <a-col :span="12">
        <a-card :bordered="false" :title="t('monitoring.contracts3h')">
          <LineChart :points="contractSeries" color="rgb(var(--green-6))" />
        </a-card>
      </a-col>
    </a-row>

    <a-card :bordered="false" :title="t('monitoring.perNodeCapacity')">
      <a-table :data="capacity" :pagination="false" row-key="node" size="medium"
        :columns="[
          { title: t('fleet.node'), dataIndex: 'node' },
          { title: t('volumes.capacity'), dataIndex: 'value', slotName: 'cap' },
        ]">
        <template #cap="{ record }"><strong>{{ record.value }}</strong> / 8 × B300</template>
      </a-table>
      <div class="hint">{{ t('monitoring.dcgmNote') }}</div>
    </a-card>
  </a-space>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 10px; line-height: 1.6; }
</style>
