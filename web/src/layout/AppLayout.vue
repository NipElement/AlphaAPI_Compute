<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'
import { papi } from '@/api'
import { NAV } from '@/router'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const ui = useUiStore()

const collapsed = ref(false)
const tenants = ref<string[]>([])

const visibleNav = computed(() => NAV.filter((n) => !n.admin || authStore.isAdmin()))
const workbench = computed(() => visibleNav.value.filter((n) => n.group === 'workbench'))
const operations = computed(() => visibleNav.value.filter((n) => n.group === 'operations'))
const selected = computed(() => [route.name as string])
const avatarText = computed(() => (authStore.me?.display || authStore.me?.name || '?').charAt(0).toUpperCase())
const roleLabel = computed(() => (authStore.isAdmin() ? t('auth.roleAdmin') : t('auth.roleUser')))

// Pin the tenant for non-admins SYNCHRONOUSLY, before any child view mounts and
// calls load(). Otherwise the first request carries the ui-store default
// (tenant-arise) and the gateway's explicit cross-tenant check 403s a
// tenant-direct user on their own first page.
if (!authStore.isAdmin() && authStore.me?.tenant) ui.setTenant(authStore.me.tenant)

onMounted(async () => {
  if (authStore.isAdmin()) {
    try {
      const f = await papi.flavors()
      tenants.value = f.tenants
      if (f.tenants.length && !f.tenants.includes(ui.tenant)) ui.setTenant(f.tenants[0])
    } catch { /* non-fatal */ }
  }
})

function go(name: string) {
  if (route.name !== name) router.push({ name })
}
async function logout() {
  await authStore.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <a-layout class="shell">
    <a-layout-sider
      :collapsed="collapsed"
      collapsible
      :width="228"
      breakpoint="lg"
      @collapse="(v: boolean) => (collapsed = v)"
    >
      <div class="brand" :class="{ mini: collapsed }">
        <div class="mark"><icon-safe :size="20" /></div>
        <div v-if="!collapsed" class="brand-text">
          <div class="brand-name">{{ t('brand.name') }}</div>
          <div class="brand-sub">{{ t('brand.subtitle') }}</div>
        </div>
      </div>
      <a-menu :selected-keys="selected" :auto-open-selected="true" @menu-item-click="go">
        <a-menu-item-group :title="collapsed ? '' : t('nav.workbench')">
          <a-menu-item v-for="n in workbench" :key="n.name">
            <template #icon><component :is="n.icon" /></template>
            {{ t(n.labelKey) }}
          </a-menu-item>
        </a-menu-item-group>
        <a-menu-item-group v-if="operations.length" :title="collapsed ? '' : t('nav.operations')">
          <a-menu-item v-for="n in operations" :key="n.name">
            <template #icon><component :is="n.icon" /></template>
            {{ t(n.labelKey) }}
          </a-menu-item>
        </a-menu-item-group>
      </a-menu>
    </a-layout-sider>

    <a-layout>
      <a-layout-header class="topbar">
        <div class="title">{{ route.meta.labelKey ? t(route.meta.labelKey as string) : '' }}</div>
        <div class="spacer" />

        <a-select
          v-if="authStore.isAdmin() && tenants.length"
          :model-value="ui.tenant"
          size="small"
          style="width: 156px"
          @change="(v: any) => ui.setTenant(String(v))"
        >
          <template #prefix>{{ t('auth.tenant') }}</template>
          <a-option v-for="tn in tenants" :key="tn" :value="tn">{{ tn }}</a-option>
        </a-select>

        <a-radio-group :model-value="ui.locale" type="button" size="small" @change="(v: any) => ui.setLang(v)">
          <a-radio value="zh">{{ t('lang.zh') }}</a-radio>
          <a-radio value="en">{{ t('lang.en') }}</a-radio>
        </a-radio-group>

        <a-radio-group :model-value="ui.theme" type="button" size="small" @change="(v: any) => ui.setTheme(v)">
          <a-radio value="auto"><icon-desktop /></a-radio>
          <a-radio value="light"><icon-sun /></a-radio>
          <a-radio value="dark"><icon-moon /></a-radio>
        </a-radio-group>

        <a-dropdown trigger="click" position="br">
          <div class="user-chip">
            <a-avatar :size="30" :style="{ background: 'rgb(var(--primary-6))' }">{{ avatarText }}</a-avatar>
            <span class="user-name">{{ authStore.me?.display || authStore.me?.name }}</span>
            <a-tag size="small" :color="authStore.isAdmin() ? 'arcoblue' : 'gray'">{{ roleLabel }}</a-tag>
          </div>
          <template #content>
            <a-doption disabled>
              <div class="umeta">
                {{ authStore.me?.name }}<span v-if="authStore.me?.tenant"> · {{ authStore.me?.tenant }}</span>
              </div>
            </a-doption>
            <a-dgroup>
              <a-doption @click="logout"><template #icon><icon-export /></template>{{ t('auth.logout') }}</a-doption>
            </a-dgroup>
          </template>
        </a-dropdown>
      </a-layout-header>

      <a-layout-content class="content">
        <router-view v-slot="{ Component }">
          <keep-alive :max="4"><component :is="Component" /></keep-alive>
        </router-view>
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<style scoped>
.shell { height: 100vh; }
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 18px;
  height: 60px;
  box-sizing: border-box;
}
.brand.mini { justify-content: center; padding: 16px 0; }
.mark {
  width: 32px; height: 32px; border-radius: 9px; flex: none;
  display: grid; place-items: center; color: #fff;
  background: linear-gradient(135deg, rgb(var(--primary-6)), rgb(var(--primary-5)));
}
.brand-name { font-size: 14px; font-weight: 700; color: var(--color-text-1); line-height: 1.2; }
.brand-sub { font-size: 10px; color: var(--color-text-3); margin-top: 2px; white-space: nowrap; }
.topbar {
  display: flex; align-items: center; gap: 12px;
  padding: 0 18px; height: 60px;
  background: var(--color-bg-2);
  border-bottom: 1px solid var(--color-border-2);
}
.title { font-size: 16px; font-weight: 600; color: var(--color-text-1); }
.spacer { flex: 1; }
.user-chip { display: flex; align-items: center; gap: 8px; cursor: pointer; padding: 4px 6px; border-radius: 8px; }
.user-chip:hover { background: var(--color-fill-2); }
.user-name { font-size: 13px; color: var(--color-text-1); }
.umeta { font-size: 12px; color: var(--color-text-3); }
.content { padding: 18px; overflow: auto; background: var(--color-fill-1); }
</style>
