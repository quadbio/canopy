// Level of detail for large datasets (review S5b B-M1–B-M3, B-M7).
//
// - Large datasets are reordered once, "representative cells first": the server's stratified
//   subset (every category kept) in a shuffled order, then the other cells, also shuffled. Any
//   prefix of that order is a fair sample, so drawing fewer cells means drawing a shorter prefix
//   of the *same* GPU buffers: no upload when the number changes.
// - The canvas shares one budget of points on screen (CANVAS_BUDGET) among its views, in
//   proportion to their cell counts. A view draws as many cells as keep about its share on
//   screen: few when the whole dataset is in view, more when zoomed in, all when only a part is
//   visible, but never more than VERTEX_FACTOR × its share (every drawn point costs some work
//   even off screen).
// - Hidden categories count as not on screen, so isolating one population draws all of it.
// - The budget adapts to the machine: when moving the canvas runs below ~40 frames per second
//   while it draws its full budget, the budget is lowered (BudgetTuner), and the browser keeps
//   the lower one (first host test: ~110 ms frames at 1.7 M cells on an M2 at 2560 × 1440).
// The canvas publishes each view's visible share in powers of two, so zooming re-renders views
// rarely.

import { create } from 'zustand';

function override(): number | null {
  try {
    const v = new URLSearchParams(window.location.search).get('lod');
    return v && /^\d+$/.test(v) ? Number(v) : null;
  } catch {
    return null;
  }
}

// Points on screen for the whole canvas at once, at most (an M2 draws 1 M points in ~20 ms at a
// 2× pixel density). `?lod=<n>` in the page address fixes it (tests, slow machines).
export const CANVAS_BUDGET = override() ?? 1_000_000;
export const MIN_BUDGET = 200_000;
const BUDGET_KEY = 'canopy.lod.budget';

function rememberedBudget(): number {
  if (override() !== null) return CANVAS_BUDGET;
  try {
    const v = Number(localStorage.getItem(BUDGET_KEY));
    return Number.isFinite(v) && v >= MIN_BUDGET && v < CANVAS_BUDGET ? v : CANVAS_BUDGET;
  } catch {
    return CANVAS_BUDGET;
  }
}
export const VERTEX_FACTOR = 1.5;
// Datasets above this get the representative order (it costs one subset request and a copy).
export const REORDER_ABOVE = Math.min(200_000, Math.max(1_000, Math.floor(CANVAS_BUDGET / 4)));
// Size of the server's stratified subset at the front of the order.
export const SUBSET_SIZE = 1_000_000;

interface Lod {
  /** Points on screen for the whole canvas (CANVAS_BUDGET, lowered on a slow machine). */
  budget: number;
  setBudget: (n: number) => void;
  /** Share of the data's extent visible in each view (0..1], in steps of powers of two. */
  visible: Record<string, number>;
  /** The area each view shows (view coordinates), snapped outwards to a coarse grid so it
   * changes only every few hundred pixels of panning (views read data in view with it). */
  extent: Record<string, [number, number, number, number]>;
  setExtent: (id: string, e: [number, number, number, number]) => void;
  /** Each view's free area in its first panel, in pixels: the panel less the title row, an open
   * legend and the view's margins (what its bounds are fitted into; e.g. a scatter's shape). */
  size: Record<string, [number, number]>;
  setSize: (id: string, w: number, h: number) => void;
  /** Points on screen each view may draw (its share of CANVAS_BUDGET). */
  allowance: Record<string, number>;
  setVisible: (id: string, share: number) => void;
  setAllowance: (a: Record<string, number>) => void;
  forget: (ids: string[]) => void;
}

export const useLod = create<Lod>()((set) => ({
  budget: rememberedBudget(),
  setBudget: (n) => {
    try {
      if (override() === null) localStorage.setItem(BUDGET_KEY, String(n));
    } catch {
      /* private window: this session only */
    }
    set((s) => (s.budget === n ? s : { budget: n }));
  },
  visible: {},
  extent: {},
  size: {},
  setSize: (id, w, h) =>
    set((s) => {
      const old = s.size[id];
      return old && old[0] === w && old[1] === h ? s : { size: { ...s.size, [id]: [w, h] } };
    }),
  allowance: {},
  setExtent: (id, e) =>
    set((s) => {
      const old = s.extent[id];
      return old && old.every((v, i) => v === e[i]) ? s : { extent: { ...s.extent, [id]: e } };
    }),
  setVisible: (id, share) => {
    const step = Math.min(1, 2 ** Math.ceil(Math.log2(Math.max(share, 1e-9))));
    set((s) => (s.visible[id] === step ? s : { visible: { ...s.visible, [id]: step } }));
  },
  setAllowance: (a) =>
    set((s) => {
      const same =
        Object.keys(a).length === Object.keys(s.allowance).length &&
        Object.entries(a).every(([k, v]) => s.allowance[k] === v);
      return same ? s : { allowance: a };
    }),
  forget: (ids) =>
    set((s) => {
      if (!ids.some((id) => id in s.visible || id in s.allowance || id in s.extent)) return s;
      const visible = { ...s.visible };
      const allowance = { ...s.allowance };
      const extent = { ...s.extent };
      const size = { ...s.size };
      for (const id of ids) {
        delete visible[id];
        delete allowance[id];
        delete extent[id];
        delete size[id];
      }
      return { visible, allowance, extent, size };
    }),
}));

