// Grid geometry for 1–9 views in one canvas (row-major, as square as possible).

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

/** Orthographic view state that fits `bounds` into a cell with a margin. */
export function fitBounds(bounds: [number, number, number, number], cell: Cell, margin = 0.06) {
  const [x0, y0, x1, y1] = bounds;
  const dx = Math.max(x1 - x0, 1e-9);
  const dy = Math.max(y1 - y0, 1e-9);
  const scale = Math.min(cell.width / dx, cell.height / dy) * (1 - 2 * margin);
  return { target: [(x0 + x1) / 2, (y0 + y1) / 2, 0] as [number, number, number], zoom: Math.log2(scale) };
}
