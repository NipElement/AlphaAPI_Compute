<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '@/stores/auth'
import { ApiError } from '@/api/client'

const { t } = useI18n()
const router = useRouter()
const authStore = useAuthStore()

const username = ref('')
const password = ref('')
const error = ref('')
const loading = ref(false)

async function submit() {
  if (loading.value) return          // guard: @keyup.enter + form submit can both fire
  error.value = ''
  loading.value = true
  try {
    await authStore.login(username.value.trim(), password.value)
    router.push('/overview')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : t('auth.badCredentials')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-bg">
    <a-card class="login-card" :bordered="false">
      <div class="brand">
        <div class="mark">
          <icon-safe :size="26" />
        </div>
        <div class="brand-text">
          <div class="brand-name">{{ t('brand.name') }}</div>
          <div class="brand-sub">AI Compute Platform · 4× DGX B300</div>
        </div>
      </div>

      <a-form :model="{ username, password }" layout="vertical" @submit.prevent="submit">
        <a-form-item :label="t('auth.username')" hide-label>
          <a-input v-model="username" :placeholder="t('auth.username')" allow-clear autofocus>
            <template #prefix><icon-user /></template>
          </a-input>
        </a-form-item>
        <a-form-item :label="t('auth.password')" hide-label>
          <a-input-password v-model="password" :placeholder="t('auth.password')" @keyup.enter="submit">
            <template #prefix><icon-lock /></template>
          </a-input-password>
        </a-form-item>
        <a-alert v-if="error" type="error" style="margin-bottom: 14px">{{ error }}</a-alert>
        <a-button type="primary" long :loading="loading" html-type="submit" @click="submit">
          {{ t('auth.signInAction') }}
        </a-button>
      </a-form>

      <div class="hint">{{ t('auth.seedHint') }}</div>
    </a-card>
  </div>
</template>

<style scoped>
.login-bg {
  height: 100vh;
  display: grid;
  place-items: center;
  background:
    radial-gradient(48rem 30rem at 12% -8%, var(--color-primary-light-2), transparent 60%),
    radial-gradient(44rem 28rem at 105% 108%, var(--color-success-light-2), transparent 60%),
    var(--color-bg-1);
}
.login-card {
  width: min(400px, 92vw);
  border-radius: 16px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06), 0 24px 60px -12px rgba(16, 24, 40, 0.18);
  padding: 12px 12px 4px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 22px;
}
.mark {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  color: #fff;
  background: linear-gradient(135deg, rgb(var(--primary-6)), rgb(var(--primary-5)));
}
.brand-name {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--color-text-1);
}
.brand-sub {
  font-size: 11px;
  color: var(--color-text-3);
  margin-top: 2px;
}
.hint {
  margin-top: 6px;
  padding-top: 14px;
  border-top: 1px solid var(--color-border-2);
  color: var(--color-text-3);
  font-size: 11px;
  line-height: 1.6;
  text-align: center;
}
</style>
