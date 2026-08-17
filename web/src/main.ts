import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ArcoVue from '@arco-design/web-vue'
import ArcoVueIcon from '@arco-design/web-vue/es/icon'
import '@arco-design/web-vue/dist/arco.css'

import App from './App.vue'
import { router } from './router'
import { i18n } from './i18n'
import './styles/global.css'

createApp(App)
  .use(createPinia())
  .use(router)
  .use(i18n)
  .use(ArcoVue)
  .use(ArcoVueIcon)
  .mount('#app')
