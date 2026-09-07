<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import enUS from "@arco-design/web-vue/es/locale/lang/en-us";
import { useUiStore } from "@/stores/ui";
import { useAuthStore } from "@/stores/auth";
import { navigationFailure } from "@/router";
import zhCN from "@arco-design/web-vue/es/locale/lang/zh-cn";

// Arco's own components (pagination "共 N 条", empty "暂无数据", date pickers…)
// localize from this object; keep it in lockstep with the app locale.
const { locale, t } = useI18n();
useUiStore(); // Apply theme on login and failure screens too.
const authStore = useAuthStore();
const failure = computed(() => authStore.loadError || navigationFailure.value);
function reload() {
  window.location.reload();
}
const arcoLocale = computed(() => (locale.value === "en" ? enUS : zhCN));
</script>

<template>
  <a-config-provider :locale="arcoLocale">
    <a-result
      v-if="failure"
      status="error"
      :title="t('common.unavailable')"
      :subtitle="failure"
      style="padding: 10vh 20px"
    >
      <template #extra>
        <a-button type="primary" @click="reload">
          {{ t("common.refresh") }}
        </a-button>
      </template>
    </a-result>
    <router-view v-else />
  </a-config-provider>
</template>
