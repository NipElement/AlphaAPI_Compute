<script setup lang="ts">
import { computed, nextTick, onUnmounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { papi } from "@/api";
import type { Flavors } from "@/api/types";
import { useUiStore } from "@/stores/ui";
import { selectScrollbar } from "@/utils/accessibility";
import { WORKLOAD_META, type WorkloadKind } from "@/utils/workloads";
import NumberField from "./NumberField.vue";
import FlavorPicker from "./FlavorPicker.vue";

const props = defineProps<{
  kind: WorkloadKind;
  catalog: Flavors | null;
  catalogLoading: boolean;
  catalogError: string;
  remainingGpu: number | null;
}>();
const emit = defineEmits<{ created: [name: string]; retry: [] }>();
const createOpen = defineModel<boolean>("visible", { default: false });
const { t } = useI18n();
const ui = useUiStore();
const route = useRoute();
const meta = computed(() => WORKLOAD_META[props.kind]);
let alive = true;
onUnmounted(() => {
  alive = false;
});
const nameInput = ref<{ focus: () => void } | null>(null);
const creating = ref(false),
  createError = ref(""),
  attempted = ref(false);
const script =
  props.kind === "services"
    ? 'from http.server import HTTPServer, BaseHTTPRequestHandler\n\nclass Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        self.send_response(200)\n        self.send_header("Content-Type", "application/json")\n        self.end_headers()\n        self.wfile.write(b\'{"status":"ok"}\')\n\nHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()'
    : 'import time\nprint("Job started", flush=True)\ntime.sleep(3600)';
const form = reactive({
  name: "",
  image: "",
  res:
    props.kind === "jobs"
      ? { vcpu: 31, memGi: 248, gpu: 1 }
      : { vcpu: 4, memGi: 16, gpu: 0 },
  replicas: 1,
  script,
  framework: "custom",
  priority: "",
  sizeGi: 20,
  volClass: props.kind === "volumes" ? "arise-shared" : "arise-longterm",
  volume: true,
  sshKey: "",
  graceSeconds: 5,
});
const priorities = computed(() => props.catalog?.priorities[ui.tenant] ?? []);
const graceCap = computed(() => props.catalog?.grace?.capSeconds ?? 300);
const graceDefault = computed(() => props.catalog?.grace?.defaultSeconds ?? 5);
const totalReplicas = computed(() =>
  ["jobs", "services"].includes(props.kind) ? form.replicas : 1,
);
const nameError = computed(() =>
  /^[a-z](?:[a-z0-9-]{0,56}[a-z0-9])?$/.test(form.name.trim())
    ? ""
    : t("console.invalidName"),
);
const gpuExceeded = computed(
  () =>
    props.remainingGpu !== null &&
    form.res.gpu * totalReplicas.value > props.remainingGpu,
);

watch(
  () => props.catalog,
  (value) => {
    if (!value) return;
    if (!form.image)
      form.image = value.images.includes(String(route.query.image))
        ? String(route.query.image)
        : (value.images[0] ?? "");
    if (!form.priority) form.priority = priorities.value[0] ?? "";
    if (form.graceSeconds === 5 && value.grace)
      form.graceSeconds = value.grace.defaultSeconds;
  },
  { immediate: true },
);
watch(createOpen, (open) => {
  if (open) {
    attempted.value = false;
    createError.value = "";
  }
});
async function create() {
  if (creating.value) return;
  attempted.value = true;
  createError.value = "";
  if (nameError.value) {
    await nextTick();
    nameInput.value?.focus();
    return;
  }
  if (props.kind !== "volumes" && !form.image) {
    createError.value = t("console.imageRequired");
    return;
  }
  if (["jobs", "services"].includes(props.kind) && !form.script.trim()) {
    createError.value = t("console.scriptRequired");
    return;
  }
  if (
    props.kind !== "volumes" &&
    (!Number.isInteger(form.res.vcpu) ||
      !Number.isInteger(form.res.memGi) ||
      form.res.vcpu < 1 ||
      form.res.memGi < 1)
  ) {
    createError.value = `${t("flavor.vcpu")} / ${t("flavor.memGi")}`;
    return;
  }
  if (
    props.kind !== "volumes" &&
    (!Number.isInteger(form.graceSeconds) ||
      form.graceSeconds < 1 ||
      form.graceSeconds > graceCap.value)
  ) {
    createError.value = `${t("console.graceLabel")}: 1–${graceCap.value}`;
    return;
  }
  if (
    (props.kind === "volumes" ||
      (props.kind === "devmachines" && form.volume)) &&
    (!Number.isInteger(form.sizeGi) ||
      form.sizeGi < 10 ||
      form.sizeGi % 10 !== 0)
  ) {
    createError.value = t("volumes.capacityGi");
    return;
  }
  if (
    !Number.isInteger(totalReplicas.value) ||
    totalReplicas.value < 1 ||
    totalReplicas.value > 8
  ) {
    createError.value = `${t("console.instances")}: 1–8`;
    return;
  }
  creating.value = true;
  const name = form.name.trim(),
    ns = ui.tenant;
  try {
    const body: Record<string, unknown> = {
      name,
      image: form.image,
      ...form.res,
      graceSeconds: form.graceSeconds,
    };
    if (props.kind === "devmachines") {
      if (form.volume)
        body.volume = { sizeGi: form.sizeGi, class: form.volClass };
      if (form.sshKey.trim()) body.sshPublicKey = form.sshKey.trim();
      await papi.createDevmachine(ns, body);
    } else if (props.kind === "jobs")
      await papi.createJob(ns, {
        ...body,
        replicas: form.replicas,
        script: form.script,
        framework: form.framework,
        priority: form.priority,
      });
    else if (props.kind === "services")
      await papi.createService(ns, {
        ...body,
        replicas: form.replicas,
        script: form.script,
      });
    else
      await papi.createVolume(ns, {
        name,
        sizeGi: form.sizeGi,
        class: form.volClass,
      });
    if (!alive) return;
    createOpen.value = false;
    form.name = "";
    form.sshKey = "";
    form.graceSeconds = graceDefault.value;
    attempted.value = false;
    emit("created", name);
  } catch (e) {
    if (alive) createError.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (alive) creating.value = false;
  }
}
</script>
<template>
  <a-drawer
    v-model:visible="createOpen"
    :mask-closable="false"
    :esc-to-close="!creating"
    :closable="!creating"
    :width="940"
    :title="t(`console.${meta.create}`)"
    unmount-on-close
  >
    <div class="create-layout">
      <a-form
        :model="form"
        layout="vertical"
        class="create-form"
        @submit="create"
      >
        <p class="create-intro">{{ t("console.createIntro") }}</p>
        <a-alert v-if="catalogError" type="error">
          {{ catalogError }}
          <a-button
            :loading="catalogLoading"
            size="mini"
            @click="emit('retry')"
          >
            {{ t("common.refresh") }}
          </a-button>
        </a-alert>
        <section class="form-section">
          <h3>
            <span>01</span>
            {{ t("console.basic") }}
          </h3>
          <a-form-item
            :label="t('console.nameLabel')"
            required
            :validate-status="attempted && nameError ? 'error' : undefined"
            :help="attempted && nameError ? nameError : t('console.nameHelp')"
            field="name"
          >
            <a-input
              ref="nameInput"
              v-model="form.name"
              :placeholder="meta.example"
              :max-length="58"
              allow-clear
              :input-attrs="{
                'aria-label': t('console.nameLabel'),
              }"
            />
          </a-form-item>
          <a-form-item
            v-if="kind !== 'volumes'"
            :label="t('console.imageLabel')"
            required
            field="image"
          >
            <a-select
              :scrollbar="selectScrollbar"
              v-model="form.image"
              :loading="catalogLoading"
              :placeholder="t('console.imageRequired')"
            >
              <a-option
                v-for="im in catalog?.images ?? []"
                :key="im"
                :value="im"
              >
                {{ im }}
              </a-option>
            </a-select>
          </a-form-item>
        </section>
        <section v-if="kind !== 'volumes'" class="form-section">
          <h3>
            <span>02</span>
            {{ t("console.compute") }}
          </h3>
          <FlavorPicker
            v-if="catalog"
            v-model="form.res"
            :flavors="catalog.flavors"
            :gpu-only="kind === 'jobs'"
          />
          <a-skeleton v-else-if="!catalogError" :animation="true">
            <a-skeleton-line :rows="3" />
          </a-skeleton>
          <a-form-item
            v-if="kind === 'jobs' || kind === 'services'"
            :label="t('console.instances')"
            style="margin-top: 20px"
            field="replicas"
          >
            <NumberField
              v-model="form.replicas"
              :min="1"
              :max="8"
              :precision="0"
              mode="button"
              :input-attrs="{
                'aria-label': t('console.instances'),
              }"
            />
          </a-form-item>
          <a-form-item
            :label="`${t('console.graceLabel')} · ${t('console.optional')}`"
            :help="t('console.graceHelp', { def: graceDefault, cap: graceCap })"
            style="margin-top: 20px"
            field="graceSeconds"
          >
            <NumberField
              v-model="form.graceSeconds"
              :min="1"
              :max="graceCap"
              :precision="0"
              mode="button"
              :input-attrs="{ 'aria-label': t('console.graceLabel') }"
            >
              <template #suffix>s </template>
            </NumberField>
          </a-form-item>
          <a-alert v-if="gpuExceeded" type="warning" style="margin-top: 16px">
            {{
              t("console.insufficientGpu", {
                requested: form.res.gpu * totalReplicas,
                remaining: remainingGpu,
              })
            }}
          </a-alert>
        </section>
        <section
          v-if="kind === 'devmachines' || kind === 'volumes'"
          class="form-section"
        >
          <h3>
            <span>{{ kind === "volumes" ? "02" : "03" }}</span>
            {{ t("console.storageAccess") }}
          </h3>
          <a-checkbox
            v-if="kind === 'devmachines'"
            v-model="form.volume"
            style="margin-bottom: 18px"
          >
            {{ t("console.dataVolume") }}
            <code>/data</code>
          </a-checkbox>
          <template v-if="kind === 'volumes' || form.volume">
            <a-form-item
              :label="t('console.dataVolume')"
              :help="t('volumes.capacityGi')"
              field="sizeGi"
            >
              <NumberField
                v-model="form.sizeGi"
                :min="10"
                :max="10000"
                :step="10"
                :precision="0"
                mode="button"
                :input-attrs="{ 'aria-label': t('console.dataVolume') }"
              >
                <template #suffix>GiB </template>
              </NumberField>
            </a-form-item>
            <a-form-item :label="t('console.volumePolicy')" field="volClass">
              <a-radio-group
                v-model="form.volClass"
                direction="vertical"
                class="policy-options"
              >
                <a-radio value="arise-longterm">
                  <strong>{{ t("console.retained") }}</strong>
                  <small>{{ t("console.retainedHelp") }}</small>
                </a-radio>
                <a-radio value="arise-shared">
                  <strong>{{ t("console.disposable") }}</strong>
                  <small>{{ t("console.disposableHelp") }}</small>
                </a-radio>
              </a-radio-group>
            </a-form-item>
          </template>
          <template v-if="kind === 'devmachines'">
            <a-form-item
              :label="`${t('console.sshLabel')} · ${t('console.optional')}`"
              :help="t('console.sshHelp')"
              field="sshKey"
            >
              <a-textarea
                v-model="form.sshKey"
                placeholder="ssh-ed25519 AAAA…"
                :auto-size="{ minRows: 3, maxRows: 5 }"
                :textarea-attrs="{ 'aria-label': t('console.sshLabel') }"
              />
            </a-form-item>
            <p class="section-note">
              {{
                form.sshKey.trim()
                  ? t("console.storageHint")
                  : t("console.sshOff")
              }}
            </p>
          </template>
        </section>
        <section
          v-if="kind === 'jobs' || kind === 'services'"
          class="form-section"
        >
          <h3>
            <span>03</span>
            {{ t("console.application") }}
          </h3>
          <div v-if="kind === 'jobs'" class="form-two-cols">
            <a-form-item :label="t('jobs.framework')" field="framework">
              <a-select
                :scrollbar="selectScrollbar"
                v-model="form.framework"
                :placeholder="t('jobs.framework')"
              >
                <a-option
                  v-for="f in ['custom', 'pytorch-ddp', 'mpi', 'tensorflow-ps']"
                  :key="f"
                >
                  {{ f }}
                </a-option>
              </a-select>
            </a-form-item>
            <a-form-item :label="t('jobs.priority')" field="priority">
              <a-select
                :scrollbar="selectScrollbar"
                v-model="form.priority"
                :placeholder="t('jobs.priority')"
              >
                <a-option v-for="p in priorities" :key="p">{{ p }}</a-option>
              </a-select>
            </a-form-item>
          </div>
          <a-form-item
            :label="t(kind === 'jobs' ? 'jobs.entrypoint' : 'services.script')"
            required
            field="script"
          >
            <a-textarea
              v-model="form.script"
              class="script-editor"
              :auto-size="{ minRows: 7, maxRows: 18 }"
              :textarea-attrs="{
                'aria-label': t(
                  kind === 'jobs' ? 'jobs.entrypoint' : 'services.script',
                ),
              }"
            />
          </a-form-item>
        </section>
      </a-form>
      <aside class="configuration">
        <div class="config-card">
          <span class="config-icon">
            <component :is="meta.icon" :size="23" />
          </span>
          <h3>{{ t("console.summary") }}</h3>
          <p class="config-name">{{ form.name || meta.example }}</p>
          <div class="config-line">
            <span>{{ t("console.targetWorkspace") }}</span>
            <strong>{{ ui.tenant }}</strong>
          </div>
          <template v-if="kind !== 'volumes'">
            <div class="config-line">
              <span>{{ t("console.imageLabel") }}</span>
              <strong>{{ form.image || "—" }}</strong>
            </div>
            <div class="config-line">
              <span>{{ t("console.instances") }}</span>
              <strong>{{ totalReplicas }}</strong>
            </div>
            <div class="config-totals">
              <span class="eyebrow">{{ t("console.aggregate") }}</span>
              <div v-if="form.res.gpu">
                <b>{{ form.res.gpu * totalReplicas }}</b>
                <span>GPU</span>
              </div>
              <div>
                <b>{{ form.res.vcpu * totalReplicas }}</b>
                <span>vCPU</span>
              </div>
              <div>
                <b>{{ form.res.memGi * totalReplicas }}</b>
                <span>GiB RAM</span>
              </div>
            </div>
          </template>
          <div
            v-if="kind === 'volumes' || kind === 'devmachines'"
            class="config-line"
          >
            <span>{{ t("console.dataVolume") }}</span>
            <strong>
              {{
                kind === "volumes" || form.volume ? `${form.sizeGi} GiB` : "—"
              }}
            </strong>
          </div>
          <p class="hint">{{ t("console.createBilling") }}</p>
        </div>
        <p class="config-footnote">
          <icon-clock-circle />
          {{ t("console.creationTime") }}
        </p>
      </aside>
    </div>
    <template #footer>
      <a-alert
        v-if="createError"
        type="error"
        style="margin-bottom: 14px; text-align: left"
        role="alert"
      >
        {{ createError }}
      </a-alert>
      <div class="create-footer">
        <span>{{ t("console.reviewConfig") }}</span>
        <a-button :disabled="creating" @click="createOpen = false">
          {{ t("common.cancel") }}
        </a-button>
        <a-button
          type="primary"
          :loading="creating"
          :disabled="
            kind !== 'volumes' &&
            (!catalog || !!catalogError || !catalog.images.length)
          "
          data-action="submit-resource"
          @click="create"
        >
          {{ t(`console.${meta.create}`) }}
          <icon-arrow-right />
        </a-button>
      </div>
    </template>
  </a-drawer>
