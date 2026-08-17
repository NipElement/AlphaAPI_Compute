import { defineStore } from 'pinia'
import { ref } from 'vue'
import { auth } from '@/api'
import type { Me } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const me = ref<Me | null>(null)
  const ready = ref(false)

  async function load() {
    try {
      me.value = await auth.me()
    } catch {
      me.value = null
    } finally {
      ready.value = true
    }
  }

  async function login(username: string, password: string) {
    me.value = await auth.login(username, password)
  }

  async function logout() {
    try {
      await auth.logout()
    } finally {
      me.value = null
    }
  }

  const isAdmin = () => me.value?.role === 'admin'

  return { me, ready, load, login, logout, isAdmin }
})
