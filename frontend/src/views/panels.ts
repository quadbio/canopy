// Panels inside a view's cell: their rectangles, and linked pan and zoom (views/registry.ts).

import { type Cell, gridCells, type Insets, type OrthoState } from './layout';
import type { Link } from './registry';

/** Rectangles (canvas pixels) of `n` panels in `cell`, below the title row and left of an open
 * legend. One panel fills the whole cell (the title and legend float over it). */
export function panelRects(n: number, cell: Cell, insets: Required<Insets>, gap = 4): Cell[] {
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
