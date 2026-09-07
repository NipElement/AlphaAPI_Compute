import { createApp, watch } from "vue";
import { createPinia } from "pinia";
import ArcoVue from "@arco-design/web-vue";
import {
  IconApps,
  IconArrowRight,
  IconArrowRise,
  IconBarChart,
  IconCheck,
  IconCheckCircle,
  IconClockCircle,
  IconCloseCircle,
  IconCloud,
  IconCode,
  IconComputer,
  IconCopy,
  IconDashboard,
  IconDelete,
  IconDesktop,
  IconDown,
  IconDownload,
  IconExclamation,
  IconExclamationCircle,
  IconExport,
  IconFile,
  IconFolder,
  IconHistory,
  IconInfoCircle,
  IconLanguage,
  IconLayers,
  IconLink,
  IconLock,
  IconMenu,
  IconMenuFold,
  IconMenuUnfold,
  IconMinus,
  IconMoon,
  IconMore,
  IconNotification,
  IconPlus,
  IconPause,
  IconPlayArrow,
  IconRefresh,
  IconSafe,
  IconSchedule,
  IconSearch,
  IconStorage,
  IconSun,
  IconSwap,
  IconThunderbolt,
  IconUser,
  IconUserGroup,
  IconWifi,
} from "@arco-design/web-vue/es/icon";
import "@arco-design/web-vue/dist/arco.css";

import App from "./App.vue";
import { router } from "./router";
import { i18n } from "./i18n";
import "./styles/global.css";
import "./styles/controls.css";

const app = createApp(App)
  .use(createPinia())
  .use(router)
  .use(i18n)
  .use(ArcoVue);

// Explicit registration keeps the unused icon catalog out of the entry bundle.
for (const [name, icon] of Object.entries({
  IconApps,
  IconArrowRight,
  IconArrowRise,
  IconBarChart,
  IconCheck,
  IconCheckCircle,
  IconClockCircle,
  IconCloseCircle,
  IconCloud,
  IconCode,
  IconComputer,
  IconCopy,
  IconDashboard,
  IconDelete,
  IconDesktop,
  IconDown,
  IconDownload,
  IconExclamation,
  IconExclamationCircle,
  IconExport,
  IconFile,
  IconFolder,
  IconHistory,
  IconInfoCircle,
  IconLanguage,
  IconLayers,
  IconLink,
  IconLock,
  IconMenu,
  IconMenuFold,
  IconMenuUnfold,
  IconMinus,
  IconMoon,
  IconMore,
  IconNotification,
  IconPlus,
  IconPause,
  IconPlayArrow,
  IconRefresh,
  IconSafe,
  IconSchedule,
  IconSearch,
  IconStorage,
  IconSun,
  IconSwap,
  IconThunderbolt,
  IconUser,
  IconUserGroup,
  IconWifi,
}))
  app.component(name, icon);
app.mount("#app");

// Keep the browser tab and document language aligned with navigation and the
// persisted language, including the first visit after reopening a tab.
watch(
  [
    () => router.currentRoute.value.meta.labelKey,
    () => i18n.global.locale.value,
  ],
  ([key, locale]) => {
    const label =
      typeof key === "string"
        ? i18n.global.t(key)
        : i18n.global.t("auth.signIn");
    document.title = `${label} · ARISE Compute`;
    document.documentElement.lang = locale === "en" ? "en" : "zh-CN";
  },
  { immediate: true },
);
