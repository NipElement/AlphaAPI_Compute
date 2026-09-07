export type TimeWindow = { start: number; end: number };
export const MAX_RANGE_SECONDS = 90 * 86400;
export const RANGE_PRESETS = [1, 6, 24, 72, 168, 336, 720] as const;
export function rangeStep(seconds: number): number {
  // Bound each series to about 600 evaluations, even across a long window.
  return Math.max(30, Math.ceil(seconds / 600 / 30) * 30);
}
export function rangeError(
  start: number,
  end: number,
  now = Date.now() / 1000,
): "invalid" | "order" | "future" | "tooLong" | null {
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "invalid";
  if (end <= start) return "order";
  if (end > now + 60) return "future";
  if (end - start > MAX_RANGE_SECONDS) return "tooLong";
  return null;
}
export function localDateInput(seconds: number): string {
  const d = new Date(seconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
