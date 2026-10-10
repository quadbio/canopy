// Grid geometry for 1–9 views in one canvas (row-major, as square as possible), and view fitting.

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

export interface OrthoState {
  target: [number, number, number];
  zoom: number;
}

/** Orthographic view state (y up) that fits `bounds` into a cell with a margin, leaving
 * `insetTop` pixels free at the top (the view's title row). */
export function fitBounds(
  bounds: [number, number, number, number],
  cell: Pick<Cell, 'width' | 'height'>,
  margin = 0.06,
  insetTop = 0,
): OrthoState {
  const [x0, y0, x1, y1] = bounds;
  const dx = Math.max(x1 - x0, 1e-9);
  const dy = Math.max(y1 - y0, 1e-9);
  const h = Math.max(cell.height - insetTop, 1);
  const scale = Math.min(cell.width / dx, h / dy) * (1 - 2 * margin);
  // Centre the data in the area below the inset: move the view centre up by half the inset.
  const shift = insetTop / 2 / scale;
  return { target: [(x0 + x1) / 2, (y0 + y1) / 2 + shift, 0], zoom: Math.log2(scale) };
}

/** The same place, after the cell changed size: keep the centre, scale the zoom with the cell. */
export function resizeView(state: OrthoState, from: Pick<Cell, 'width' | 'height'>, to: Pick<Cell, 'width' | 'height'>): OrthoState {
  const ratio = Math.min(to.width / Math.max(from.width, 1), to.height / Math.max(from.height, 1));
  return { ...state, zoom: state.zoom + Math.log2(ratio) };
}
