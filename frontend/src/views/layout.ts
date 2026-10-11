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

export interface OrthoState {
  target: [number, number, number];
  zoom: number;
}

export interface Insets {
  top?: number;
  right?: number;
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
  const { top = 0, right = 0 } = typeof insets === 'number' ? { top: insets } : insets;
  const [x0, y0, x1, y1] = bounds;
  const dx = Math.max(x1 - x0, 1e-9);
  const dy = Math.max(y1 - y0, 1e-9);
  const w = Math.max(cell.width - right, 1);
  const h = Math.max(cell.height - top, 1);
  const scale = Math.min(w / dx, h / dy) * (1 - 2 * margin);
  // Centre the data in the free area: move the view centre towards the insets by half of them.
  const up = top / 2 / scale;
  return {
    target: [(x0 + x1) / 2 + right / 2 / scale, (y0 + y1) / 2 + (flipY ? -up : up), 0],
    zoom: Math.log2(scale),
  };
}

/** The same place, after the cell changed size: keep the centre, scale the zoom with the cell. */
export function resizeView(state: OrthoState, from: Pick<Cell, 'width' | 'height'>, to: Pick<Cell, 'width' | 'height'>): OrthoState {
  const ratio = Math.min(to.width / Math.max(from.width, 1), to.height / Math.max(from.height, 1));
  return { ...state, zoom: state.zoom + Math.log2(ratio) };
}
