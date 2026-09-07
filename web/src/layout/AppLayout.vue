<script setup lang="ts">
import { selectScrollbar } from "@/utils/accessibility";
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { useAuthStore } from "@/stores/auth";
import { useUiStore } from "@/stores/ui";
import { papi, auth } from "@/api";
import { ApiError } from "@/api/client";
import { Message } from "@arco-design/web-vue";
import { NAV } from "@/router";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const authStore = useAuthStore();
const ui = useUiStore();

const collapsed = ref(false);
const tenants = ref<string[]>([]);

const visibleNav = computed(() =>
  NAV.filter((n) => !n.admin || authStore.isAdmin()),
);
const workbench = computed(() =>
  visibleNav.value.filter((n) => n.group === "workbench"),
);
const operations = computed(() =>
  visibleNav.value.filter((n) => n.group === "operations"),
);
const mobileOpen = ref(false);
const compactViewport = ref(window.innerWidth < 1024);
function resize() {
  compactViewport.value = window.innerWidth < 1024;
  if (!compactViewport.value) mobileOpen.value = false;
}
watch(mobileOpen, async (open) => {
  if (open) collapsed.value = false;
  await nextTick();
  if (open)
    document.querySelector<HTMLElement>(".sidebar .nav-item.active")?.focus();
  else if (compactViewport.value)
    document.querySelector<HTMLElement>(".mobile-menu")?.focus();
});
const commandOpen = ref(false);
const query = ref("");
const commandIndex = ref(0);
const commandResults = computed(() =>
  visibleNav.value.filter((n) =>
    `${t(n.labelKey)} ${n.name}`
      .toLowerCase()
      .includes(query.value.toLowerCase().trim()),
  ),
);
watch(query, () => (commandIndex.value = 0));
watch(
  () => route.name,
  () => {
    mobileOpen.value = false;
    commandOpen.value = false;
  },
);
function openCommand() {
  query.value = "";
  commandIndex.value = 0;
  commandOpen.value = true;
}
function keydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    commandOpen.value ? (commandOpen.value = false) : openCommand();
  }
  if (e.key === "Escape") mobileOpen.value = false;
  if (e.key === "Tab" && mobileOpen.value && compactViewport.value) {
    const elements = [
      ...document.querySelectorAll<HTMLElement>(".sidebar a, .sidebar button"),
    ].filter((el) => el.getClientRects().length);
    const first = elements[0],
      last = elements[elements.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }
}
function commandKey(e: KeyboardEvent) {
  const count = commandResults.value.length;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    commandIndex.value = count
      ? (commandIndex.value + (e.key === "ArrowDown" ? 1 : -1) + count) % count
      : 0;
  }
  if (e.key === "Enter" && commandResults.value[commandIndex.value]) {
    e.preventDefault();
    go(commandResults.value[commandIndex.value].name);
  }
}
onMounted(() => {
  window.addEventListener("keydown", keydown);
  window.addEventListener("resize", resize);
});
onUnmounted(() => {
  window.removeEventListener("keydown", keydown);
  window.removeEventListener("resize", resize);
});
const avatarText = computed(() =>
  (authStore.me?.display || authStore.me?.name || "?").charAt(0).toUpperCase(),
);
const roleLabel = computed(() =>
  authStore.isAdmin() ? t("auth.roleAdmin") : t("auth.roleUser"),
);

// Pin the tenant for non-admins SYNCHRONOUSLY, before any child view mounts and
// calls load(). Otherwise the first request carries the ui-store default
// (tenant-arise) and the gateway's explicit cross-tenant check 403s a
// tenant-direct user on their own first page.
if (!authStore.isAdmin() && authStore.me?.tenant)
  ui.setTenant(authStore.me.tenant);

onMounted(async () => {
  if (authStore.isAdmin()) {
    try {
      const f = await papi.flavors();
      tenants.value = f.tenants;
      if (f.tenants.length && !f.tenants.includes(ui.tenant))
        ui.setTenant(f.tenants[0]);
    } catch {
      /* non-fatal */
    }
  }
});

