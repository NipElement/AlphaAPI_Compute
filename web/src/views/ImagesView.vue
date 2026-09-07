<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { papi } from "@/api";
import type { Flavors } from "@/api/types";
import PageHeading from "@/components/PageHeading.vue";
import EmptyState from "@/components/EmptyState.vue";
const { t } = useI18n(),
  router = useRouter();
const flavors = ref<Flavors | null>(null),
  loadError = ref(""),
  loading = ref(true),
  search = ref("");
const images = computed(() =>
  (flavors.value?.images ?? []).filter((im) =>
    im.toLowerCase().includes(search.value.toLowerCase().trim()),
  ),
);
async function load() {
  loading.value = true;
  loadError.value = "";
  try {
    flavors.value = await papi.flavors();
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
function launch(route: string, image: string) {
  router.push({ name: route, query: { create: "1", image } });
}
</script>
<template>
  <div class="page">
    <PageHeading
      :title="t('nav.images')"
      :description="t('console.imagesDesc')"
    >
      <template #actions>
        <a-button :loading="loading" @click="load">
          <template #icon><icon-refresh /></template>
          {{ t("common.refresh") }}
        </a-button>
      </template>
    </PageHeading>
    <a-alert v-if="loadError" type="error" class="page-error">
      {{ loadError }}
    </a-alert>
    <div class="runtime-toolbar">
      <span>
        {{ t("console.environment") }}
        <b>{{ flavors?.images.length ?? 0 }}</b>
      </span>
      <a-input
        v-model="search"
        allow-clear
        :placeholder="t('common.search')"
        :input-attrs="{ 'aria-label': t('common.search') }"
      >
        <template #prefix><icon-search /></template>
      </a-input>
    </div>
    <a-spin :loading="loading" style="display: block">
      <div v-if="images.length" class="runtime-grid">
        <article v-for="im in images" :key="im" class="runtime-card">
          <div class="runtime-top">
            <div class="runtime-symbol"><icon-code :size="27" /></div>
            <span>{{ t("console.approved") }}</span>
          </div>
          <h2>{{ im }}</h2>
          <p>{{ t("console.runtimeHint") }}</p>
          <div class="runtime-type">
            <icon-layers />
            {{ t("console.environment") }}
          </div>
          <div class="runtime-actions">
            <a-button type="primary" @click="launch('dev', im)">
              {{ t("console.useEnvironment") }}
              <icon-arrow-right />
            </a-button>
            <a-dropdown trigger="click" position="br">
              <a-button :aria-label="`${t('console.actions')}: ${im}`">
                <icon-more />
              </a-button>
              <template #content>
                <a-doption @click="launch('jobs', im)">
                  {{ t("console.createJob") }}
                </a-doption>
                <a-doption @click="launch('services', im)">
                  {{ t("console.createService") }}
                </a-doption>
              </template>
            </a-dropdown>
          </div>
        </article>
      </div>
      <section v-else-if="!loading && !loadError" class="panel">
        <EmptyState
          :title="t(search ? 'console.emptyFiltered' : 'console.noImages')"
          :description="
            t(search ? 'console.emptyFilteredDesc' : 'console.noImagesDesc')
          "
          icon="icon-layers"
        >
          <a-button v-if="search" @click="search = ''">
            {{ t("console.clearFilters") }}
          </a-button>
        </EmptyState>
      </section>
    </a-spin>
  </div>
</template>
<style scoped>
.runtime-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0;
  gap: 20px;
}
.runtime-toolbar > span {
  font-size: var(--text-sm);
  color: var(--muted);
}
.runtime-toolbar b {
  font-size: var(--text-xs);
  font-weight: 500;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 3px 7px;
  margin-left: 8px;
}
.runtime-toolbar :deep(.arco-input-wrapper) {
  max-width: 280px;
}
.runtime-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  gap: 22px;
}
.runtime-card {
  padding: 26px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
  max-width: 440px;
}
.runtime-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
.runtime-symbol {
  width: 54px;
  height: 54px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
  border-radius: 12px;
}
.runtime-top > span {
  font-size: var(--text-xs);
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 4px 7px;
}
.runtime-card h2 {
  font-size: 1.1875rem;
  font-weight: 600;
  letter-spacing: -0.3px;
  margin: 25px 0 13px;
  overflow-wrap: anywhere;
}
.runtime-card p {
  font-size: var(--text-sm);
  line-height: 1.9;
  color: var(--muted);
  margin: 0;
}
.runtime-type {
  display: flex;
  gap: 7px;
  align-items: center;
  color: var(--muted);
  font-size: var(--text-xs);
  margin: 24px 0;
}
.runtime-actions {
  display: flex;
  gap: 8px;
  border-top: 1px solid var(--line);
  padding-top: 20px;
}
.runtime-actions > :first-child {
  flex: 1;
  justify-content: space-between;
}
@media (max-width: 600px) {
  .runtime-toolbar {
    align-items: flex-start;
    flex-direction: column;
    gap: 14px;
  }
  .runtime-toolbar :deep(.arco-input-wrapper) {
    max-width: none;
  }
  .runtime-grid {
    grid-template-columns: 1fr;
  }
  .runtime-card {
    max-width: none;
  }
}
</style>