</template>

<style scoped>
.create-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 250px;
  gap: 34px;
}
.create-intro {
  font-size: var(--text-sm);
  color: var(--muted);
  margin: 0 0 25px;
  line-height: 1.8;
}
.form-section {
  padding-bottom: 24px;
  margin-bottom: 24px;
  border-bottom: 1px solid var(--line);
}
.form-section h3 {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: var(--text-base);
  margin: 0 0 22px;
}
.form-section h3 > span {
  font-size: var(--text-xs);
  font-weight: 500;
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 1px solid var(--line);
  border-radius: 7px;
  color: var(--muted);
}
.form-section :deep(.arco-form-item:last-child) {
  margin-bottom: 0;
}
.form-two-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.policy-options {
  width: 100%;
}
.policy-options :deep(.arco-radio) {
  padding: 13px;
  border: 1px solid var(--line);
  border-radius: 8px;
  width: 100%;
  margin-bottom: 8px;
  align-items: flex-start;
}
.policy-options :deep(.arco-radio-checked) {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.policy-options strong {
  font-size: var(--text-sm);
  font-weight: 500;
  display: block;
  margin-bottom: 5px;
}
.policy-options small {
  display: block;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.8;
}
.script-editor :deep(textarea) {
  font-family: "SFMono-Regular", Consolas, monospace;
  font-size: var(--text-sm);
  line-height: 1.8;
}
.configuration {
  min-width: 0;
}
.config-card {
  position: sticky;
  top: 0;
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 24px 20px;
}
.config-icon {
  color: var(--accent);
}
.config-card h3 {
  font-size: var(--text-lg);
  margin: 15px 0 8px;
}
.config-name {
  font-size: var(--text-sm);
  color: var(--muted);
  word-break: break-all;
  margin-bottom: 25px;
}
.config-line {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 11px 0;
  font-size: var(--text-xs);
  color: var(--muted);
  border-bottom: 1px solid var(--line);
  line-height: 1.5;
}
.config-line strong {
  font-weight: 500;
  color: var(--ink);
  text-align: right;
  overflow-wrap: anywhere;
}
.config-totals {
  margin: 24px 0;
}
.config-totals > div {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 14px;
}
.config-totals b {
  font-size: 1.625rem;
  font-weight: 600;
  letter-spacing: -1px;
}
.config-totals > div span {
  font-size: var(--text-sm);
  color: var(--muted);
}
.config-card .hint {
  font-size: var(--text-xs);
  margin: 20px 0 0;
}
.config-footnote {
  display: flex;
  gap: 8px;
  margin: 15px 4px;
  font-size: var(--text-xs);
  line-height: 1.8;
  color: var(--muted);
}
.config-footnote svg {
  flex: none;
  margin-top: 4px;
}
.create-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
}
.create-footer > span {
  margin-right: auto;
  color: var(--muted);
  font-size: var(--text-sm);
}
.create-error {
  margin-bottom: 20px;
}
@media (max-width: 767px) {
  .create-layout {
    grid-template-columns: 1fr;
    gap: 0;
  }
  .configuration {
    order: -1;
    margin-bottom: 25px;
  }
  .config-card {
    padding: 18px;
  }
  .config-card h3 {
    display: inline;
    margin-left: 10px;
  }
  .config-name,
  .config-card .hint,
  .config-footnote,
  .config-card .config-line {
    display: none;
  }
  .config-totals {
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
    margin: 12px 0 0;
    align-items: center;
  }
  .config-totals .eyebrow {
    display: none;
  }
  .config-totals > div {
    margin: 0;
    gap: 5px;
  }
  .config-totals b {
    font-size: 1.375rem;
  }
  .config-totals > div span {
    font-size: var(--text-xs);
  }
  .create-footer > span {
    display: none;
  }
  .form-two-cols {
    grid-template-columns: 1fr;
  }
}
</style>
