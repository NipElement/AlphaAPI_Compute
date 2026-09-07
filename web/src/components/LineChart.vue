<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { TimeWindow } from "@/utils/monitoring";
const props = defineProps<{
  points: Array<[number, number]>;
  color?: string;
  unit?: string;
  ymax?: number | null;
  label?: string;
  window?: TimeWindow;
  step?: number;
}>();
const { t, locale } = useI18n();
const host = ref<HTMLElement>();
const width = ref(600),
  H = 240,
  P = { t: 18, r: 24, b: 36, l: 48 },
  hover = ref<number | null>(null);
let observer: ResizeObserver | undefined;
onMounted(() => {
  observer = new ResizeObserver((entries) => {
    width.value = Math.max(220, entries[0].contentRect.width);
  });
  if (host.value) observer.observe(host.value);
});
onUnmounted(() => observer?.disconnect());
watch(
  () => props.window,
  () => {
    hover.value = null;
  },
);
const points = computed(() =>
  [...props.points]
    .filter((p) => Number.isFinite(p[0]))
    .sort((a, b) => a[0] - b[0]),
);
const dateLocale = computed(() => (locale.value === "zh" ? "zh-CN" : "en-US"));
const view = computed(() => {
  const pts = points.value,
    finite = pts.filter((p) => Number.isFinite(p[1]));
  if (!finite.length) return null;
  const W = width.value;
  const x0 = props.window?.start ?? pts[0][0],
    x1 = props.window?.end ?? pts[pts.length - 1][0];
  const max = Math.max(1, ...finite.map((p) => p[1])),
    min = Math.min(0, ...finite.map((p) => p[1]));
  const scale = 10 ** Math.floor(Math.log10(max - min));
  const yMax = props.ymax ?? Math.ceil((max * 1.1) / scale) * scale,
    yMin = Math.floor(min / scale) * scale;
  const X = (v: number) =>
    P.l + ((W - P.l - P.r) * (v - x0)) / Math.max(1, x1 - x0);
  const Y = (v: number) =>
    H - P.b - ((H - P.t - P.b) * (v - yMin)) / Math.max(1, yMax - yMin);
  let line = "",
    area = "",
    segment: Array<[number, number]> = [];
  const flush = () => {
    if (!segment.length) return;
    const path = segment
      .map(
        (p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(2)},${Y(p[1]).toFixed(2)}`,
      )
      .join(" ");
    line += path + " ";
    area += `${path} L${X(segment[segment.length - 1][0])},${H - P.b} L${X(segment[0][0])},${H - P.b} Z `;
    segment = [];
  };
  for (const p of pts) {
    if (
      segment.length &&
      props.step &&
      p[0] - segment[segment.length - 1][0] > props.step * 1.5
    )
      flush();
    if (Number.isFinite(p[1])) segment.push(p);
    else flush();
  }
  flush();
  const selected =
    hover.value === null
      ? finite[finite.length - 1]
      : pts[Math.min(hover.value, pts.length - 1)];
  const valid = Number.isFinite(selected[1]);
  const multiDay = x1 - x0 >= 86400;
  const tickFormat = (value: number) =>
    new Date(value * 1000).toLocaleString(
      dateLocale.value,
      multiDay
        ? {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          }
        : { hour: "2-digit", minute: "2-digit", hour12: false },
    );
  const times = (
    W >= 700 ? [0, 0.25, 0.5, 0.75, 1] : W >= 420 ? [0, 0.5, 1] : [0, 1]
  ).map((f, i, all) => ({
    x: X(x0 + (x1 - x0) * f),
    text: tickFormat(x0 + (x1 - x0) * f),
    anchor: i === 0 ? "start" : i === all.length - 1 ? "end" : "middle",
  }));
  return {
    line,
    area,
    times,
    ticks: [0, 0.25, 0.5, 0.75, 1].map((f) => ({
      v: Number((yMin + (yMax - yMin) * f).toFixed(2)),
      y: Y(yMin + (yMax - yMin) * f),
    })),
    selected: {
      x: X(selected[0]),
      y: valid ? Y(selected[1]) : 0,
      value: valid ? Number(selected[1].toFixed(2)) : "—",
      time: new Date(selected[0] * 1000).toLocaleString(dateLocale.value, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }),
      valid,
    },
  };
});
function move(event: PointerEvent) {
  const rect = (event.currentTarget as SVGElement).getBoundingClientRect();
  const x0 = props.window?.start ?? points.value[0][0],
    x1 = props.window?.end ?? points.value[points.value.length - 1][0];
  const target =
    x0 +
    ((x1 - x0) * (event.clientX - rect.left - P.l)) / (width.value - P.l - P.r);
  let best = 0;
  points.value.forEach((p, i) => {
    if (Math.abs(p[0] - target) < Math.abs(points.value[best][0] - target))
      best = i;
  });
  hover.value = best;
}
function key(event: KeyboardEvent) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  hover.value =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? points.value.length - 1
        : Math.max(
            0,
            Math.min(
              points.value.length - 1,
              (hover.value ?? points.value.length - 1) +
                (event.key === "ArrowRight" ? 1 : -1),
            ),
          );
}
const stroke = computed(() => props.color || "var(--accent)");
</script>
<template>
  <div class="chart" ref="host">
    <div v-if="view" class="chart-value">
      <strong>
        {{ view.selected.value }}
        <span>{{ unit }}</span>
      </strong>
      <span>{{ view.selected.time }}</span>
    </div>
    <svg
      v-if="view"
      :viewBox="`0 0 ${width} ${H}`"
      role="img"
      tabindex="0"
      :aria-label="`${label ?? ''}: ${view.selected.value}${unit ?? ''}, ${view.selected.time}`"
      @pointermove="move"
      @pointerleave="hover = null"
      @keydown="key"
    >
      <g v-for="tick in view.ticks" :key="tick.y">
        <line
          :x1="P.l"
          :x2="width - P.r"
          :y1="tick.y"
          :y2="tick.y"
          stroke="var(--line)"
          stroke-dasharray="3 5"
        />
        <text
          :x="P.l - 10"
          :y="tick.y + 4"
          text-anchor="end"
          font-size="12"
          fill="var(--muted)"
        >
          {{ tick.v }}
        </text>
      </g>
      <path :d="view.area" :fill="stroke" opacity=".07" />
      <path
        :d="view.line"
        fill="none"
        :stroke="stroke"
        stroke-width="2"
        stroke-linejoin="round"
      />
      <line
        v-if="hover !== null"
        :x1="view.selected.x"
        :x2="view.selected.x"
        :y1="P.t"
        :y2="H - P.b"
        stroke="var(--muted)"
        stroke-dasharray="3 4"
        opacity=".7"
      />
      <circle
        v-if="view.selected.valid"
        :cx="view.selected.x"
        :cy="view.selected.y"
        r="4"
        :fill="stroke"
        stroke="var(--surface)"
        stroke-width="2"
      />
      <text
        v-for="time in view.times"
        :key="time.x"
        :x="time.x"
        :y="H - 10"
        :text-anchor="time.anchor"
        font-size="12"
        fill="var(--muted)"
      >
        {{ time.text }}
      </text>
    </svg>
    <div v-else class="chart-empty">
      <icon-bar-chart :size="28" />
      <strong>{{ t("monitoring.noSeries") }}</strong>
      <span>{{ t("monitoring.emptyHelp") }}</span>
    </div>
  </div>
</template>
<style scoped>
.chart {
  width: 100%;
  min-width: 0;
  padding-top: 4px;
}
.chart-value {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
  min-height: 42px;
  margin: 0 0 16px;
}
.chart-value > strong {
  font-size: 1.875rem;
  font-weight: 600;
}
.chart-value > strong > span {
  font-size: var(--text-sm);
  color: var(--muted);
  margin-left: 6px;
  font-weight: 400;
}
.chart-value > span {
  font-size: var(--text-sm);
  color: var(--muted);
}
.chart svg {
  display: block;
  width: 100%;
  height: 240px;
  touch-action: pan-y;
  outline-offset: 3px;
  border-radius: 4px;
}
.chart-empty {
  min-height: 294px;
  padding: 24px 12px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  align-items: center;
  justify-content: center;
  color: var(--muted);
  font-size: var(--text-sm);
  text-align: center;
}
.chart-empty strong {
  color: var(--ink);
  font-size: var(--text-base);
}
</style>
