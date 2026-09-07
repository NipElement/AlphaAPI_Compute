import { defineStore } from "pinia";
import { ref, watch } from "vue";
import { setLocale, initialLocale, type Locale } from "@/i18n";
import { readPreference, savePreference } from "@/utils/preferences";

type Theme = "light" | "dark";
function initialTheme(): Theme {
  const stored = readPreference("arise-theme");
  // Resolve the previous 'auto' preference once; the control is now binary.
  return stored === "dark" ||
    (stored === "auto" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches)
    ? "dark"
    : "light";
}
let themeFrame: number | undefined;
function applyTheme(theme: Theme) {
  const root = document.documentElement;
  // Switch the entire palette atomically. Hover transitions must not blend a
  // light background with already-dark foreground colors for several frames.
  if (themeFrame !== undefined) cancelAnimationFrame(themeFrame);
  root.classList.add("theme-switching");
  root.dataset.theme = theme;
  document.body.setAttribute("arco-theme", theme);
  void root.offsetHeight;
  themeFrame = requestAnimationFrame(() => {
    themeFrame = requestAnimationFrame(() => {
      root.classList.remove("theme-switching");
      themeFrame = undefined;
    });
  });
}

export const useUiStore = defineStore("ui", () => {
  const theme = ref<Theme>(initialTheme());
  const locale = ref<Locale>(initialLocale);
  const tenant = ref("tenant-arise");
  watch(
    theme,
    (value) => {
      applyTheme(value);
      savePreference("arise-theme", value);
    },
    { immediate: true, flush: "sync" },
  );
  function setTheme(value: Theme) {
    theme.value = value;
  }
  function toggleTheme() {
    setTheme(theme.value === "dark" ? "light" : "dark");
  }
  function setLang(value: Locale) {
    locale.value = value;
    setLocale(value);
  }
  function setTenant(ns: string) {
    tenant.value = ns;
  }
  return { theme, locale, tenant, setTheme, toggleTheme, setLang, setTenant };
});
