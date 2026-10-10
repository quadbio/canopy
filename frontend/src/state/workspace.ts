// The workspace: open views, their parameters and which one is selected. Plain JSON, so it
// can later be saved on the server or shared as a link; for now it is kept per browser.

import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

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
  addView: (v: Omit<ViewSpec, 'id'>) => string;
  removeView: (id: string) => void;
  updateParams: (id: string, patch: Record<string, unknown>) => void;
  select: (id: string | null) => void;
  selectDataset: (id: string | null) => void;
}

export const MAX_VIEWS = 9;

const newId = () => `v${Math.random().toString(36).slice(2, 8)}`;

export const useWorkspace = create<Workspace>()(
  persist(
    (set) => ({
      views: [],
      selected: null,
      browserSelection: null,
      addView: (v) => {
        const id = newId();
        set((s) => ({ views: [...s.views, { ...v, id }].slice(-MAX_VIEWS), selected: id }));
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
    }),
    {
      name: 'dbx-workspace',
      version: 1,
      storage: createJSONStorage(() => {
        try {
          return localStorage;
        } catch {
          return sessionStorage; // e.g. storage blocked: the workspace is then not kept
        }
      }),
      partialize: (s) => ({ views: s.views, selected: s.selected }),
    },
  ),
);
