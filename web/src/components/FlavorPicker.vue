<script setup lang="ts">
import NumberField from "./NumberField.vue";
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { Flavor } from "@/api/types";
const props = defineProps<{ flavors: Flavor[]; gpuOnly?: boolean }>();
const model = defineModel<{ vcpu: number; memGi: number; gpu: number }>({
  default: () => ({ vcpu: 4, memGi: 16, gpu: 0 }),
});
const { t } = useI18n();
const selectedKey = ref("");
const mode = computed(() =>
  model.value.gpu > 0 || props.gpuOnly ? "gpu" : "cpu",
);
const shown = computed(() =>
  props.flavors.filter((f) => (mode.value === "gpu" ? f.gpu > 0 : f.gpu === 0)),
);
const isCustom = computed(
  () => props.flavors.find((f) => f.key === selectedKey.value)?.vcpu === null,
);
function pick(f: Flavor) {
  selectedKey.value = f.key;
  model.value = {
    vcpu: f.vcpu ?? model.value.vcpu,
    memGi: f.memGi ?? model.value.memGi,
    gpu: f.gpu,
  };
}
function switchMode(next: string) {
  const f = props.flavors.find((f) =>
    next === "gpu" ? f.gpu > 0 : f.gpu === 0,
  );
  if (f) pick(f);
}
watch(
  () => props.flavors,
  (list) => {
    if (!selectedKey.value && list.length) {
      const match = list.find(
        (f) =>
          f.gpu === model.value.gpu &&
          f.vcpu === model.value.vcpu &&
          f.memGi === model.value.memGi &&
          (!props.gpuOnly || f.gpu > 0),
      );
      const custom =
        !props.gpuOnly && model.value.gpu === 0
          ? list.find((f) => f.vcpu === null && f.gpu === 0)
          : undefined;
      const initial =
        match ?? custom ?? list.find((f) => !props.gpuOnly || f.gpu > 0);
      if (initial) pick(initial);
    }
  },
  { immediate: true },
);
</script>
<template>
  <div class="flavor-picker">
    <div v-if="!gpuOnly" class="compute-modes">
      <button
        v-for="m in ['cpu', 'gpu']"
        :key="m"
        type="button"
        :class="{ selected: mode === m }"
        :aria-pressed="mode === m"
        @click="switchMode(m)"
      >
        <icon-desktop v-if="m === 'cpu'" />
        <icon-thunderbolt v-else />
        {{ t(m === "cpu" ? "console.cpuCompute" : "console.gpuCompute") }}
      </button>
    </div>
    <p class="flavor-hint">
      {{ t(mode === "gpu" ? "console.gpuHint" : "console.cpuHint") }}
    </p>
    <div class="flavor-grid">
      <button
        v-for="f in shown"
        :key="f.key"
        type="button"
        class="fcard"
        :class="{ on: selectedKey === f.key }"
        :aria-pressed="selectedKey === f.key"
        :data-flavor="f.key"
        @click="pick(f)"
      >
        <span class="flavor-radio">
          <icon-check v-if="selectedKey === f.key" :size="10" />
        </span>
        <strong>
          {{
            f.vcpu == null
              ? t("console.customCompute")
              : f.gpu
                ? `${f.gpu} × GPU`
                : `${f.vcpu} vCPU`
          }}
        </strong>
        <span v-if="f.vcpu != null">
          {{ f.gpu ? `${f.vcpu} vCPU · ` : "" }}{{ f.memGi }} GiB
        </span>
        <span v-else>{{ t("flavor.custom") }}</span>
        <small>{{ f.key }}</small>
      </button>
    </div>
    <div v-if="isCustom" class="custom-grid">
      <a-form-item :label="t('flavor.vcpu')" field="res.vcpu">
        <NumberField
          v-model="model.vcpu"
          :min="1"
          :max="256"
          :step="1"
          :precision="0"
          mode="button"
          :input-attrs="{ 'aria-label': t('flavor.vcpu') }"
        />
      </a-form-item>
      <a-form-item :label="t('flavor.memGi')" field="res.memGi">
        <NumberField
          v-model="model.memGi"
          :min="1"
          :max="2048"
          :step="1"
          :precision="0"
          mode="button"
          :input-attrs="{ 'aria-label': t('flavor.memGi') }"
        />
      </a-form-item>
    </div>
  </div>
</template>
<style scoped>
.flavor-picker {
  width: 100%;
}
.compute-modes {
  display: flex;
  padding: 4px;
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 9px;
  gap: 4px;
}
.compute-modes button {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 10px;
  border: 0;
  border-radius: 6px;
  background: none;
  font: inherit;
  color: var(--muted);
  cursor: pointer;
}
.compute-modes button.selected {
  background: var(--surface);
  color: var(--accent);
  box-shadow: 0 1px 4px #10182812;
}
.flavor-hint {
  font-size: var(--text-sm);
  line-height: 1.7;
  color: var(--muted);
  margin: 13px 0;
}
.flavor-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.fcard {
  position: relative;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
  padding: 17px 14px;
  cursor: pointer;
  transition: border-color 0.15s;
  color: var(--ink);
  min-width: 0;
}
.fcard:hover {
  border-color: var(--accent);
}
.fcard.on {
  border-color: var(--accent);
  background: var(--accent-soft);
  box-shadow: 0 0 0 1px var(--accent);
}
.fcard strong {
  font-size: var(--text-base);
  display: block;
  line-height: 1.6;
  padding-right: 16px;
}
.fcard > span:not(.flavor-radio) {
  display: block;
  font-size: var(--text-sm);
  margin-top: 7px;
  color: var(--muted);
}
.fcard small {
  display: block;
  font-size: var(--text-xs);
  color: var(--muted);
  margin-top: 13px;
}
.flavor-radio {
  position: absolute;
  right: 13px;
  top: 18px;
  width: 14px;
  height: 14px;
  border: 1px solid var(--line);
  background: var(--surface);
  border-radius: 50%;
  display: grid;
  place-items: center;
}
.on .flavor-radio {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-on);
}
.custom-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-top: 18px;
}
.custom-grid :deep(.arco-form-item) {
  margin-bottom: 0;
}
@media (max-width: 450px) {
  .flavor-grid {
    grid-template-columns: 1fr;
  }
  .custom-grid {
    grid-template-columns: 1fr;
  }
}
</style>
