<script setup lang="ts">
import { onUnmounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { Message } from "@arco-design/web-vue";
import { copyText } from "@/utils/clipboard";
const props = defineProps<{ code: string; label?: string }>();
const { t } = useI18n();
const copied = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;
onUnmounted(() => clearTimeout(timer));
async function copy() {
  try {
    await copyText(props.code);
    copied.value = true;
    clearTimeout(timer);
    timer = setTimeout(() => {
      copied.value = false;
    }, 2000);
  } catch {
    Message.error(t("console.copyFailed"));
  }
}
</script>
<template>
  <div class="code-block">
    <div class="code-toolbar">
      <span>{{ label || "TERMINAL" }}</span>
      <button type="button" @click="copy">
        <icon-check v-if="copied" />
        <icon-copy v-else />
        {{ t(copied ? "console.copied" : "console.copy") }}
      </button>
    </div>
    <pre><code>{{ code }}</code></pre>
  </div>
</template>
