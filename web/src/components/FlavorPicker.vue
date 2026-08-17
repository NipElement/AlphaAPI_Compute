<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Flavor } from '@/api/types'

const props = defineProps<{
  flavors: Flavor[]
  gpuOnly?: boolean
}>()

// The create body carries whole-unit resources; the gateway/portal enforce the
// quantization grid, so we only need to surface valid choices here.
const model = defineModel<{ vcpu: number; memGi: number; gpu: number }>({
  default: () => ({ vcpu: 4, memGi: 16, gpu: 0 }),
})

const { t } = useI18n()

const shown = computed(() => props.flavors.filter((f) => (props.gpuOnly ? f.gpu > 0 : true)))
const selectedKey = ref<string>('')

function pick(f: Flavor) {
  selectedKey.value = f.key
  if (f.vcpu != null && f.memGi != null) {
    model.value = { vcpu: f.vcpu, memGi: f.memGi, gpu: f.gpu }
  } else {
    // custom flavor — keep current custom values, gpu from the flavor (0)
    model.value = { ...model.value, gpu: f.gpu }
  }
}

const isCustom = computed(() => {
  const f = props.flavors.find((x) => x.key === selectedKey.value)
  return f != null && f.vcpu == null
})

// default to the first shown flavor
watch(
  shown,
  (list) => {
    if (!selectedKey.value && list.length) pick(list[0])
  },
  { immediate: true },
)
</script>

<template>
  <div class="fp">
    <div class="cards">
      <button
        v-for="f in shown"
        :key="f.key"
        type="button"
        class="fcard"
        :class="{ on: selectedKey === f.key }"
        @click="pick(f)"
      >
        <div class="fkey">{{ f.key }}</div>
        <div class="fspec">
          <span v-if="f.gpu">{{ f.gpu }}× GPU · </span>
          <span v-if="f.vcpu != null">{{ f.vcpu }} vCPU · {{ f.memGi }} GiB</span>
          <span v-else>{{ t('flavor.custom') }}</span>
        </div>
        <div class="fdesc">{{ f.desc }}</div>
      </button>
    </div>
    <div v-if="isCustom" class="custom">
      <a-form-item :label="t('flavor.vcpu')">
        <a-input-number v-model="model.vcpu" :min="1" :max="256" :step="1" mode="button" style="width: 160px" />
      </a-form-item>
      <a-form-item :label="t('flavor.memGi')">
        <a-input-number v-model="model.memGi" :min="1" :max="2048" :step="1" mode="button" style="width: 160px" />
      </a-form-item>
    </div>
  </div>
</template>

<style scoped>
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
.fcard {
  text-align: left; cursor: pointer; padding: 12px 14px; border-radius: 10px;
  border: 1px solid var(--color-border-2); background: var(--color-bg-2);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.fcard:hover { border-color: rgb(var(--primary-5)); }
.fcard.on {
  border-color: rgb(var(--primary-6));
  box-shadow: 0 0 0 2px color-mix(in srgb, rgb(var(--primary-6)) 20%, transparent);
}
.fkey { font-weight: 700; font-size: 13px; color: var(--color-text-1); }
.fspec { font-size: 12px; color: var(--color-text-2); margin-top: 4px; }
.fdesc { font-size: 11px; color: var(--color-text-3); margin-top: 6px; }
.custom { display: flex; gap: 20px; margin-top: 12px; }
</style>
