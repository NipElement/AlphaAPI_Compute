import { defineStore } from "pinia";
import { ref } from "vue";
import { auth } from "@/api";
import { ApiError } from "@/api/client";
import type { Me } from "@/api/types";

export const useAuthStore = defineStore("auth", () => {
  const me = ref<Me | null>(null);
  const ready = ref(false);
  const loadError = ref("");

  async function load() {
    loadError.value = "";
    try {
      me.value = await auth.me();
      ready.value = true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        me.value = null;
        ready.value = true;
      } else {
        loadError.value = e instanceof Error ? e.message : String(e);
        ready.value = false;
      }
    }
  }

  async function login(username: string, password: string) {
    me.value = await auth.login(username, password);
  }

  async function logout() {
    await auth.logout();
    me.value = null;
  }

  const isAdmin = () => me.value?.role === "admin";

  return { me, ready, loadError, load, login, logout, isAdmin };
});
