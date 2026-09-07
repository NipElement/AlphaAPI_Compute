import { onMounted, onUnmounted, ref } from "vue";
import { papi } from "@/api";
import type { Overview } from "@/api/types";
export function useOverview(tenant: () => string) {
  const data = ref<Overview | null>(null),
    loading = ref(true),
    error = ref("");
  let generation = 0;
  async function load(silent = false) {
    const ticket = ++generation,
      ns = tenant();
    if (!silent) loading.value = true;
    error.value = "";
    try {
      const value = await papi.overview(ns);
      if (ticket === generation && tenant() === ns) data.value = value;
    } catch (e) {
      if (ticket === generation)
        error.value = e instanceof Error ? e.message : String(e);
    } finally {
      if (ticket === generation) loading.value = false;
    }
  }
  onMounted(() => load());
  onUnmounted(() => {
    generation++;
  });
  return { data, loading, error, load };
}
