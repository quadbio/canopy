// Level of detail: how much of each view's data is on screen, shared by the canvas (which knows
// the zoom) and the views (which decide what to draw). Updated only when the share changes by a
// factor of two, so zooming does not re-render the views on every frame.

import { create } from 'zustand';

interface Lod {
  /** Share of the data's extent visible in each view (0..1], in steps of powers of two. */
  visible: Record<string, number>;
  setVisible: (id: string, share: number) => void;
  forget: (id: string) => void;
}

export const useLod = create<Lod>()((set) => ({
  visible: {},
  setVisible: (id, share) => {
    const step = Math.min(1, 2 ** Math.ceil(Math.log2(Math.max(share, 1e-9))));
    set((s) => (s.visible[id] === step ? s : { visible: { ...s.visible, [id]: step } }));
  },
  forget: (id) =>
    set((s) => {
      if (!(id in s.visible)) return s;
      const visible = { ...s.visible };
      delete visible[id];
      return { visible };
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

// Datasets above FULL_LIMIT cells draw SUBSET_SIZE representative cells while more than
// FULL_LIMIT would be on screen (measured on an M2: 1 M points at 60 fps, 3 M at ~15 fps).
export const FULL_LIMIT = 1_500_000;
export const SUBSET_SIZE = 1_000_000;

/** Whether to draw the subset: many cells, and many of them on screen. */
export function useSubsetNow(id: string, total: number, allCells: boolean): boolean {
  const share = useLod((s) => s.visible[id] ?? 1);
  return !allCells && total > FULL_LIMIT && total * share > FULL_LIMIT;
}
