// Kubernetes Quantity: normalize units before comparing quotas or showing cores/GiB.
export function quantity(raw: string): number {
  const match =
    /^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([numkKMGTPE]|[KMGTPE]i)?$/.exec(
      raw,
    );
  if (!match) return NaN;
  const factors: Record<string, number> = {
    n: 1e-9,
    u: 1e-6,
    m: 1e-3,
    k: 1e3,
    K: 1e3,
    M: 1e6,
    G: 1e9,
    T: 1e12,
    P: 1e15,
    E: 1e18,
    Ki: 2 ** 10,
    Mi: 2 ** 20,
    Gi: 2 ** 30,
    Ti: 2 ** 40,
    Pi: 2 ** 50,
    Ei: 2 ** 60,
  };
  const value = Number(match[1]) * (factors[match[2]] ?? 1);
  return Number.isFinite(value) && value >= 0 ? value : NaN;
}

export function quotaRatio(used: string, hard: string): number {
  const u = quantity(used),
    h = quantity(hard);
  if (!Number.isFinite(u) || !Number.isFinite(h)) return 0;
  if (h === 0) return u === 0 ? 0 : 1;
  return Math.min(1, u / h);
}