/** Share of `bounds` (data coordinates) inside a `width` × `height` px view at `zoom`. */
export function visibleShare(
  bounds: [number, number, number, number],
  width: number,
  height: number,
  zoom: number,
): number {
  const scale = 2 ** zoom;
  const dw = Math.max(bounds[2] - bounds[0], 1e-12);
  const dh = Math.max(bounds[3] - bounds[1], 1e-12);
  return Math.min(1, width / scale / dw) * Math.min(1, height / scale / dh);
}

/** The area a `width` × `height` px view centred on `target` at `zoom` shows, grown to whole
 * cells of a grid of 256 px at the nearest half zoom step (so small pans keep the same area). */
export function snappedExtent(
  target: [number, number, number],
  zoom: number,
  width: number,
  height: number,
): [number, number, number, number] {
  const scale = 2 ** zoom;
  const hw = width / 2 / scale;
  const hh = height / 2 / scale;
  const cell = 256 / 2 ** (Math.floor(zoom * 2) / 2);
  const lo = (v: number) => Math.floor(v / cell) * cell;
  const hi = (v: number) => Math.ceil(v / cell) * cell;
  return [lo(target[0] - hw), lo(target[1] - hh), hi(target[0] + hw), hi(target[1] + hh)];
}

/** Each view's share of the canvas budget, by its cell count; Infinity when all fit. */
export function allowances(totals: Record<string, number>, budget = CANVAS_BUDGET): Record<string, number> {
  const sum = Object.values(totals).reduce((a, b) => a + b, 0);
  const out: Record<string, number> = {};
  for (const [id, n] of Object.entries(totals)) {
    out[id] = sum <= budget ? Infinity : Math.max(1_000, Math.round((budget * n) / sum / 1_000) * 1_000);
  }
  return out;
}

/** How many cells (a prefix of the representative order) a view draws. `visible`: share of the
 * data's extent on screen; `notHidden`: share of cells not in hidden categories. Hidden cells
 * are dropped in the vertex shader and cost little, so the cap grows as more are hidden. */
export function drawCount(total: number, visible: number, allowance: number, notHidden = 1): number {
  if (!Number.isFinite(allowance)) return total;
  const shown = Math.max(visible * notHidden, 1e-9);
  const forScreen = allowance / shown; // keeps ~allowance points on screen
  const cap = (VERTEX_FACTOR * allowance) / Math.max(notHidden, 1e-9);
  return Math.round(Math.min(total, Math.max(allowance, forScreen), cap));
}

/** Seeded shuffle (mulberry32), so the order is the same on every reload. */
function shuffle(a: Int32Array, seed: number): Int32Array {
  let t = seed >>> 0;
  const rand = () => {
    t = (t + 0x6d2b79f5) >>> 0;
    let r = Math.imul(t ^ (t >>> 15), 1 | t);
    r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
  for (let i = a.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rand() * (i + 1));
    const x = a[i];
    a[i] = a[j];
    a[j] = x;
  }
  return a;
}

/** The representative order of `n` cells: the subset (sorted indices) shuffled, then the rest
 * shuffled. */
export function representativeOrder(subset: Int32Array, n: number): Int32Array {
  const inSubset = new Uint8Array(n);
  for (const i of subset) inSubset[i] = 1;
  const rest = new Int32Array(n - subset.length);
  let k = 0;
  for (let i = 0; i < n; i += 1) if (!inSubset[i]) rest[k++] = i;
  const order = new Int32Array(n);
  order.set(shuffle(Int32Array.from(subset), 1));
  order.set(shuffle(rest, 2), subset.length);
  return order;
}

/** Lowers (or raises again) the canvas budget from frame intervals measured while the canvas
 * moves at its full budget. Every SAMPLE frames: below ~40 fps (median over SLOW_MS) the budget
 * shrinks in proportion, down to MIN_BUDGET; at a steady 60 fps (median and 95th percentile
 * within FAST_MS) it grows by a quarter, up to CANVAS_BUDGET. Returns the new budget, or null. */
export class BudgetTuner {
  static SAMPLE = 30;
  static SLOW_MS = 25;
  static FAST_MS = 18;
  private gaps: number[] = [];

  frame(gap: number, budget: number): number | null {
    if (!(gap > 0 && gap <= 250) || override() !== null) return null;
    this.gaps.push(gap);
    if (this.gaps.length < BudgetTuner.SAMPLE) return null;
    const g = this.gaps.sort((a, b) => a - b);
    this.gaps = [];
    const median = g[Math.floor(g.length / 2)];
    const p95 = g[Math.floor(g.length * 0.95)];
    let next = budget;
    if (median > BudgetTuner.SLOW_MS) next = Math.max(MIN_BUDGET, budget * Math.max(0.5, Math.min(0.8, 16.7 / median)));
    else if (median <= BudgetTuner.FAST_MS && p95 <= BudgetTuner.FAST_MS * 1.5) next = Math.min(CANVAS_BUDGET, budget * 1.25);
    next = Math.round(next / 50_000) * 50_000;
    return next !== budget ? next : null;
  }

  reset() {
    this.gaps = [];
  }
}
