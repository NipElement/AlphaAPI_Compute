<script setup lang="ts">
import { computed, useAttrs } from "vue";
import { useI18n } from "vue-i18n";
defineOptions({ inheritAttrs: false });
const props = withDefaults(
  defineProps<{
    min?: number;
    max?: number;
    step?: number;
    disabled?: boolean;
  }>(),
  { min: 0, max: Number.MAX_SAFE_INTEGER, step: 1, disabled: false },
);
const model = defineModel<number>({ required: true });
const attrs = useAttrs(),
  { t } = useI18n();
const label = computed(
  () =>
    (attrs["input-attrs"] as Record<string, string> | undefined)?.[
      "aria-label"
    ] ?? "",
);
function adjust(direction: number) {
  const current = Number.isFinite(model.value)
    ? model.value
    : props.min - direction * props.step;
  model.value = Math.min(
    props.max,
    Math.max(props.min, current + direction * props.step),
  );
}
</script>
<template>
  <a-input-number
    v-model="model"
    v-bind="$attrs"
    :min="min"
    :max="max"
    :step="step"
    :disabled="disabled"
    hide-button
    mode="button"
    class="number-field"
  >
    <template #prepend>
      <button
        type="button"
        class="number-step minus"
        :aria-label="`${t('console.decrease')} ${label}`"
        :disabled="disabled || model <= min"
        @click="adjust(-1)"
      >
        <icon-minus />
      </button>
    </template>
    <template #append>
      <button
        type="button"
        class="number-step plus"
        :aria-label="`${t('console.increase')} ${label}`"
        :disabled="disabled || model >= max"
        @click="adjust(1)"
      >
        <icon-plus />
      </button>
    </template>
    <template v-if="$slots.suffix" #suffix><slot name="suffix" /></template>
  </a-input-number>
</template>
<style scoped>
.number-step {
  height: 40px;
  width: 40px;
  display: grid;
  place-items: center;
  background: var(--surface-soft);
  border: 0;
  color: var(--muted);
  cursor: pointer;
  font-size: var(--text-sm);
  flex: none;
}
.number-step:hover:not(:disabled) {
  color: var(--accent);
  background: var(--accent-soft);
}
.number-step:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.number-step.minus {
  border-radius: 7px 0 0 7px;
}
.number-step.plus {
  border-radius: 0 7px 7px 0;
}
</style>
