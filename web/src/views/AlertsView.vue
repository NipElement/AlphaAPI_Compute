<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { metrics } from '@/api'
import type { AlertRow } from '@/api/types'

const { t } = useI18n()
const alerts = ref<AlertRow[]>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try { alerts.value = await metrics.alerts() }
  catch { alerts.value = [] }
  finally { loading.value = false }
}
onMounted(load)

const columns = computed(() => [
  { title: t('alerts.severity'), dataIndex: 'sev', slotName: 'sev', width: 110 },
  { title: t('alerts.name'), dataIndex: 'name', slotName: 'name' },
  { title: t('alerts.object'), dataIndex: 'node' },
  { title: t('alerts.since'), dataIndex: 'since' },
])
const rows = computed(() =>
  alerts.value.map((a, i) => ({
    _k: i,                                   // same alertname can fire on many nodes
    sev: a.labels.severity || '-',
    name: a.labels.alertname || '-',
    node: a.labels.node || '-',
    since: a.startsAt ? a.startsAt.replace('T', ' ').slice(0, 19) : '-',
  })))
</script>

<template>
  <a-card :bordered="false" :loading="loading">
    <template #title>{{ t('alerts.firing') }}</template>
    <template #extra><a-button size="small" @click="load">
      <template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button></template>
    <a-table :columns="columns" :data="rows" :pagination="false" row-key="_k" size="medium">
      <template #sev="{ record }">
        <a-tag :color="record.sev === 'P0' ? 'red' : 'orange'">{{ record.sev }}</a-tag>
      </template>
      <template #name="{ record }"><strong>{{ record.name }}</strong></template>
      <template #empty><a-empty :description="t('alerts.noAlerts')" /></template>
    </a-table>
  </a-card>
</template>
