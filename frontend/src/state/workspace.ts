// The workspace: open views, their parameters and which one is selected. Plain JSON, so it
// can later be saved on the server or shared as a link; for now it is kept per browser.
//
// Saved state is never trusted: on restore, views of unknown types are dropped and each view's
// parameters go through its type's `normalise` (see views/registry.ts). Restoring is triggered
// by main.tsx after all view types are registered (`restoreWorkspace`).

import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

import { viewType } from '../views/registry';

export interface ViewSpec<P = Record<string, unknown>> {
  id: string;
  type: string;
  datasetId: string;
  table: string | null;
  title: string;
  params: P;
}

export interface Workspace {
  views: ViewSpec[];
  selected: string | null;
  browserSelection: string | null; // dataset highlighted in the file browser
  /** Adds a view; returns its id, or null when the canvas is full (MAX_VIEWS). */
  addView: (v: Omit<ViewSpec, 'id'>) => string | null;
  removeView: (id: string) => void;
  updateParams: (id: string, patch: Record<string, unknown>) => void;
  select: (id: string | null) => void;
  selectDataset: (id: string | null) => void;
  reset: () => void;
}

export const MAX_VIEWS = 9;
export const STORAGE_KEY = 'dbx-workspace';
const VERSION = 2;

const newId = () => `v${Math.random().toString(36).slice(2, 8)}`;

/** Saved views that can still be shown, with valid parameters. */
export function restoreViews(raw: unknown): ViewSpec[] {
  if (!Array.isArray(raw)) return [];
  const out: ViewSpec[] = [];
  for (const v of raw.slice(0, MAX_VIEWS)) {
    if (!v || typeof v !== 'object') continue;
    const r = v as Partial<ViewSpec>;
    const t = r.type ? viewType(r.type) : undefined;
    if (!t || typeof r.id !== 'string' || typeof r.datasetId !== 'string') continue;
    const params = t.normalise(r.params);
    if (!params) continue;
    out.push({
      id: r.id,
      type: r.type!,
      datasetId: r.datasetId,
      table: typeof r.table === 'string' ? r.table : null,
      title: typeof r.title === 'string' ? r.title : r.datasetId,
      params: params as Record<string, unknown>,
    });
  }
  return out;
}

const storage = createJSONStorage(() => {
  try {
    return localStorage;
  } catch {
    return sessionStorage; // e.g. storage blocked: the workspace is then not kept
  }
});

export const useWorkspace = create<Workspace>()(
  persist(
    (set, get) => ({
      views: [],
      selected: null,
      browserSelection: null,
      addView: (v) => {
        if (get().views.length >= MAX_VIEWS) return null;
        const id = newId();
        set((s) => ({ views: [...s.views, { ...v, id }], selected: id }));
        return id;
      },
      removeView: (id) =>
        set((s) => {
          const views = s.views.filter((v) => v.id !== id);
          return { views, selected: s.selected === id ? (views.at(-1)?.id ?? null) : s.selected };
        }),
      updateParams: (id, patch) =>
        set((s) => ({ views: s.views.map((v) => (v.id === id ? { ...v, params: { ...v.params, ...patch } } : v)) })),
      select: (id) => set({ selected: id }),
      selectDataset: (id) => set({ browserSelection: id }),
      reset: () => set({ views: [], selected: null, browserSelection: null }),
    }),
    {
      name: STORAGE_KEY,
      version: VERSION,
      storage,
      skipHydration: true,
      partialize: (s) => ({ views: s.views, selected: s.selected }),
      // Older saved versions: keep what can be kept; `merge` validates every view anyway.
      migrate: (persisted) => persisted as Partial<Workspace>,
      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<Workspace>;
        const views = restoreViews(p.views);
        const selected = views.some((v) => v.id === p.selected) ? (p.selected ?? null) : (views.at(-1)?.id ?? null);
        return { ...current, views, selected };
      },
    },
  ),
);

/** Restore the saved workspace; call once, after all view types are registered. */
export function restoreWorkspace(): void {
  try {
    void useWorkspace.persist.rehydrate();
  } catch {
    useWorkspace.getState().reset();
  }
}
