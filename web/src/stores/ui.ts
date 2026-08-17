import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { setLocale, type Locale } from '@/i18n'

type Theme = 'auto' | 'light' | 'dark'

function systemDark() {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function applyTheme(theme: Theme) {
  const dark = theme === 'dark' || (theme === 'auto' && systemDark())
  // Arco reads body[arco-theme="dark"]; keep documentElement in sync for our CSS.
  document.body.setAttribute('arco-theme', dark ? 'dark' : 'light')
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
}

export const useUiStore = defineStore('ui', () => {
  const theme = ref<Theme>((localStorage.getItem('arise-theme') as Theme) || 'auto')
  const locale = ref<Locale>((localStorage.getItem('arise-lang') as Locale) === 'en' ? 'en' : 'zh')
  // Admin-selected tenant namespace (users are pinned server-side regardless).
  const tenant = ref<string>('tenant-arise')

  applyTheme(theme.value)
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  mq.addEventListener('change', () => { if (theme.value === 'auto') applyTheme('auto') })

  watch(theme, (t) => {
    localStorage.setItem('arise-theme', t)
    applyTheme(t)
  })

  function setTheme(t: Theme) { theme.value = t }
  function setLang(l: Locale) { locale.value = l; setLocale(l) }
  function setTenant(ns: string) { tenant.value = ns }

  return { theme, locale, tenant, setTheme, setLang, setTenant }
})
