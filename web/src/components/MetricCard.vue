<script setup lang="ts">
withDefaults(
  defineProps<{
    label: string;
    value: number | null;
    total?: number | string;
    precision?: number;
    detail?: string;
    icon?: string;
    accent?: string;
  }>(),
  { precision: 0, accent: "blue" },
);
</script>
<template>
  <section class="metric-card">
    <div class="metric-top">
      <span>{{ label }}</span>
      <span v-if="icon" class="metric-icon" :class="accent">
        <component :is="icon" :size="17" />
      </span>
    </div>
    <a-statistic v-if="value !== null" :value="value" :precision="precision">
      <template v-if="total !== undefined" #suffix>
        <span class="metric-total">/ {{ total }}</span>
      </template>
    </a-statistic>
    <div v-else class="metric-unavailable">—</div>
    <div v-if="detail" class="metric-detail">{{ detail }}</div>
  </section>
</template>
<style scoped>
.metric-unavailable {
  color: var(--muted);
  font-size: 1.9375rem;
  font-weight: 650;
  line-height: 1.5;
}
</style>
