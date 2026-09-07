<script setup lang="ts">
import { computed, watch } from "vue";
import { useI18n } from "vue-i18n";
const props = defineProps<{ total: number; pageSize: number }>();
const current = defineModel<number>("current", { default: 1 });
const { t } = useI18n();
const pages = computed(() =>
  Math.max(1, Math.ceil(props.total / props.pageSize)),
);
watch(pages, (last) => {
  current.value = Math.min(last, Math.max(1, current.value));
});
const visible = computed(() => {
  const numbers = new Set([
    1,
    pages.value,
    current.value - 1,
    current.value,
    current.value + 1,
  ]);
  const values = [...numbers]
    .filter((n) => n > 0 && n <= pages.value)
    .sort((a, b) => a - b);
  const result: Array<number | string> = [];
  values.forEach((n, i) => {
    if (i && n - values[i - 1] > 1) result.push(`gap-${n}`);
    result.push(n);
  });
  return result;
});
</script>
<template>
  <nav class="pagination" :aria-label="t('pagination.label')">
    <button
      type="button"
      :disabled="current <= 1"
      :aria-label="t('pagination.previous')"
      @click="current--"
    >
      <span aria-hidden="true">←</span>
    </button>
    <template v-for="item in visible" :key="item">
      <button
        v-if="typeof item === 'number'"
        type="button"
        :aria-label="t('pagination.page', { n: item })"
        :aria-current="current === item ? 'page' : undefined"
        @click="current = item"
      >
        {{ item }}
      </button>
      <span v-else class="pagination-gap" aria-hidden="true">…</span>
    </template>
    <button
      type="button"
      :disabled="current >= pages"
      :aria-label="t('pagination.next')"
      @click="current++"
    >
      <span aria-hidden="true">→</span>
    </button>
  </nav>
</template>
<style scoped>
.pagination {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.pagination button {
  min-width: 36px;
  height: 36px;
  padding: 0 8px;
  border: 1px solid var(--line);
  border-radius: 7px;
  color: var(--ink);
  background: var(--surface);
  font-size: var(--text-sm);
}
.pagination button:hover:not(:disabled) {
  background: var(--surface-hover);
}
.pagination button[aria-current="page"] {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
  font-weight: 600;
}
.pagination button:disabled {
  color: var(--disabled);
  cursor: not-allowed;
}
.pagination-gap {
  color: var(--muted);
}
</style>
