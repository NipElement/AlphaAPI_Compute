import { createI18n } from 'vue-i18n'
import zh from './locales/zh'
import en from './locales/en'

// Keyed i18n: components call t('nav.devMachines'); switching locale re-renders
// reactively. There is no post-render DOM text replacement, so none of the
// text-node / attribute / composed-string / reversibility bug classes exist.
export type Locale = 'zh' | 'en'

const stored = localStorage.getItem('arise-lang')
export const initialLocale: Locale = stored === 'en' ? 'en' : 'zh'

export const i18n = createI18n({
  legacy: false,
  locale: initialLocale,
  fallbackLocale: 'zh',
  messages: { zh, en },
})

export function setLocale(l: Locale) {
  i18n.global.locale.value = l
  localStorage.setItem('arise-lang', l)
  document.documentElement.lang = l === 'en' ? 'en' : 'zh-CN'
}
