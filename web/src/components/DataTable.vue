<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { TableData } from "@arco-design/web-vue";
import Pagination from "./Pagination.vue";
defineOptions({ inheritAttrs: false });
const props = withDefaults(
  defineProps<{
    data: TableData[];
    pagination?: false | { pageSize?: number; hideOnSinglePage?: boolean };
  }>(),
  { pagination: () => ({ pageSize: 15, hideOnSinglePage: true }) },
);
const { t } = useI18n();
const current = ref(1);
const pageSize = ref(props.pagination ? (props.pagination.pageSize ?? 15) : 15);
const sizes = computed(() =>
  [...new Set([pageSize.value, 10, 25, 50])].sort((a, b) => a - b),
);
const paginated = computed(() =>
  props.pagination
    ? props.data.slice(
        (current.value - 1) * pageSize.value,
        current.value * pageSize.value,
      )
    : props.data,
);
watch(
  () => props.data.length,
  (n) => {
    current.value = Math.min(
      current.value,
      Math.max(1, Math.ceil(n / pageSize.value)),
    );
  },
);
function setSize(event: Event) {
  pageSize.value = Number((event.target as HTMLSelectElement).value);
  current.value = 1;
}
</script>
<template>
  <div class="data-table">
    <a-table v-bind="$attrs" :data="paginated" :pagination="false">
      <template v-for="(_, name) in $slots" #[name]="slotData">
        <slot :name="name" v-bind="slotData || {}" />
      </template>
    </a-table>
    <div
      v-if="
        pagination &&
        (data.length > pageSize ||
          !pagination.hideOnSinglePage ||
          (pageSize !== pagination.pageSize && data.length > 0))
      "
      class="table-pagination"
    >
      <div class="pagination-info">
        <span>
          {{
            t("pagination.showing", {
              start: data.length ? (current - 1) * pageSize + 1 : 0,
              end: Math.min(current * pageSize, data.length),
              total: data.length,
            })
          }}
        </span>
        <select
          :value="pageSize"
          :aria-label="t('pagination.pageSize')"
          @change="setSize"
        >
          <option v-for="size in sizes" :key="size" :value="size">
            {{ t("pagination.perPage", { n: size }) }}
          </option>
        </select>
      </div>
      <Pagination
        v-model:current="current"
        :total="data.length"
        :page-size="pageSize"
      />
    </div>
  </div>
</template>
<style scoped>
.data-table {
  min-width: 0;
  max-width: 100%;
}
.table-pagination {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  padding: 20px;
  border-top: 1px solid var(--line);
}
.pagination-info {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  font-size: var(--text-sm);
  color: var(--muted);
}
.pagination-info select {
  font-size: var(--text-sm);
  background: var(--surface);
  color: var(--ink);
  padding: 6px 10px;
  border: 1px solid var(--control-border);
  border-radius: 7px;
  min-height: 36px;
}
@media (max-width: 767px) {
  .table-pagination {
    padding: 16px;
  }
}
</style>
