<script setup lang="ts">
import NumberField from "./NumberField.vue";
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import type { WorkloadRow } from "@/api/types";
import clientSource from "../../../services/ssh-bastion/client.py?raw";
import { downloadText } from "@/utils/clipboard";
import CodeBlock from "./CodeBlock.vue";
const props = defineProps<{
  resource: WorkloadRow | null;
  ns: string;
  kind: string;
}>();
const visible = defineModel<boolean>("visible", { default: false });
const { t } = useI18n();
const host = ref(""),
  port = ref(2222);
const validHost = computed(
  () =>
    host.value.length <= 253 &&
    /^[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$/.test(host.value) &&
    host.value
      .split(".")
      .every(
        (label) =>
          label.length <= 63 &&
          !label.startsWith("-") &&
          !label.endsWith("-") &&
          label.length > 0,
      ),
);
const command = computed(() => {
  if (
    !validHost.value ||
    !props.resource ||
    !Number.isInteger(port.value) ||
    port.value < 1 ||
    port.value > 65535
  )
    return "";
  const name = props.resource.name;
  const base = `python3 ./arise-client.py ${props.kind === "services" ? "service" : "proxy"} ${name} --host ${host.value} --port ${port.value} --key ~/.ssh/id_ed25519 --known-hosts ~/.ssh/arise_known_hosts`;
  if (props.kind === "services") return `${base} --local-port 8080`;
  return `ssh -i ~/.ssh/id_ed25519 \\\n  -o IdentitiesOnly=yes \\\n  -o StrictHostKeyChecking=yes \\\n  -o UserKnownHostsFile=~/.ssh/arise_known_hosts \\\n  -o HostKeyAlias=${name}.${props.ns} \\\n  -o 'ProxyCommand=${base}' \\\n  dev@${name}`;
});
</script>
<template>
  <a-drawer
    v-model:visible="visible"
    :title="t('console.connection')"
    :width="660"
    :footer="false"
    unmount-on-close
  >
    <div class="connection-resource">
      <span class="resource-symbol"><icon-link /></span>
      <div>
        <strong>{{ resource?.name }}</strong>
        <p class="resource-sub">{{ ns }}</p>
      </div>
    </div>
    <a-alert v-if="kind === 'devmachines' && !resource?.ssh" type="warning">
      {{ t("console.missingSsh") }}
    </a-alert>
    <template v-else>
      <div class="private-address">
        <span class="eyebrow">{{ t("console.privateEndpoint") }}</span>
        <code>
          {{
            kind === "services"
              ? resource?.endpoint
              : `${resource?.ssh?.user}@${resource?.ssh?.service}:${resource?.ssh?.port}`
          }}
        </code>
        <p class="hint">{{ t("console.internalOnly") }}</p>
      </div>
      <ol class="access-steps">
        <li>
          <span class="step-no">1</span>
          <div>
            <h3>{{ t("console.accessStep1") }}</h3>
            <p>{{ t("console.accessStep1Desc") }}</p>
            <a-form layout="vertical" :model="{ host, port }">
              <a-form-item
                :label="t('console.gatewayHost')"
                :validate-status="host && !validHost ? 'error' : undefined"
                :help="host && !validHost ? t('console.hostRequired') : ''"
                field="host"
              >
                <a-input
                  v-model="host"
                  placeholder="ssh.example.com"
                  :input-attrs="{
                    'aria-label': t('console.gatewayHost'),
                  }"
                />
              </a-form-item>
              <a-form-item label="SSH port" field="port">
                <NumberField
                  v-model="port"
                  :min="1"
                  :max="65535"
                  :precision="0"
                  :input-attrs="{ 'aria-label': 'SSH port' }"
                />
              </a-form-item>
            </a-form>
            <p>{{ t("console.gatewayHelp") }}</p>
          </div>
        </li>
        <li>
          <span class="step-no">2</span>
          <div>
            <h3>{{ t("console.accessStep2") }}</h3>
            <p>{{ t("console.accessStep2Desc") }}</p>
            <a-button
              @click="
                downloadText('arise-client.py', clientSource, 'text/x-python')
              "
            >
              <template #icon><icon-download /></template>
              {{ t("console.downloadClient") }}
            </a-button>
          </div>
        </li>
        <li>
          <span class="step-no">3</span>
          <div>
            <h3>{{ t("console.accessStep3") }}</h3>
            <p>{{ t("console.accessStep3Desc") }}</p>
            <p v-if="kind !== 'services'" class="hint">
              HostKeyAlias:
              <code>{{ resource?.name }}.{{ ns }}</code>
            </p>
            <CodeBlock v-if="command" :code="command" label="TERMINAL" />
            <div v-else class="section-note">
              {{ t("console.hostRequired") }}
            </div>
            <p v-if="kind === 'services'">{{ t("console.serviceTunnel") }}</p>
          </div>
        </li>
      </ol>
    </template>
  </a-drawer>
</template>
<style scoped>
.connection-resource {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 26px;
}
.connection-resource strong {
  font-size: 1.125rem;
}
.private-address {
  padding: 18px;
  background: var(--surface-soft);
  border: 1px solid var(--line);
  border-radius: 9px;
}
.private-address code {
  display: block;
  margin-top: 10px;
  word-break: break-all;
  font-size: var(--text-sm);
}
.private-address p {
  margin-bottom: 0;
}
.access-steps {
  list-style: none;
  padding: 0;
  margin: 30px 0;
}
.access-steps li {
  display: flex;
  gap: 16px;
  margin: 0 0 28px;
}
.access-steps li > div {
  min-width: 0;
  flex: 1;
}
.step-no {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 50%;
  font-size: var(--text-sm);
  flex: none;
}
.access-steps h3 {
  font-size: var(--text-base);
  margin: 4px 0 10px;
}
.access-steps p {
  font-size: var(--text-sm);
  line-height: 1.8;
  color: var(--muted);
}
.access-steps :deep(.arco-form) {
  display: grid;
  grid-template-columns: 1fr 110px;
  gap: 12px;
  margin-top: 18px;
}
.access-steps :deep(.arco-form-item) {
  margin-bottom: 0;
}
</style>
