// The workspace: open views, their parameters and which one is selected. Plain JSON, so it
// can later be saved on the server or shared as a link; for now it is kept per browser.
//
// Saved state is never trusted: on restore, views of unknown types are dropped and each view's
// parameters go through its type's `normalise` (see views/registry.ts). Restoring is triggered
// by main.tsx after all view types are registered (`restoreWorkspace`).
//
// The last settings of each dataset (per table and view type) are remembered when they change,
// so opening a dataset again continues where it was left (`recall`); "Reset to defaults" in the
// settings panel goes back to the view type's defaults.

import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

import { mergeSlots } from '../colour/slots';
import { viewType } from '../views/registry';

export interface ViewSpec<P = Record<string, unknown>> {
  id: string;
  type: string;
  datasetId: string;
  table: string | null;
  title: string;
  params: P;
}

export interface Remembered {
  params: Record<string, unknown>;
  at: number; // ms since epoch, for dropping the oldest
}

export interface Workspace {
  views: ViewSpec[];
  /** Last settings by `rememberKey`; at most MAX_REMEMBERED, oldest dropped. */
  remembered: Record<string, Remembered>;
  /** Colours by name: category label -> palette slot, oldest first (colour/slots.ts). */
  labelSlots: [string, number][];
  addLabelSlots: (added: [string, number][]) => void;
  selected: string | null;
  browserSelection: string | null; // dataset highlighted in the file browser
  /** Adds a view; returns its id, or null when the canvas is full (MAX_VIEWS). */
  addView: (v: Omit<ViewSpec, 'id'>) => string | null;
  removeView: (id: string) => void;
  updateParams: (id: string, patch: Record<string, unknown>) => void;
  /** Replaces all parameters (e.g. "Reset to defaults"). */
  setParams: (id: string, params: Record<string, unknown>) => void;
  /** The remembered settings for a dataset/table/view type, validated; null if none. */
  recall: (key: string) => Record<string, unknown> | null;
  select: (id: string | null) => void;
  selectDataset: (id: string | null) => void;
  reset: () => void;
}

export const MAX_VIEWS = 9;
export const MAX_REMEMBERED = 200;
export const STORAGE_KEY = 'dbx-workspace';
const VERSION = 2;

const newId = () => `v${Math.random().toString(36).slice(2, 8)}`;

export const rememberKey = (v: Pick<ViewSpec, 'type' | 'datasetId' | 'table'>) => `${v.type}|${v.datasetId}|${v.table ?? ''}`;

/** `remembered` with this view's current settings, the oldest entries dropped beyond the limit. */
function remember(all: Record<string, Remembered>, v: ViewSpec): Record<string, Remembered> {
  const next = { ...all, [rememberKey(v)]: { params: v.params, at: Date.now() } };
  const keys = Object.keys(next);
  if (keys.length <= MAX_REMEMBERED) return next;
  keys.sort((a, b) => next[a].at - next[b].at);
  for (const k of keys.slice(0, keys.length - MAX_REMEMBERED)) delete next[k];
  return next;
}

/** Saved label slots, keeping only [string, non-negative integer] pairs. */
export function restoreSlots(raw: unknown): [string, number][] {
  if (!Array.isArray(raw)) return [];
  return mergeSlots(
    [],
    raw.filter(
      (e): e is [string, number] =>
        Array.isArray(e) && typeof e[0] === 'string' && Number.isInteger(e[1]) && (e[1] as number) >= 0,
    ),
  );
}

/** Saved remembered settings, keeping only well-formed entries (params are validated on use). */
export function restoreRemembered(raw: unknown): Record<string, Remembered> {
  if (!raw || typeof raw !== 'object') return {};
  const out: Record<string, Remembered> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const r = v as Partial<Remembered> | null;
    if (r && typeof r === 'object' && r.params && typeof r.params === 'object' && typeof r.at === 'number') {
      out[k] = { params: r.params as Record<string, unknown>, at: r.at };
    }
  }
  return out;
}

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

/** Used when browser storage is blocked or missing: the workspace then lasts for this page only. */
const memoryStorage = (() => {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  };
})();

const storage = createJSONStorage(() => {
  try {
    if (typeof localStorage !== 'undefined' && localStorage) return localStorage;
  } catch {
    // access throws when storage is blocked
  }
  return memoryStorage;
});

export const useWorkspace = create<Workspace>()(
  persist(
    (set, get) => ({
      views: [],
      remembered: {},
      labelSlots: [],
      addLabelSlots: (added) => {
        if (added.length) set((s) => ({ labelSlots: mergeSlots(s.labelSlots, added) }));
      },
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
      updateParams: (id, patch) => {
        const v = get().views.find((x) => x.id === id);
        if (v) get().setParams(id, { ...v.params, ...patch });
      },
      setParams: (id, params) =>
        set((s) => {
          const v = s.views.find((x) => x.id === id);
          if (!v) return {};
          const nv = { ...v, params };
          return { views: s.views.map((x) => (x.id === id ? nv : x)), remembered: remember(s.remembered, nv) };
        }),
      recall: (key) => {
        const r = get().remembered[key];
        const t = r ? viewType(key.split('|')[0]) : undefined;
        return r && t ? (t.normalise(r.params) as Record<string, unknown> | null) : null;
      },
      select: (id) => set({ selected: id }),
      selectDataset: (id) => set({ browserSelection: id }),
      reset: () => set({ views: [], selected: null, browserSelection: null }),
    }),
    {
      name: STORAGE_KEY,
      version: VERSION,
      storage,
      skipHydration: true,
      partialize: (s) => ({ views: s.views, selected: s.selected, remembered: s.remembered, labelSlots: s.labelSlots }),
      // Older saved versions: keep what can be kept; `merge` validates every view anyway.
      migrate: (persisted) => persisted as Partial<Workspace>,
      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<Workspace>;
        const views = restoreViews(p.views);
        const selected = views.some((v) => v.id === p.selected) ? (p.selected ?? null) : (views.at(-1)?.id ?? null);
        return {
          ...current,
          views,
          selected,
          remembered: restoreRemembered(p.remembered),
          labelSlots: restoreSlots(p.labelSlots),
        };
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
