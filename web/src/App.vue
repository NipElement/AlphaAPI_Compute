<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import enUS from '@arco-design/web-vue/es/locale/lang/en-us'
import zhCN from '@arco-design/web-vue/es/locale/lang/zh-cn'

// Arco's own components (pagination "共 N 条", empty "暂无数据", date pickers…)
// localize from this object; keep it in lockstep with the app locale.
const { locale } = useI18n()
const arcoLocale = computed(() => (locale.value === 'en' ? enUS : zhCN))
</script>

<template>
  <a-config-provider :locale="arcoLocale">
    <router-view />
  </a-config-provider>
</template>
