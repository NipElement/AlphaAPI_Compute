<script setup lang="ts">
withDefaults(
  defineProps<{
    title: string;
    description?: string;
    icon?: string;
    compact?: boolean;
  }>(),
  { icon: "icon-layers", compact: false },
);
</script>
<template>
  <div class="empty-state" :class="{ compact }">
    <div class="empty-orbit"><component :is="icon" :size="25" /></div>
    <h3>{{ title }}</h3>
    <p v-if="description">{{ description }}</p>
    <div v-if="$slots.default" class="empty-actions"><slot /></div>
  </div>
</template>
