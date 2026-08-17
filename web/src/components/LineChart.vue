<script setup lang="ts">
import { computed } from 'vue'

// Lightweight dependency-free line chart (Arco ships no chart lib). Points are
// [unixSeconds, value]. Renders an area + line + last-value label, theme-aware
// via CSS custom properties.
const props = defineProps<{
  points: Array<[number, number]>
  color?: string
  unit?: string
  ymax?: number | null
}>()

const W = 560
const H = 150
const P = { t: 12, r: 12, b: 22, l: 38 }

const view = computed(() => {
  const pts = props.points
  if (!pts.length) return null
  const xs = pts.map((p) => p[0])
  const ys = pts.map((p) => p[1])
  const x0 = Math.min(...xs)
  const x1 = Math.max(...xs)
  const yM = props.ymax != null ? props.ymax : Math.max(1, Math.max(...ys) * 1.15)
  const X = (t: number) => P.l + ((W - P.l - P.r) * (t - x0)) / Math.max(1, x1 - x0)
  const Y = (v: number) => H - P.b - ((H - P.t - P.b) * v) / yM
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join(' ')
  const area = `${line} L${X(x1).toFixed(1)} ${H - P.b} L${X(x0).toFixed(1)} ${H - P.b} Z`
  const ticks = [0, 0.5, 1].map((f) => ({ v: Math.round(yM * f), y: Y(yM * f) }))
  const fmt = (s: number) => {
    const d = new Date(s * 1000)
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  }
  const last = pts[pts.length - 1]
  return { line, area, ticks, last: { x: X(last[0]), y: Y(last[1]), v: last[1] }, t0: fmt(x0), t1: fmt(x1) }
})

const stroke = computed(() => props.color || 'rgb(var(--primary-6))')
</script>

<template>
  <div class="chart">
    <svg v-if="view" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" role="img">
      <g v-for="tk in view.ticks" :key="tk.y">
        <line :x1="P.l" :x2="W - P.r" :y1="tk.y" :y2="tk.y" stroke="var(--color-border-2)" />
        <text :x="P.l - 6" :y="tk.y + 4" text-anchor="end" font-size="10" fill="var(--color-text-3)">{{ tk.v }}</text>
      </g>
      <path :d="view.area" :fill="stroke" opacity="0.12" />
      <path :d="view.line" fill="none" :stroke="stroke" stroke-width="2" stroke-linejoin="round" />
      <circle :cx="view.last.x" :cy="view.last.y" r="3.5" :fill="stroke" stroke="var(--color-bg-2)" stroke-width="2" />
      <text :x="P.l" :y="H - 6" font-size="10" fill="var(--color-text-3)">{{ view.t0 }}</text>
      <text :x="W - P.r" :y="H - 6" text-anchor="end" font-size="10" fill="var(--color-text-3)">{{ view.t1 }}</text>
    </svg>
    <div v-else class="empty">—</div>
    <div v-if="view" class="last">{{ view.last.v }}{{ unit || '' }}</div>
  </div>
</template>

<style scoped>
.chart { position: relative; width: 100%; }
.chart svg { width: 100%; height: 150px; display: block; }
.empty { height: 150px; display: grid; place-items: center; color: var(--color-text-3); }
.last { position: absolute; top: 6px; right: 10px; font-size: 12px; font-weight: 600; color: var(--color-text-2); }
</style>
