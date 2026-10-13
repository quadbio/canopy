// Grid geometry for views in one canvas and for the panels inside a view (row-major, as square
// as possible), and view fitting.

export interface Cell {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function gridCells(n: number, width: number, height: number, gap = 1): Cell[] {
  if (n <= 0) return [];
  const cols = Math.ceil(Math.sqrt(n));
  const rows = Math.ceil(n / cols);
  const w = (width - gap * (cols - 1)) / cols;
  const h = (height - gap * (rows - 1)) / rows;
  return Array.from({ length: n }, (_, i) => ({
    x: Math.round((i % cols) * (w + gap)),
    y: Math.round(Math.floor(i / cols) * (h + gap)),
    width: Math.round(w),
    height: Math.round(h),
  }));
}

// The share of its view's tile a companion plot takes, at the bottom (owner, S7a demo).
export const COMPANION_SHARE = 1 / 3;

/** Tiles for views: the grid holds one tile per view, except that a view following another
 * (a plot belonging to an embedding: `follows[i]` is that view's id) takes the bottom third of
 * the followed view's tile instead of a tile of its own. In the order of `ids`. */
export function viewCells(ids: string[], follows: (string | null)[], width: number, height: number, gap = 1): Cell[] {
  const attached = ids.map((_, i) => {
    const f = follows[i];
    return f !== null && f !== ids[i] && ids.includes(f) && follows[ids.indexOf(f)] === null ? f : null;
  });
  const own = ids.filter((_, i) => attached[i] === null);
  const tiles = gridCells(own.length, width, height, gap);
  const tileOf = new Map(own.map((id, k) => [id, tiles[k]]));
  const out: Cell[] = [];
  ids.forEach((id, i) => {
    const host = attached[i];
    const tile = tileOf.get(host ?? id)!;
    const companion = host === null && attached.indexOf(id) >= 0;
    if (!tile) return;
    if (host === null && !companion) {
      out[i] = tile;
      return;
    }
    const lower = Math.round(tile.height * COMPANION_SHARE);
    const upper = tile.height - lower - gap;
    out[i] =
      host === null
        ? { ...tile, height: upper }
        : { x: tile.x, y: tile.y + upper + gap, width: tile.width, height: lower };
  });
  return out;
}

export interface OrthoState {
  target: [number, number, number];
  zoom: number;
}

export interface Insets {
  top?: number;
  right?: number;
  bottom?: number;
  left?: number;
}

/** Orthographic view state that fits `bounds` into a cell with a margin, leaving `insets` pixels
 * free (the title row at the top, an open legend at the right). A number is the top inset.
 * `flipY`: y grows downwards on screen (image and spatial coordinates). */
export function fitBounds(
  bounds: [number, number, number, number],
  cell: Pick<Cell, 'width' | 'height'>,
  margin = 0.06,
  insets: Insets | number = 0,
  flipY = false,
): OrthoState {
  const { top = 0, right = 0, bottom = 0, left = 0 } = typeof insets === 'number' ? { top: insets } : insets;
  const [x0, y0, x1, y1] = bounds;
  const dx = Math.max(x1 - x0, 1e-9);
  const dy = Math.max(y1 - y0, 1e-9);
  const w = Math.max(cell.width - right - left, 1);
  const h = Math.max(cell.height - top - bottom, 1);
  const scale = Math.min(w / dx, h / dy) * (1 - 2 * margin);
  // Centre the data in the free area: move the view centre towards the insets by half of them.
  const up = (top - bottom) / 2 / scale;
  return {
    target: [(x0 + x1) / 2 + (right - left) / 2 / scale, (y0 + y1) / 2 + (flipY ? -up : up), 0],
    zoom: Math.log2(scale),
  };
}

/** The same place, after the cell changed size: keep the centre, scale the zoom with the cell. */
export function resizeView(state: OrthoState, from: Pick<Cell, 'width' | 'height'>, to: Pick<Cell, 'width' | 'height'>): OrthoState {
  const ratio = Math.min(to.width / Math.max(from.width, 1), to.height / Math.max(from.height, 1));
  return { ...state, zoom: state.zoom + Math.log2(ratio) };
}

/** Tiles with linked groups (S7b): a group takes one tile, its views a grid inside it, below
 * the group's title row (`top`) and left of its legend (`right[group]`, when open). Other views
 * as `viewCells`. Returns each view's cell (in the order of `views`) and each group's tile. */
export function tileCells(
  views: { id: string; follows: string | null; group?: string }[],
  width: number,
  height: number,
  gap: number,
  top: number,
  right: Record<string, number> = {},
): { cells: Cell[]; groups: Record<string, Cell> } {
  const firstOf = new Map<string, string>();
  for (const v of views) if (v.group && !firstOf.has(v.group)) firstOf.set(v.group, v.id);
  // one entry per tile: independent views (with their followers), and each group's first view
  const units = views.filter((v) => !v.group || firstOf.get(v.group) === v.id);
  const unitCells = viewCells(
    units.map((v) => v.id),
    units.map((v) => (v.group ? null : v.follows)),
    width,
    height,
    gap,
  );
  const cellOf = new Map(units.map((v, i) => [v.id, unitCells[i]]));
  const groups: Record<string, Cell> = {};
  const members = new Map<string, string[]>();
  for (const v of views) if (v.group) members.set(v.group, [...(members.get(v.group) ?? []), v.id]);
  const inner = new Map<string, Cell>();
  for (const [g, ids] of members) {
    const tile = cellOf.get(firstOf.get(g)!);
    if (!tile) continue;
    groups[g] = tile;
    const w = Math.max(tile.width - (right[g] ?? 0), 20);
    const h = Math.max(tile.height - top, 20);
    gridCells(ids.length, w, h, 4).forEach((r, k) =>
      inner.set(ids[k], { x: tile.x + r.x, y: tile.y + top + r.y, width: r.width, height: r.height }),
    );
  }
  return { cells: views.map((v) => (v.group ? inner.get(v.id)! : cellOf.get(v.id)!)), groups };
}
