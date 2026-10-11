// How a view's values become a lookup table for the GPU (gpu.ts): pure functions, tested in node.

import type { ValueColour } from './gpu';
import { colormapLut, type ColormapName, isDiverging, MISSING, type RGB } from './palettes';

const MISSING_RGBA: [number, number, number, number] = [...MISSING, 255];

/** 1st–99th percentile of the finite values (robust to outliers); sampled above 200 k values. */
export function robustRange(values: ArrayLike<number>): [number, number] {
  const step = Math.max(1, Math.floor(values.length / 200_000));
  const finite: number[] = [];
  for (let i = 0; i < values.length; i += step) if (Number.isFinite(values[i])) finite.push(values[i]);
  if (!finite.length) return [0, 1];
  const s = Float64Array.from(finite).sort();
  const lo = s[Math.floor(0.01 * (s.length - 1))];
  const hi = s[Math.ceil(0.99 * (s.length - 1))];
  return lo === hi ? [s[0], s[s.length - 1] === s[0] ? s[0] + 1 : s[s.length - 1]] : [lo, hi];
}

/** The range a colour map is drawn over: symmetric around 0 for diverging maps. */
export function displayRange(range: [number, number], map: ColormapName): [number, number] {
  if (!isDiverging(map)) return range;
  const m = Math.max(Math.abs(range[0]), Math.abs(range[1])) || 1;
  return [-m, m];
}

/** Categories: one entry per code; hidden categories get alpha 0. */
export function categoryLut(colours: RGB[], hidden: boolean[]): Uint8Array {
  const lut = new Uint8Array(Math.max(1, colours.length) * 4);
  colours.forEach((c, i) => lut.set([c[0], c[1], c[2], hidden[i] ? 0 : 255], i * 4));
  return lut;
}

export function categoryColouring(colours: RGB[], hidden: boolean[]): ValueColour {
  return { lut: categoryLut(colours, hidden), mode: 'categories', missing: MISSING_RGBA };
}

export function numberColouring(map: ColormapName, range: [number, number]): ValueColour {
  return { lut: colormapLut(map), mode: 'numbers', range: displayRange(range, map), missing: MISSING_RGBA };
}

/** A single colour for every point (no column chosen): one category, code 0. */
export function constantColouring(rgb: RGB): ValueColour {
  return { lut: categoryLut([rgb], [false]), mode: 'categories', missing: MISSING_RGBA };
}

/** CPU reference of the shader lookup (gpu.ts), for tests and for tooltips/legends. */
export function lookup(vc: ValueColour, v: number): [number, number, number, number] {
  const n = vc.lut.length / 4;
  let idx: number;
  if (vc.mode === 'numbers') {
    if (!Number.isFinite(v) || v < -1e38) return vc.missing;
    const [lo, hi] = vc.range ?? [0, 1];
    const t = hi - lo > 0 ? Math.min(1, Math.max(0, (v - lo) / (hi - lo))) : 0.5;
    idx = Math.floor(t * (n - 1) + 0.5);
  } else {
    if (v < -0.5 || v > n - 0.5) return vc.missing;
    idx = Math.floor(v + 0.5);
  }
  return Array.from(vc.lut.slice(idx * 4, idx * 4 + 4)) as [number, number, number, number];
}
