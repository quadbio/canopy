// Lasso selections (S7c): cells drawn around in a view, kept per view (in this page only: a
// selection is a mask of up to millions of cells), and summaries of "selected or not" against
// another feature for the plot below the view, computed in the browser from values the view
// already holds (the same form as the server's summaries, data/summary.py).

import { create } from 'zustand';

import type { PlotSummary, ViolinGroup } from '../api/types';

export type Polygon = [number, number][];

export interface Selection {
  /** 1 for each selected cell (one entry per cell of the view's table). */
  mask: Uint8Array;
  count: number;
  /** The lassos drawn (view coordinates), per panel, for as long as `key` is the view's. */
  polygons: { panel: string; points: Polygon }[];
  key: string;
}

interface SelectionStore {
  selections: Record<string, Selection>;
  set: (view: string, s: Selection | null) => void;
}

export const useSelection = create<SelectionStore>()((set) => ({
  selections: {},
  set: (view, sel) =>
    set((s) => {
      const next = { ...s.selections };
      if (sel) next[view] = sel;
      else delete next[view];
      return { selections: next };
    }),
}));

// (in this order also in the charts' natural order)
export const SELECTED = 'in the selection';
export const NOT_SELECTED = 'not in the selection';

/** Whether (x, y) is inside the polygon (even-odd rule). */
export function inside(x: number, y: number, poly: Polygon): boolean {
  let hit = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

/** Cells (rows of `xy`, or of `cells` when given) inside the polygon, set to 1 in `mask`;
 * `skip(i)` leaves a cell out (e.g. a hidden category). Returns how many were added. */
export function lassoInto(
  mask: Uint8Array,
  xy: ArrayLike<number>,
  poly: Polygon,
  cells: ArrayLike<number> | null = null,
  skip: ((i: number) => boolean) | null = null,
  rowOf: ArrayLike<number> | null = null,
): number {
  if (poly.length < 3) return 0;
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const [x, y] of poly) {
    x0 = Math.min(x0, x);
    x1 = Math.max(x1, x);
    y0 = Math.min(y0, y);
    y1 = Math.max(y1, y);
  }
  let added = 0;
  const n = cells ? cells.length : xy.length / 2;
  for (let k = 0; k < n; k += 1) {
    const p = cells ? cells[k] : k;
    const x = xy[2 * p];
    const y = xy[2 * p + 1];
    if (!(x >= x0 && x <= x1 && y >= y0 && y <= y1)) continue;
    const i = rowOf ? rowOf[p] : p;
    if (i < 0 || i >= mask.length || mask[i] || (skip && skip(i)) || !inside(x, y, poly)) continue;
    mask[i] = 1;
    added += 1;
  }
  return added;
}

const ok = (v: number) => Number.isFinite(v) && v > -1e30; // MISSING_VALUE is -inf-like

function quantile(sorted: Float32Array, q: number): number {
  const at = (sorted.length - 1) * q;
  const lo = Math.floor(at);
  const hi = Math.min(lo + 1, sorted.length - 1);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (at - lo);
}

/** Violins of a number for the selected cells and the others (as `summary.violin`). */
export function selectionViolin(values: ArrayLike<number>, mask: Uint8Array, log1p: boolean, bins = 64): Extract<PlotSummary, { kind: 'violin' }> {
  const t = (v: number) => (log1p ? Math.log1p(Math.max(v, 0)) : v);
  let lo = Infinity;
  let hi = -Infinity;
  const sizes = [0, 0];
  let missing = 0;
  for (let i = 0; i < values.length; i += 1) {
    const v = values[i];
    if (!ok(v)) {
      missing += 1;
      continue;
    }
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
    sizes[mask[i] ? 0 : 1] += 1;
  }
  if (!Number.isFinite(lo)) [lo, hi] = [0, 1];
  if (log1p) [lo, hi] = [Math.log1p(Math.max(lo, 0)), Math.log1p(Math.max(hi, 0))];
  if (hi <= lo) hi = lo + 1;
  const vals = [new Float32Array(sizes[0]), new Float32Array(sizes[1])];
  const hist = [new Array<number>(bins).fill(0), new Array<number>(bins).fill(0)];
  const above = [0, 0];
  const zeros = [0, 0];
  let below = 0;
  const sums = [0, 0];
  const fill = [0, 0];
  for (let i = 0; i < values.length; i += 1) {
    const v = values[i];
    if (!ok(v)) continue;
    const g = mask[i] ? 0 : 1;
    const tv = t(v);
    vals[g][fill[g]++] = tv;
    hist[g][Math.min(bins - 1, Math.max(0, Math.floor(((tv - lo) / (hi - lo)) * bins)))] += 1;
    if (v > 0) above[g] += 1;
    else if (v === 0) zeros[g] += 1;
    else if (log1p) below += 1;
    sums[g] += tv;
  }
  const groups: ViolinGroup[] = [0, 1].map((g) => {
    if (!sizes[g]) return { n: 0, hist: hist[g] };
    const s = vals[g].sort();
    return {
      n: sizes[g],
      hist: hist[g],
      q1: quantile(s, 0.25),
      median: quantile(s, 0.5),
      q3: quantile(s, 0.75),
      mean: sums[g] / sizes[g],
      frac_above_zero: above[g] / sizes[g],
      n_zero: zeros[g],
    };
  });
  return {
    kind: 'violin',
    categories: [SELECTED, NOT_SELECTED],
    range: [lo, hi],
    bins,
    log1p,
    groups,
    n_missing_value: missing,
    n_missing_category: 0,
    n_below_zero_at_zero: below,
  };
}

/** Composition of the selected cells and of the others by a category column (as
 * `summary.bars`: rows selected / not selected, stacks the categories). */
export function selectionBars(codes: ArrayLike<number>, labels: string[], mask: Uint8Array): Extract<PlotSummary, { kind: 'bars' }> {
  const k = labels.length;
  const counts = [new Array<number>(k).fill(0), new Array<number>(k).fill(0)];
  let missing = 0;
  for (let i = 0; i < codes.length; i += 1) {
    const c = codes[i];
    if (!(c >= 0 && c < k)) {
      missing += 1;
      continue;
    }
    counts[mask[i] ? 0 : 1][c] += 1;
  }
  return { kind: 'bars', rows: [SELECTED, NOT_SELECTED], columns: labels, counts, n_missing: missing };
}