function go(name: string) {
  commandOpen.value = false;
  if (route.name !== name) router.push({ name }).catch(() => {});
}
const pw = ref({ visible: false, current: "", next: "", busy: false });
async function changePassword() {
  if (pw.value.next.length < 12) {
    Message.warning(t("auth.pwTooShort"));
    return false;
  }
  pw.value.busy = true;
  try {
    await auth.changePassword(pw.value.current, pw.value.next);
    pw.value.visible = false;
    Message.success(t("auth.pwChanged"));
    pw.value.current = "";
    pw.value.next = "";
    authStore.me = null; // password change has already revoked every session
    await router.replace({ name: "login" });
    return true;
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e));
    return false;
  } finally {
    pw.value.busy = false;
  }
}
async function logout() {
  try {
    await authStore.logout();
    router.push({ name: "login" });
  } catch (error) {
    Message.error(error instanceof Error ? error.message : String(error));
  }
}
</script>
<template>
  <div class="shell" :class="{ collapsed }">
    <a href="#main-content" class="skip-link">{{ t("console.navigate") }}</a>
    <button
      v-if="mobileOpen"
      class="sidebar-backdrop"
      :aria-label="t('console.close')"
      @click="mobileOpen = false"
    />
    <aside
      :inert="compactViewport && !mobileOpen"
      class="sidebar"
      :class="{ 'mobile-open': mobileOpen }"
      :aria-label="t('console.menu')"
    >
      <router-link to="/overview" class="brand" aria-label="ARISE Compute">
        <svg
          class="brand-mark"
          width="34"
          height="34"
          viewBox="0 0 34 34"
          fill="none"
          aria-hidden="true"
        >
          <rect width="34" height="34" rx="10" fill="currentColor" />
          <path
            d="M9 24 16 9h3l7 15h-5l-1.3-3H15l1.5-3.6h1.6L17.4 15 13.5 24H9Z"
            fill="white"
          />
        </svg>
        <span class="brand-text">
          ARISE
          <small>COMPUTE</small>
        </span>
      </router-link>
      <div class="workspace-card" v-if="!collapsed">
        <span class="workspace-icon"><icon-apps /></span>
        <div>
          <small>{{ t("console.workspace") }}</small>
          <strong :title="ui.tenant">{{ ui.tenant }}</strong>
        </div>
        <icon-lock v-if="!authStore.isAdmin()" />
      </div>
      <nav class="sidebar-nav">
        <div class="nav-section-label">
          {{ collapsed ? "—" : t("console.workspace") }}
        </div>
        <router-link
          v-for="n in workbench"
          :key="n.name"
          :to="{ name: n.name }"
          class="nav-item"
          :class="{ active: route.name === n.name }"
          :aria-current="route.name === n.name ? 'page' : undefined"
          :title="collapsed ? t(n.labelKey) : undefined"
        >
          <component :is="n.icon" :size="18" />
          <span v-if="!collapsed">{{ t(n.labelKey) }}</span>
          <i v-if="route.name === n.name && !collapsed" />
        </router-link>
        <template v-if="operations.length">
          <div class="nav-section-label operations-label">
            {{ collapsed ? "—" : t("console.platform") }}
          </div>
          <router-link
            v-for="n in operations"
            :key="n.name"
            :to="{ name: n.name }"
            class="nav-item"
            :class="{ active: route.name === n.name }"
            :aria-current="route.name === n.name ? 'page' : undefined"
            :title="collapsed ? t(n.labelKey) : undefined"
          >
            <component :is="n.icon" :size="18" />
            <span v-if="!collapsed">{{ t(n.labelKey) }}</span>
            <i v-if="route.name === n.name && !collapsed" />
          </router-link>
        </template>
      </nav>
      <div class="sidebar-footer">
        <span v-if="!collapsed">
          ARISE
          <span class="muted">/</span>
          {{ t("console.console") }}
        </span>
        <button
          type="button"
          :aria-label="t(collapsed ? 'console.expand' : 'console.collapse')"
          @click="collapsed = !collapsed"
        >
          <icon-menu-fold v-if="!collapsed" />
          <icon-menu-unfold v-else />
        </button>
      </div>
    </aside>
    <div class="main-shell" :inert="compactViewport && mobileOpen">
      <header class="topbar">
        <a-button
          class="mobile-menu"
          type="text"
          :aria-label="t('console.menu')"
          :aria-expanded="mobileOpen"
          @click="mobileOpen = !mobileOpen"
        >
          <icon-menu />
        </a-button>
        <div class="breadcrumbs">
          <span>
            {{
              t(
                route.meta.requiresAdmin
                  ? "console.platform"
                  : "console.workspace",
              )
            }}
          </span>
          <span class="crumb-divider">/</span>
          <span
            class="title"
            :title="route.meta.labelKey ? t(route.meta.labelKey as string) : ''"
          >
            {{ route.meta.labelKey ? t(route.meta.labelKey as string) : "" }}
          </span>
        </div>
        <div class="spacer" />
        <button
          type="button"
          class="command-trigger"
          :aria-label="t('console.searchPages')"
          @click="openCommand"
        >
          <icon-search />
          <span>{{ t("console.searchPages") }}</span>
          <kbd>⌘ K</kbd>
        </button>
        <a-select
          :scrollbar="selectScrollbar"
          v-if="authStore.isAdmin() && tenants.length"
          class="tenant-select"
          :placeholder="t('console.workspaceSwitch')"
          :model-value="ui.tenant"
          size="small"
          @change="(v: any) => ui.setTenant(String(v))"
        >
          <a-option v-for="tn in tenants" :key="tn" :value="tn">
            {{ tn }}
          </a-option>
        </a-select>
        <a-button
          type="text"
          class="language-button"
          :aria-label="t('lang.label')"
          @click="ui.setLang(ui.locale === 'zh' ? 'en' : 'zh')"
        >
          {{ ui.locale === "zh" ? "EN" : "中文" }}
        </a-button>
        <a-button
          type="text"
          class="theme-toggle"
          :aria-label="
            t(ui.theme === 'dark' ? 'theme.switchLight' : 'theme.switchDark')
          "
          :title="
            t(ui.theme === 'dark' ? 'theme.switchLight' : 'theme.switchDark')
          "
          :aria-pressed="ui.theme === 'dark'"
          @click="ui.toggleTheme()"
        >
          <icon-sun v-if="ui.theme === 'dark'" />
          <icon-moon v-else />
        </a-button>
        <div class="topbar-divider" />
        <a-dropdown trigger="click" position="br">
          <button
            type="button"
            class="user-chip"
            :aria-label="authStore.me?.display || authStore.me?.name"
            :title="authStore.me?.display || authStore.me?.name"
          >
            <span class="user-avatar">{{ avatarText }}</span>
            <span class="user-copy">
              <strong>{{ authStore.me?.display || authStore.me?.name }}</strong>
              <small>{{ roleLabel }}</small>
            </span>
            <icon-down :size="10" />
          </button>
          <template #content>
            <a-doption disabled class="account-menu-identity">
              {{ authStore.me?.name }}
            </a-doption>
            <a-dgroup>
              <a-doption @click="pw.visible = true">
                <template #icon><icon-lock /></template>
                {{ t("auth.changePassword") }}
              </a-doption>
              <a-doption @click="logout">
                <template #icon><icon-export /></template>
                {{ t("auth.logout") }}
              </a-doption>
            </a-dgroup>
          </template>
        </a-dropdown>
      </header>
      <main id="main-content" class="content" tabindex="-1">
        <router-view v-slot="{ Component }">
          <component
            :is="Component"
            :key="`${String(route.name)}:${ui.tenant}`"
          />
        </router-view>
      </main>
    </div>
    <a-modal
      v-model:visible="commandOpen"
      :title="t('console.searchPages')"
      :footer="false"
      :width="560"
      unmount-on-close
    >
      <a-input
        v-model="query"
        allow-clear
        :placeholder="t('console.searchHint')"
        @keydown="commandKey"
        :input-attrs="{
          'aria-label': t('console.searchPages'),
          autofocus: true,
        }"
      >
        <template #prefix><icon-search /></template>
      </a-input>
      <div class="command-results">
        <button
          v-for="(n, i) in commandResults"
          :key="n.name"
          type="button"
          :class="{ highlighted: commandIndex === i }"
          @click="go(n.name)"
          @mouseenter="commandIndex = i"
        >
          <component :is="n.icon" />
          <span>{{ t(n.labelKey) }}</span>
          <icon-arrow-right />
        </button>
        <p v-if="!commandResults.length" class="muted">
          {{ t("console.noMatches") }}
        </p>
      </div>
      <div class="command-hint">
        <span>↑ ↓</span>
        {{ t("console.navigate") }}
        <span>↵</span>
        Enter
        <span>esc</span>
        {{ t("console.close") }}
      </div>
    </a-modal>
    <a-modal
      v-model:visible="pw.visible"
      :title="t('auth.changePassword')"
      :on-before-ok="changePassword"
      @cancel="
        pw.current = '';
        pw.next = '';
      "
    >
      <p class="hint">{{ t("auth.pwHint") }}</p>
      <a-input-password
        v-model="pw.current"
        :placeholder="t('auth.pwCurrent')"
        style="margin-bottom: 14px"
        :input-attrs="{
          'aria-label': t('auth.pwCurrent'),
          autocomplete: 'current-password',
        }"
      />
      <a-input-password
        v-model="pw.next"
        :placeholder="t('auth.pwNew')"
        :input-attrs="{
          'aria-label': t('auth.pwNew'),
          autocomplete: 'new-password',
        }"
      />
    </a-modal>
  </div>
