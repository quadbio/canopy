// Panels inside a view's cell: their rectangles, and linked pan and zoom (views/registry.ts).

import { type Cell, fitBounds, gridCells, type Insets, type OrthoState } from './layout';
import type { Bounds, Link } from './registry';

/** Rectangles (canvas pixels) of `n` panels in `cell`, below the title row and left of an open
 * legend. One panel fills the whole cell (the title and legend float over it). */
export function panelRects(n: number, cell: Cell, insets: { top: number; right: number }, gap = 4): Cell[] {
  if (n <= 1) return n === 1 ? [cell] : [];
  const area = {
    width: Math.max(cell.width - insets.right, 20),
    height: Math.max(cell.height - insets.top, 20),
  };
  return gridCells(n, area.width, area.height, gap).map((r) => ({
    ...r,
    x: cell.x + r.x,
    y: cell.y + insets.top + r.y,
  }));
}

/** View states of linked panels after `changed` moved to `next`. The others follow relative to
 * their own fitted state: zoomed by the same steps and, with 'pan+zoom', moved by the same
 * distance on screen. (With one shared scale, they simply take the same zoom.) */
export function linkedStates(
  link: Link,
  changed: string,
  next: OrthoState,
  current: Record<string, OrthoState>,
  fitted: Record<string, OrthoState>,
): Record<string, OrthoState> {
  const out: Record<string, OrthoState> = { [changed]: next };
  const f0 = fitted[changed];
  if (link === 'none' || !f0) return out;
  const steps = next.zoom - f0.zoom;
  const scale = 2 ** next.zoom;
  const dx = (next.target[0] - f0.target[0]) * scale; // screen pixels
  const dy = (next.target[1] - f0.target[1]) * scale;
  for (const [key, st] of Object.entries(current)) {
    const f = fitted[key];
    if (key === changed || !f) continue;
    const zoom = f.zoom + steps;
    if (link === 'pan+zoom') {
      const s = 2 ** zoom;
      out[key] = { target: [f.target[0] + dx / s, f.target[1] + dy / s, 0], zoom };
    } else {
      out[key] = { ...st, zoom };
    }
  }
  return out;
}

/** What fitting the panels of a view needs. */
export interface FitLayout {
  keys: string[];
  rects: Cell[];
  fitInsets: Insets;
  sameScale: boolean;
  sameCoordinates: boolean;
  flipY: boolean;
  bounds: (Bounds | null)[];
}

/** The smallest box holding all boxes. */
function unionOf(boxes: (Bounds | null)[]): Bounds | null {
  const b = boxes.filter((x): x is Bounds => !!x);
  if (!b.length) return null;
  return [Math.min(...b.map((x) => x[0])), Math.min(...b.map((x) => x[1])), Math.max(...b.map((x) => x[2])), Math.max(...b.map((x) => x[3]))];
}

/** A layout's panels centred on their data at a given zoom (to join others at their scale): the
 * inset shift depends on the zoom, so the target is worked out at that zoom. */
export function atZoom(l: FitLayout, zoom: number): Record<string, OrthoState> {
  const out: Record<string, OrthoState> = {};
  const up = (l.fitInsets.top ?? 0) / 2 / 2 ** zoom;
  l.keys.forEach((key, j) => {
    const b = l.bounds[j];
    if (b) out[key] = { target: [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2 + (l.flipY ? -up : up), 0], zoom };
  });
  return out;
}

/** Fitted view states of all panels of a view; with `sameScale` they share the smallest zoom;
 * with `sameCoordinates` every panel shows the union of the panels' data. */
export function fitLayout(l: FitLayout): Record<string, OrthoState> {
  if (l.sameCoordinates) {
    const all = unionOf(l.bounds);
    if (!all) return {};
    return Object.fromEntries(l.keys.map((key, j) => [key, fitBounds(all, l.rects[j], 0.06, l.fitInsets, l.flipY)]));
  }
  const fits = l.keys.map((_, j) => (l.bounds[j] ? fitBounds(l.bounds[j]!, l.rects[j], 0.06, l.fitInsets, l.flipY) : null));
  const out: Record<string, OrthoState> = {};
  const zooms = fits.filter((f): f is OrthoState => !!f).map((f) => f.zoom);
  const common = l.sameScale && zooms.length > 1 ? Math.min(...zooms) : null;
  l.keys.forEach((key, j) => {
    const f = fits[j];
    if (!f) return;
    if (common === null) {
      out[key] = f;
      return;
    }
    // the same centre, at the shared zoom (the inset shift depends on the zoom)
    const b = l.bounds[j]!;
    const scale = 2 ** common;
    const up = (l.fitInsets.top ?? 0) / 2 / scale;
    out[key] = { target: [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2 + (l.flipY ? -up : up), 0], zoom: common };
  });
  return out;
}

