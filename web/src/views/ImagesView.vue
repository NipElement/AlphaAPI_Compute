<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { papi } from '@/api'
import type { Flavors } from '@/api/types'

const { t } = useI18n()
const flavors = ref<Flavors | null>(null)
onMounted(async () => { flavors.value = await papi.flavors() })

const columns = computed(() => [
  { title: t('images.image'), dataIndex: 'image', slotName: 'image' },
  { title: t('images.purpose'), dataIndex: 'purpose' },
  { title: t('common.status'), dataIndex: 'status', slotName: 'status' },
])
const data = computed(() =>
  (flavors.value?.images || []).map((im) => ({ image: im, purpose: t('images.baseRuntime'), status: 'available' })))
</script>

<template>
  <a-card :bordered="false" :title="t('images.presetImages')">
    <a-table :columns="columns" :data="data" :pagination="false" row-key="image" size="medium">
      <template #image="{ record }"><a-typography-text code>{{ record.image }}</a-typography-text></template>
      <template #status><a-tag color="green">{{ t('images.available') }}</a-tag></template>
    </a-table>
    <div class="hint">{{ t('images.note') }}</div>
  </a-card>
</template>

<style scoped>
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 10px; line-height: 1.6; }
</style>