</template>
<style scoped>
.shell {
  --sidebar-width: 256px;
  min-height: 100vh;
}
.shell.collapsed {
  --sidebar-width: 76px;
}
.sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  width: var(--sidebar-width);
  background: var(--surface);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  z-index: 30;
}
.brand {
  height: 82px;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 0 24px;
  text-decoration: none;
  color: var(--accent);
  flex-shrink: 0;
}
.brand-mark {
  flex-shrink: 0;
}
.brand-text {
  color: var(--ink);
  font-weight: 800;
  font-size: 1.25rem;
  letter-spacing: 1px;
}
.brand-text small {
  display: block;
  font-size: var(--text-xs);
  letter-spacing: 3.6px;
  font-weight: 500;
  margin-top: 2px;
  color: var(--muted);
}
.collapsed .brand {
  padding: 0 21px;
}
.collapsed .sidebar-footer {
  justify-content: center;
  padding: 16px 12px;
}
.collapsed .brand-text {
  display: none;
}
.workspace-card {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: 9px;
  margin: 0 17px 23px;
  padding: 10px;
  background: var(--surface-soft);
  min-width: 0;
}
.workspace-icon {
  display: grid;
  place-items: center;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 7px;
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  color: var(--accent);
}
.workspace-card > div {
  min-width: 0;
  flex: 1;
}
.workspace-card small {
  display: block;
  font-size: var(--text-xs);
  color: var(--muted);
  margin-bottom: 4px;
}
.workspace-card strong {
  display: block;
  font-size: var(--text-sm);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
}
.workspace-card > svg {
  font-size: var(--text-sm);
  color: var(--muted);
}
.sidebar-nav {
  padding: 0 14px;
  overflow-y: auto;
  flex: 1;
}
.nav-section-label {
  font-size: var(--text-xs);
  color: var(--muted);
  padding: 0 12px 10px;
  letter-spacing: 1px;
}
.operations-label {
  margin-top: 27px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 46px;
  padding-top: 8px;
  padding-bottom: 8px;
  padding: 0 13px;
  margin: 3px 0;
  border-radius: 8px;
  color: var(--muted);
  text-decoration: none;
  font-size: var(--text-base);
  font-weight: 500;
  transition:
    background 0.15s,
    color 0.15s;
}
.nav-item > svg {
  flex-shrink: 0;
}
.nav-item:hover {
  background: var(--surface-soft);
  color: var(--ink);
}
.nav-item.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.nav-item i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  margin-left: auto;
}
.sidebar-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px;
  font-size: var(--text-xs);
  color: var(--muted);
  border-top: 1px solid var(--line);
  gap: 8px;
}
.sidebar-footer button {
  min-width: 36px;
  min-height: 36px;
  background: none;
  border: 0;
  padding: 0;
  color: var(--muted);
  cursor: pointer;
  font-size: var(--text-lg);
}
.main-shell {
  margin-left: var(--sidebar-width);
  min-width: 0;
}
.topbar {
  height: 72px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  padding: 0 32px;
  position: sticky;
  top: 0;
  z-index: 20;
}
.breadcrumbs {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  font-size: var(--text-sm);
  white-space: nowrap;
  color: var(--muted);
}
.breadcrumbs .title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--ink);
  font-weight: 500;
}
.crumb-divider {
  opacity: 0.4;
}
.spacer {
  flex: 1;
}
.command-trigger {
  min-height: 38px;
  flex-shrink: 0;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--surface);
  color: var(--muted);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}
