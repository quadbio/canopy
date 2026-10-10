// Per-point RGBA colours. Pure functions: run in the colour Web Worker, tested in node.

import { colormapLut, type ColormapName, MISSING, type RGB } from './palettes';

export type ColourSpec =
  | { kind: 'constant'; n: number; rgb: RGB; opacity: number }
  | {
      kind: 'categorical';
      codes: Int8Array | Int16Array | Int32Array | Uint8Array;
      colors: RGB[];
      hidden: boolean[]; // per category; hidden points get alpha 0
      opacity: number;
    }
  | { kind: 'continuous'; values: Float32Array; colormap: ColormapName; range: [number, number] | null; opacity: number };

export interface ColourResult {
  rgba: Uint8Array;
  range: [number, number] | null; // the range used for continuous colours
}

/** 1st–99th percentile of the finite values (robust to outliers); sampled above 1 M values. */
export function robustRange(values: Float32Array): [number, number] {
  const step = Math.max(1, Math.floor(values.length / 1_000_000));
  const finite: number[] = [];
  for (let i = 0; i < values.length; i += step) if (Number.isFinite(values[i])) finite.push(values[i]);
  if (!finite.length) return [0, 1];
  const s = Float64Array.from(finite).sort();
  const lo = s[Math.floor(0.01 * (s.length - 1))];
  const hi = s[Math.ceil(0.99 * (s.length - 1))];
  return lo === hi ? [s[0], s[s.length - 1] === s[0] ? s[0] + 1 : s[s.length - 1]] : [lo, hi];
}

export function computeColours(spec: ColourSpec): ColourResult {
  const alpha = Math.round(Math.min(1, Math.max(0, spec.opacity)) * 255);
  if (spec.kind === 'constant') {
    const rgba = new Uint8Array(spec.n * 4);
    for (let i = 0; i < spec.n; i += 1) rgba.set([...spec.rgb, alpha], i * 4);
    return { rgba, range: null };
  }
  if (spec.kind === 'categorical') {
    const { codes, colors, hidden } = spec;
    const rgba = new Uint8Array(codes.length * 4);
    for (let i = 0; i < codes.length; i += 1) {
      const c = codes[i];
      const rgb = c >= 0 && c < colors.length ? colors[c] : MISSING;
      const o = i * 4;
      rgba[o] = rgb[0];
      rgba[o + 1] = rgb[1];
      rgba[o + 2] = rgb[2];
      rgba[o + 3] = c >= 0 && hidden[c] ? 0 : alpha;
    }
    return { rgba, range: null };
  }
  const { values } = spec;
  const range = spec.range ?? robustRange(values);
  const lut = colormapLut(spec.colormap);
  const scale = 255 / (range[1] - range[0] || 1);
  const rgba = new Uint8Array(values.length * 4);
  for (let i = 0; i < values.length; i += 1) {
    const v = values[i];
    const o = i * 4;
    if (!Number.isFinite(v)) {
      rgba.set([...MISSING, alpha], o);
      continue;
    }
    const k = Math.min(255, Math.max(0, Math.round((v - range[0]) * scale))) * 3;
    rgba[o] = lut[k];
    rgba[o + 1] = lut[k + 1];
    rgba[o + 2] = lut[k + 2];
    rgba[o + 3] = alpha;
  }
  return { rgba, range };
}
