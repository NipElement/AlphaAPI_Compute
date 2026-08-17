<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { oapi } from '@/api'
import type { EventRow } from '@/api/types'

const { t } = useI18n()
const events = ref<EventRow[]>([])
const loading = ref(false)

const rows = computed(() =>
  events.value.map((e, i) => ({ ...e, _k: i })))   // unique key: 'at' collides at second precision

async function load() {
  loading.value = true
  try {
    const f = await oapi.fleet()
    events.value = [...f.events].sort((a, b) => (a.at < b.at ? 1 : -1))
  } catch {
    events.value = []
  } finally {
    loading.value = false
  }
}
onMounted(load)

const columns = computed(() => [
  { title: t('audit.time'), dataIndex: 'at', slotName: 'at', width: 170 },
  { title: t('drawer.type'), dataIndex: 'type', slotName: 'type', width: 100 },
  { title: t('alerts.object'), dataIndex: 'object', width: 120 },
  { title: t('drawer.reason'), dataIndex: 'reason', slotName: 'reason' },
  { title: t('drawer.detail'), dataIndex: 'message' },
])
</script>

<template>
  <a-card :bordered="false" :loading="loading">
    <template #title>{{ t('audit.trail') }}</template>
    <template #extra><a-button size="small" @click="load">
      <template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button></template>
    <a-table :columns="columns" :data="rows" :pagination="{ pageSize: 20 }" row-key="_k" size="medium">
      <template #at="{ record }"><span class="muted">{{ record.at ? record.at.replace('T', ' ').slice(0, 19) : '—' }}</span></template>
      <template #type="{ record }">
        <a-tag :color="record.type === 'Warning' ? 'orange' : 'gray'">{{ record.type }}</a-tag>
      </template>
      <template #reason="{ record }"><strong>{{ record.reason }}</strong></template>
      <template #empty><a-empty :description="t('audit.noEvents')" /></template>
    </a-table>
  </a-card>
</template>

<style scoped>
.muted { color: var(--color-text-3); font-size: 12px; }
</style>