.command-trigger kbd {
  font-size: var(--text-xs);
  margin-left: 24px;
  background: var(--surface-soft);
  padding: 2px 4px;
  border: 1px solid var(--line);
  border-radius: 4px;
}
.topbar :deep(.tenant-select) {
  width: 180px;
  flex: 0 1 180px;
  min-width: 120px;
}
.language-button {
  font-size: var(--text-sm);
  padding: 0 8px;
}
.topbar-divider {
  height: 25px;
  width: 1px;
  background: var(--line);
  margin: 0 4px;
}
.user-chip {
  max-width: 224px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 9px;
  background: none;
  border: 0;
  cursor: pointer;
  color: var(--ink);
  font: inherit;
  padding: 4px 0;
}
.user-avatar {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--violet-soft);
  color: var(--violet);
  font-weight: 600;
  font-size: var(--text-sm);
}
.user-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: left;
}
.user-copy strong {
  display: block;
  max-width: 160px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-weight: 600;
  font-size: var(--text-sm);
}
.user-copy small {
  font-size: var(--text-xs);
  color: var(--muted);
}
.content {
  padding: 32px 36px 48px;
  max-width: 1680px;
  margin: auto;
  outline: none;
}
.mobile-menu {
  display: none;
}
.sidebar-backdrop {
  display: none;
}
.skip-link {
  position: fixed;
  top: -100px;
  left: 250px;
  z-index: 2000;
  background: var(--accent);
  color: var(--accent-on);
  padding: 12px;
}
.skip-link:focus {
  top: 8px;
}
.command-results {
  margin: 14px -4px;
  max-height: 390px;
  overflow: auto;
}
.command-results button {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  border: 0;
  border-radius: 7px;
  background: none;
  color: var(--ink);
  padding: 12px;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.command-results button.highlighted {
  background: var(--accent-soft);
  color: var(--accent);
}
.command-results button span {
  flex: 1;
}
.command-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: var(--text-sm);
  border-top: 1px solid var(--line);
  padding-top: 14px;
}
.command-hint span {
  padding: 2px 5px;
  border: 1px solid var(--line);
  border-radius: 4px;
}
.command-hint span:not(:first-child) {
  margin-left: 12px;
}
@media (min-width: 1024px) and (max-width: 1399px) {
  .command-trigger span,
  .command-trigger kbd,
  .user-copy {
    display: none;
  }
  .topbar {
    padding: 0 22px;
    gap: 8px;
  }
  .content {
    padding: 26px 22px;
  }
  .breadcrumbs {
    gap: 8px;
  }
}
@media (max-width: 1023px) {
  .shell,
  .shell.collapsed {
    --sidebar-width: 0px;
  }
  .sidebar {
    width: min(280px, calc(100vw - 48px));
    transform: translateX(-100%);
    transition: transform 0.2s;
  }
  .sidebar.mobile-open {
    transform: translateX(0);
  }
  .collapsed .brand-text {
    display: block;
  }
  .sidebar-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 29;
    border: 0;
    background: #10182866;
  }
  .sidebar-footer button {
    display: none;
  }
  .main-shell {
    margin-left: 0;
  }
  .topbar {
    height: auto;
    min-height: 68px;
    flex-wrap: wrap;
    padding: 10px 16px;
    gap: 7px;
  }
  .mobile-menu {
    display: inline-flex;
  }
  .breadcrumbs,
  .breadcrumbs > span:first-child,
  .crumb-divider,
  .command-trigger span,
  .command-trigger kbd,
  .user-copy,
  .user-chip > svg,
  .topbar-divider {
    display: none;
  }
  .command-trigger {
    border: 0;
    font-size: 1.0625rem;
    padding: 7px;
  }
  .topbar :deep(.tenant-select) {
    width: min(200px, 55vw);
    flex: 0 1 200px;
  }

  .content {
    padding: 24px 16px 36px;
  }
  .user-avatar {
    width: 28px;
    height: 28px;
  }
  .topbar :deep(.arco-btn),
  .command-trigger {
    min-width: 44px;
    min-height: 44px;
    padding: 0 8px;
  }
  .breadcrumbs {
    font-size: var(--text-sm);
  }
  .sidebar .nav-item span {
    display: block;
  }
  .sidebar-nav {
    padding-bottom: 20px;
  }
}
@media (max-width: 575px) {
  .topbar :deep(.tenant-select) {
    order: 10;
    flex: 0 0 100%;
    width: 100%;
    min-width: 0;
  }
  .topbar .user-chip {
    min-width: 44px;
    min-height: 44px;
    justify-content: center;
  }
}
</style>
