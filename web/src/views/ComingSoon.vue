<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'

const route = useRoute()
const { t } = useI18n()
const title = computed(() => {
  const key = route.meta.labelKey as string | undefined
  return key ? t(key) : ''
})
</script>

<template>
  <a-card :bordered="false">
    <a-result status="info" :title="title">
      <template #subtitle>{{ t('common.buildingModule') }}</template>
    </a-result>
  </a-card>
</template>
