<script setup lang="ts">
import { selectScrollbar } from "@/utils/accessibility";
import DataTable from "@/components/DataTable.vue";
import { computed, onMounted, reactive, ref } from "vue";
import PageHeading from "@/components/PageHeading.vue";
import MetricCard from "@/components/MetricCard.vue";
import EmptyState from "@/components/EmptyState.vue";
import { useI18n } from "vue-i18n";
import { Message } from "@arco-design/web-vue";
import { auth, papi } from "@/api";
import { ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import type { Me } from "@/api/types";

const { t } = useI18n();
const authStore = useAuthStore();

const loadError = ref("");
const loading = ref(false);
const users = ref<Me[]>([]);
const showCreate = ref(false);
const query = ref(""),
  roleFilter = ref("all"),
  formError = ref("");
const filtered = computed(() =>
  users.value.filter(
    (u) =>
      `${u.name} ${u.display}`
        .toLowerCase()
        .includes(query.value.toLowerCase().trim()) &&
      (roleFilter.value === "all" || roleFilter.value === u.role),
  ),
);
const deletion = reactive({
  visible: false,
  user: null as Me | null,
  error: "",
});
function deleteReason(u: Me) {
  if (u.name === authStore.me?.name) return t("users.noSelfDelete");
  if (["admin", "arise-dev", "direct-cust"].includes(u.name))
    return t("console.systemAccountHelp");
  return t("users.lastAdmin");
}
function openDelete(u: Me) {
  deletion.user = u;
  deletion.error = "";
  deletion.visible = true;
}
async function confirmDelete() {
  if (!deletion.user) return false;
  try {
    await auth.deleteUser(deletion.user.name);
    Message.success(t("users.deleted", { name: deletion.user.name }));
    await load();
    return true;
  } catch (e) {
    deletion.error = e instanceof Error ? e.message : String(e);
    return false;
  }
}

const form = reactive({
  username: "",
  display: "",
  password: "",
  role: "user" as "user" | "admin",
  tenant: "tenant-arise",
});

const tenants = ref<string[]>([]);

async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    const [accounts, catalog] = await Promise.all([
      auth.listUsers(),
      papi.flavors(),
    ]);
    users.value = accounts.users;
    tenants.value = catalog.tenants;
    if (!tenants.value.includes(form.tenant))
      form.tenant = tenants.value[0] || "";
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

function resetForm() {
  formError.value = "";
  form.username = "";
  form.display = "";
  form.password = "";
  form.role = "user";
  form.tenant = tenants.value[0] || "";
}

async function beforeCreate() {
  formError.value = "";
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(form.username.trim())) {
    formError.value = t("users.usernameHint");
    return false;
  }
  if (form.password.length < 12 || form.password.length > 1024) {
    formError.value = t("users.passwordHint");
    return false;
  }
  if (form.role === "user" && !tenants.value.includes(form.tenant)) {
    formError.value = t("users.tenantRequired");
    return false;
  }
  // on-before-ok: Arco keeps the modal open + shows OK-loading until this
  // resolves; return true to close, false to keep it open on error.
  try {
    await auth.createUser({
      username: form.username.trim(),
      display: form.display.trim() || form.username.trim(),
      password: form.password,
      role: form.role,
      tenant: form.role === "user" ? form.tenant : undefined,
    });
    Message.success(t("users.created", { name: form.username.trim() }));
    resetForm();
    await load();
    return true;
  } catch (e) {
    formError.value = e instanceof ApiError ? e.message : String(e);
    return false;
  }
}

const adminCount = computed(
  () => users.value.filter((u) => u.role === "admin").length,
);
function canDelete(u: Me) {
  if (["admin", "arise-dev", "direct-cust"].includes(u.name)) return false;
  if (u.name === authStore.me?.name) return false; // no self-delete
  if (u.role === "admin" && adminCount.value <= 1) return false; // keep last admin
  return true;
}

const columns = computed(() => [
  { title: t("users.username"), dataIndex: "name", slotName: "name" },
  { title: t("users.role"), dataIndex: "role", slotName: "role" },
  { title: t("auth.tenant"), dataIndex: "tenant", slotName: "tenant" },
  {
    title: t("console.actions"),
    dataIndex: "op",
    slotName: "op",
    width: 110,
    align: "right" as const,
  },
]);

