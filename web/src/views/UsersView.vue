<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Message } from '@arco-design/web-vue'
import { auth } from '@/api'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import type { Me } from '@/api/types'

const { t } = useI18n()
const authStore = useAuthStore()

const loading = ref(false)
const users = ref<Me[]>([])
const showCreate = ref(false)

const form = reactive({
  username: '',
  display: '',
  password: '',
  role: 'user' as 'user' | 'admin',
  tenant: 'tenant-arise',
})

const tenants = ['tenant-arise', 'tenant-direct']

async function load() {
  loading.value = true
  try {
    users.value = (await auth.listUsers()).users
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

function resetForm() {
  form.username = ''
  form.display = ''
  form.password = ''
  form.role = 'user'
  form.tenant = 'tenant-arise'
}

async function beforeCreate() {
  // on-before-ok: Arco keeps the modal open + shows OK-loading until this
  // resolves; return true to close, false to keep it open on error.
  try {
    await auth.createUser({
      username: form.username.trim(),
      display: form.display.trim() || form.username.trim(),
      password: form.password,
      role: form.role,
      tenant: form.role === 'user' ? form.tenant : undefined,
    })
    Message.success(t('users.created', { name: form.username.trim() }))
    resetForm()
    await load()
    return true
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
    return false
  }
}

async function remove(u: Me) {
  try {
    await auth.deleteUser(u.name)
    Message.success(t('users.deleted', { name: u.name }))
    await load()
  } catch (e) {
    Message.error(e instanceof ApiError ? e.message : String(e))
  }
}

const adminCount = computed(() => users.value.filter((u) => u.role === 'admin').length)
function canDelete(u: Me) {
  if (u.name === authStore.me?.name) return false // no self-delete
  if (u.role === 'admin' && adminCount.value <= 1) return false // keep last admin
  return true
}

const columns = computed(() => [
  { title: t('users.username'), dataIndex: 'name', slotName: 'name' },
  { title: t('users.display'), dataIndex: 'display' },
  { title: t('users.role'), dataIndex: 'role', slotName: 'role' },
  { title: t('auth.tenant'), dataIndex: 'tenant', slotName: 'tenant' },
  { title: '', dataIndex: 'op', slotName: 'op', width: 110, align: 'right' as const },
])

onMounted(load)
</script>

<template>
  <a-card :bordered="false">
    <template #title>{{ t('nav.users') }}</template>
    <template #extra>
      <a-space>
        <a-button @click="load"><template #icon><icon-refresh /></template>{{ t('common.refresh') }}</a-button>
        <a-button type="primary" @click="showCreate = true">
          <template #icon><icon-plus /></template>{{ t('users.create') }}
        </a-button>
      </a-space>
    </template>

    <a-alert type="normal" style="margin-bottom: 14px">{{ t('users.seedNote') }}</a-alert>

    <a-table :columns="columns" :data="users" :loading="loading" :pagination="false" row-key="name" size="medium">
      <template #name="{ record }">
        <a-space>
          <a-avatar :size="26" :style="{ background: record.role === 'admin' ? 'rgb(var(--primary-6))' : 'var(--color-fill-3)' }">
            {{ (record.display || record.name).charAt(0).toUpperCase() }}
          </a-avatar>
          <strong>{{ record.name }}</strong>
        </a-space>
      </template>
      <template #role="{ record }">
        <a-tag :color="record.role === 'admin' ? 'arcoblue' : 'gray'">
          {{ record.role === 'admin' ? t('auth.roleAdmin') : t('auth.roleUser') }}
        </a-tag>
      </template>
      <template #tenant="{ record }">
        <span v-if="record.tenant">{{ record.tenant }}</span>
        <span v-else class="muted">—</span>
      </template>
      <template #op="{ record }">
        <a-popconfirm :content="t('users.confirmDelete', { name: record.name })" @ok="remove(record)">
          <a-button v-if="canDelete(record)" status="danger" size="mini" type="text">{{ t('common.delete') }}</a-button>
          <a-tooltip v-else :content="record.name === authStore.me?.name ? t('users.noSelfDelete') : t('users.lastAdmin')">
            <a-button size="mini" type="text" disabled>{{ t('common.delete') }}</a-button>
          </a-tooltip>
        </a-popconfirm>
      </template>
    </a-table>

    <a-modal v-model:visible="showCreate" :title="t('users.create')" :on-before-ok="beforeCreate"
      :ok-text="t('common.create')" :cancel-text="t('common.cancel')" @cancel="resetForm">
      <a-form :model="form" layout="vertical">
        <a-form-item :label="t('users.username')" required>
          <a-input v-model="form.username" placeholder="alice" allow-clear />
        </a-form-item>
        <a-form-item :label="t('users.display')">
          <a-input v-model="form.display" allow-clear />
        </a-form-item>
        <a-form-item :label="t('users.password')" required>
          <a-input-password v-model="form.password" :placeholder="t('users.passwordHint')" />
        </a-form-item>
        <a-form-item :label="t('users.role')">
          <a-radio-group v-model="form.role" type="button">
            <a-radio value="user">{{ t('auth.roleUser') }}</a-radio>
            <a-radio value="admin">{{ t('auth.roleAdmin') }}</a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item v-if="form.role === 'user'" :label="t('auth.tenant')">
          <a-select v-model="form.tenant">
            <a-option v-for="tn in tenants" :key="tn" :value="tn">{{ tn }}</a-option>
          </a-select>
        </a-form-item>
      </a-form>
    </a-modal>
  </a-card>
</template>

<style scoped>
.muted { color: var(--color-text-3); }
</style>
