<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
const props = defineProps<{ status?: string | null; label?: string }>();
const { t } = useI18n();
const state = computed(() => {
  const key = (props.status || "").toLowerCase();
  if (["running", "ready", "bound", "available", "healthy"].includes(key))
    return {
      tone: "green",
      label:
        key === "bound"
          ? "statusBound"
          : key === "running"
            ? "statusRunning"
            : "statusReady",
    };
  if (
    ["failed", "error", "notready", "quarantined", "critical", "p0"].includes(
      key,
    )
  )
    return {
      tone: "red",
      label: key === "notready" ? "statusNotReady" : "statusFailed",
    };
  if (
    ["pending", "queued", "creating", "restarting", "p1", "warning"].includes(
      key,
    )
  )
    return { tone: "amber", label: "statusPending" };
  if (["succeeded", "completed", "finished"].includes(key))
    return { tone: "blue", label: "statusCompleted" };
  if (["terminating", "deleting"].includes(key))
    return { tone: "gray", label: "statusTerminating" };
  if (key === "released") return { tone: "gray", label: "statusReleased" };
  if (key === "suspended") return { tone: "gray", label: "statusSuspended" };
  return { tone: "gray", label: "" };
});
</script>
<template>
  <span class="status-badge" :class="state.tone">
    <i />
    <span>
      {{
        label ||
        (state.label
          ? t(`console.${state.label}`)
          : status || t("console.statusUnknown"))
      }}
    </span>
  </span>
</template>