onMounted(load);
</script>
<template>
  <div class="page">
    <PageHeading :title="t('nav.users')" :description="t('console.usersDesc')">
      <template #actions>
        <a-button
          :loading="loading"
          :aria-label="t('common.refresh')"
          @click="load"
        >
          <icon-refresh />
        </a-button>
        <a-button type="primary" @click="showCreate = true">
          <template #icon><icon-plus /></template>
          {{ t("users.create") }}
        </a-button>
      </template>
    </PageHeading>
    <a-alert v-if="loadError" type="error" class="page-error">
      {{ loadError }}
    </a-alert>
    <div v-if="!loading && !loadError" class="metrics-grid user-metrics">
      <MetricCard
        :label="t('console.totalUsers')"
        :value="users.length"
        icon="icon-user-group"
        :detail="t('console.userInfo')"
      />
      <MetricCard
        :label="t('console.administrators')"
        :value="adminCount"
        icon="icon-safe"
        accent="violet"
        :detail="t('console.platform')"
      />
      <MetricCard
        :label="t('console.members')"
        :value="users.length - adminCount"
        icon="icon-user"
        accent="teal"
        :detail="t('console.workspace')"
      />
    </div>
    <section class="panel users-panel">
      <div class="toolbar">
        <div class="filter-tabs">
          <button
            v-for="role in ['all', 'admin', 'user']"
            :key="role"
            type="button"
            :class="{ active: roleFilter === role }"
            :aria-pressed="roleFilter === role"
            @click="roleFilter = role"
          >
            {{
              t(
                role === "all"
                  ? "console.totalUsers"
                  : role === "admin"
                    ? "console.administrators"
                    : "console.members",
              )
            }}
          </button>
        </div>
        <a-input
          v-model="query"
          class="toolbar-search"
          :placeholder="t('console.searchUsers')"
          allow-clear
          :input-attrs="{ 'aria-label': t('console.searchUsers') }"
        >
          <template #prefix><icon-search /></template>
        </a-input>
      </div>
      <DataTable
        :columns="columns"
        :data="loadError ? [] : filtered"
        :loading="loading"
        :pagination="{ pageSize: 15, hideOnSinglePage: true }"
        row-key="name"
        :bordered="false"
      >
        <template #name="{ record }">
          <div class="account-name">
            <span
              class="account-avatar"
              :class="{ admin: record.role === 'admin' }"
            >
              {{ (record.display || record.name).charAt(0).toUpperCase() }}
            </span>
            <div>
              <strong>
                {{ record.display || record.name }}
                <span
                  v-if="record.name === authStore.me?.name"
                  class="you-label"
                >
                  {{ t("console.you") }}
                </span>
              </strong>
              <span class="resource-sub">{{ record.name }}</span>
            </div>
          </div>
        </template>
        <template #role="{ record }">
          <span class="role-tag" :class="{ admin: record.role === 'admin' }">
            <icon-safe v-if="record.role === 'admin'" />
            {{
              t(record.role === "admin" ? "auth.roleAdmin" : "auth.roleUser")
            }}
          </span>
        </template>
        <template #tenant="{ record }">
          <span class="account-scope">
            <icon-apps />
            {{ record.tenant || t("console.platform") }}
          </span>
        </template>
        <template #op="{ record }">
          <a-button
            v-if="canDelete(record)"
            size="small"
            type="text"
            status="danger"
            @click="openDelete(record)"
          >
            {{ t("common.delete") }}
          </a-button>
          <a-tooltip v-else :content="deleteReason(record)">
            <span class="locked-account" tabindex="0">
              <icon-lock />
              {{ t("console.systemAccount") }}
            </span>
          </a-tooltip>
        </template>
        <template #empty>
          <EmptyState
            :title="
              loadError ? t('common.unavailable') : t('console.emptyFiltered')
            "
            :description="loadError || t('console.emptyFilteredDesc')"
            compact
          >
            <a-button
              v-if="!loadError"
              @click="
                query = '';
                roleFilter = 'all';
              "
            >
              {{ t("console.clearFilters") }}
            </a-button>
          </EmptyState>
        </template>
      </DataTable>
      <p class="accounts-note">
        <icon-info-circle />
        {{ t("users.seedNote") }}
      </p>
    </section>
    <a-modal
      v-model:visible="showCreate"
      :title="t('users.create')"
      :on-before-ok="beforeCreate"
      :ok-text="t('common.create')"
      :cancel-text="t('common.cancel')"
      @cancel="resetForm"
    >
      <a-form :model="form" layout="vertical">
        <a-form-item :label="t('users.username')" required field="username">
          <a-input
            v-model="form.username"
            placeholder="alice"
            allow-clear
            :input-attrs="{
              'aria-label': t('users.username'),
              autocomplete: 'off',
            }"
          />
        </a-form-item>
        <a-form-item :label="t('users.display')" field="display">
          <a-input
            v-model="form.display"
            allow-clear
            :input-attrs="{ 'aria-label': t('users.display') }"
          />
        </a-form-item>
        <a-form-item
          :label="t('users.password')"
          required
          :help="t('users.passwordHint')"
          field="password"
        >
          <a-input-password
            v-model="form.password"
            :placeholder="t('users.passwordHint')"
            :input-attrs="{
              'aria-label': t('users.password'),
              autocomplete: 'new-password',
            }"
          />
        </a-form-item>
        <a-form-item :label="t('users.role')" field="role">
          <a-radio-group v-model="form.role" type="button">
            <a-radio value="user">{{ t("auth.roleUser") }}</a-radio>
            <a-radio value="admin">{{ t("auth.roleAdmin") }}</a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item
          v-if="form.role === 'user'"
          :label="t('console.workspace')"
          field="tenant"
        >
          <a-select
            :scrollbar="selectScrollbar"
            v-model="form.tenant"
            :placeholder="t('console.workspace')"
          >
            <a-option v-for="tn in tenants" :key="tn" :value="tn">
              {{ tn }}
            </a-option>
          </a-select>
        </a-form-item>
        <a-alert v-if="formError" type="error">{{ formError }}</a-alert>
      </a-form>
    </a-modal>
    <a-modal
      v-model:visible="deletion.visible"
      :title="t('common.confirmDelete')"
      :ok-text="t('common.delete')"
      :ok-button-props="{ status: 'danger' }"
      :on-before-ok="confirmDelete"
    >
      <p>{{ t("users.confirmDelete", { name: deletion.user?.name }) }}</p>
      <a-alert v-if="deletion.error" type="error">{{ deletion.error }}</a-alert>
    </a-modal>
  </div>
