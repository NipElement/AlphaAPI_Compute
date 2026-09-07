<script setup lang="ts">
import { computed } from "vue";
const props = withDefaults(
  defineProps<{
    percent: number;
    label: string;
    color?: string;
    description?: string;
    height?: number;
  }>(),
  { height: 6 },
);
const percent = computed(() =>
  Number.isFinite(props.percent) ? Math.max(0, Math.min(1, props.percent)) : 0,
);
</script>
<template>
  <div
    class="quota-meter"
    role="meter"
    :aria-label="label"
    :aria-valuemin="0"
    :aria-valuemax="100"
    :aria-valuenow="Number((percent * 100).toFixed(2))"
    :aria-valuetext="description"
    :style="{ height: `${height}px` }"
  >
    <span
      class="quota-meter-fill"
      :style="{
        width: `${percent * 100}%`,
        background: color || 'var(--accent)',
      }"
    />
  </div>
</template>
<style scoped>
.quota-meter {
  width: 100%;
  background: var(--surface-hover);
  border-radius: 6px;
  overflow: hidden;
}
.quota-meter-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
}
</style>
