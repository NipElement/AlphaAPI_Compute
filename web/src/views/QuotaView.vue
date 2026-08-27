<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { papi } from '@/api'
import { ApiError } from '@/api/client'
import { useUiStore } from '@/stores/ui'
import type { Flavors, Overview } from '@/api/types'

const { t } = useI18n()
const ui = useUiStore()

const ov = ref<Overview | null>(null)
const flavors = ref<Flavors | null>(null)
const loading = ref(false)

async function load() {
  loading.value = true
  const reqTenant = ui.tenant                 // drop stale responses (tenant race)
  try {
    const [o, f] = await Promise.all([papi.overview(reqTenant), papi.flavors()])
    if (reqTenant !== ui.tenant) return
    ov.value = o
    flavors.value = f
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally { loading.value = false }
}

function label(k: string) {
  // Order matters: the storage-class rule must run FIRST. Stripping the bare
  // 'requests.' prefix beforehand also eats the one inside
  // '...storage.k8s.io/requests.storage', after which the storage-class rule can
  // never match and the label renders as the raw key.
  return k
    .replace('.storageclass.storage.k8s.io/requests.storage', ` ${t('quota.storageSuffix')}`)
    .replace('requests.arise.dev/', '')
    .replace(/^requests\./, '')
}
function pct(used: string, hard: string) {
  const u = parseFloat(used) || 0
  const h = parseFloat(hard) || 1
  return Math.min(100, Math.round((100 * u) / h))
}
const quotaRows = computed(() =>
  Object.entries(ov.value?.quota || {}).map(([k, v]) => ({ key: k, label: label(k), ...v })))
const priorities = computed(() => (flavors.value?.priorities[ui.tenant] || []).join(' / '))

onMounted(load)
watch(() => ui.tenant, load)
</script>

<template>
  <a-space direction="vertical" size="medium" fill>
    <a-card :bordered="false" :title="t('quota.tenantQueue')">
      <a-descriptions :column="1" bordered size="medium">
        <a-descriptions-item :label="t('nav.quota')">
          <a-tag color="arcoblue">{{ ov?.queue }}</a-tag>
        </a-descriptions-item>
        <a-descriptions-item :label="t('quota.scheduling')">{{ t('quota.gangNote') }} · {{ priorities }}</a-descriptions-item>
        <a-descriptions-item :label="t('quota.entitlement')">{{ t('quota.entitlementNote') }}</a-descriptions-item>
      </a-descriptions>
    </a-card>

    <a-card :bordered="false" :loading="loading">
      <template #title>{{ t('quota.resourceQuota') }} ({{ ov?.namespace }})</template>
      <template #extra><a-button size="small" @click="load">
        <template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button></template>
      <a-row :gutter="[16, 16]">
        <a-col v-for="q in quotaRows" :key="q.key" :xs="12" :sm="8" :md="6">
          <div class="q">
            <div class="qlabel">{{ q.label }}</div>
            <div class="qval"><strong>{{ q.used }}</strong> / {{ q.hard }}</div>
            <a-progress :percent="pct(q.used, q.hard) / 100" :show-text="false" size="small" />
          </div>
        </a-col>
      </a-row>
      <div class="hint">{{ t('quota.simNote') }}</div>
      <div class="hint">{{ t('quota.nativeRowsNote') }}</div>
    </a-card>
  </a-space>
</template>

<style scoped>
.q { padding: 14px; border: 1px solid var(--color-border-2); border-radius: 10px; background: var(--color-bg-2); }
.qlabel { font-size: 12px; color: var(--color-text-3); word-break: break-all; }
.qval { font-size: 18px; margin: 6px 0 10px; color: var(--color-text-1); }
.hint { color: var(--color-text-3); font-size: 12px; margin-top: 14px; }
</style>