</template>
<style scoped>
.user-metrics {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-bottom: 0;
}
.users-panel {
  overflow: hidden;
}
.users-panel > .toolbar {
  padding: 20px 24px;
  border-bottom: 1px solid var(--line);
}
.account-name {
  display: flex;
  gap: 12px;
  align-items: center;
}
.account-avatar {
  display: grid;
  place-items: center;
  width: 35px;
  height: 35px;
  border-radius: 50%;
  background: var(--teal-soft);
  color: var(--teal);
  font-weight: 600;
  font-size: var(--text-sm);
}
.account-avatar.admin {
  background: var(--violet-soft);
  color: var(--violet);
}
.account-name strong {
  font-size: var(--text-sm);
  font-weight: 500;
  display: flex;
  gap: 8px;
  align-items: center;
}
.you-label {
  font-size: var(--text-xs);
  color: var(--muted);
  padding: 3px 5px;
  background: var(--surface-soft);
  border-radius: 4px;
}
.role-tag {
  font-size: var(--text-xs);
  display: inline-flex;
  gap: 5px;
  align-items: center;
  padding: 4px 8px;
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 5px;
  color: var(--muted);
}
.role-tag.admin {
  color: var(--accent);
  background: var(--accent-soft);
  border-color: transparent;
}
.account-scope {
  font-size: var(--text-sm);
  color: var(--muted);
  display: flex;
  gap: 7px;
  align-items: center;
}
.locked-account {
  font-size: var(--text-xs);
  color: var(--muted);
  display: inline-flex;
  align-items: center;
  gap: 5px;
  cursor: help;
}
.accounts-note {
  display: flex;
  gap: 8px;
  padding: 17px 24px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.8;
  margin: 0;
}
.accounts-note svg {
  flex: none;
  margin-top: 3px;
}
@media (max-width: 600px) {
  .user-metrics {
    grid-template-columns: 1fr;
  }
  .users-panel > .toolbar {
    padding: 15px;
  }
  .accounts-note {
    padding: 16px;
  }
}
</style>
