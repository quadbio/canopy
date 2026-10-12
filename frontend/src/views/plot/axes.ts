// Axis helpers for the Plot view: "nice" tick values and short labels.

/** About `count` round tick values covering [lo, hi] (1, 2 or 5 × a power of ten apart). */
export function niceTicks(lo: number, hi: number, count = 5): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (hi < lo) [lo, hi] = [hi, lo];
  if (hi === lo) return [lo];
  const raw = (hi - lo) / Math.max(count, 1);
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * p).find((s) => s >= raw) ?? 10 * p;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}

/** A short label for a tick: 1.2k, 35M, 0.05, 1e-4. */
export function tickLabel(v: number): string {
  const a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1e6) return `${+(v / 1e6).toPrecision(3)}M`;
  if (a >= 1e4) return `${+(v / 1e3).toPrecision(3)}k`;
  if (a >= 1e-3) return String(+v.toPrecision(3));
  return v.toExponential(0);
}

/** Values on a log1p axis: the tick positions (in log1p units) for round values. */
export function log1pTicks(maxLog: number): { at: number; label: string }[] {
  const out: { at: number; label: string; v: number }[] = [{ at: 0, label: '0', v: 0 }];
  for (const v of [1, 2, 5, 10, 20, 50, 100, 200, 500, 1e3, 2e3, 5e3, 1e4, 1e5, 1e6]) {
    const at = Math.log1p(v);
    if (at > maxLog * 1.0001) break;
    out.push({ at, label: tickLabel(v), v });
  }
  // many: keep 0 and the powers of ten
  const kept = out.length > 6 ? out.filter((t) => t.v === 0 || Number.isInteger(Math.log10(t.v))) : out;
  return kept.map(({ at, label }) => ({ at, label }));
}
