// The workspace: open views, their parameters and which one is selected. Plain JSON, so it
// can later be saved on the server or shared as a link; for now it is kept per browser.
//
// Saved state is never trusted: on restore, views of unknown types are dropped and each view's
// parameters go through its type's `normalise` (see views/registry.ts). Restoring is triggered
// by main.tsx after all view types are registered (`restoreWorkspace`).
//
// Linked groups (S7b): views opened together from several datasets carry the same `group`.
// They share one tile, pan and zoom together and share their settings: a change made in one
// applies to all, except the view type's `ownKeys` (e.g. the embedding, whose names differ
// between datasets). A group and independent views never share the canvas (owner, S7b review):
// a group is opened on an empty canvas and fills it; while one is open, nothing else opens.
//
// The last settings of each dataset (per table and view type) are remembered when they change,
// so opening a dataset again continues where it was left (`recall`); "Reset to defaults" in the
// settings panel goes back to the view type's defaults.

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
  /** The linked group this view belongs to (absent: an independent view). */
  group?: string;
}

export interface Remembered {
  params: Record<string, unknown>;
  at: number; // ms since epoch, for dropping the oldest
}

export interface Workspace {
  views: ViewSpec[];
  /** Last settings by `rememberKey`; at most MAX_REMEMBERED, oldest dropped. */
  remembered: Record<string, Remembered>;
  selected: string | null;
  browserSelection: string | null; // dataset highlighted in the file browser
  /** Adds a view (after view `after`, else at the end) and selects it; returns its id, or null
   * when the canvas is full (MAX_VIEWS). */
  addView: (v: Omit<ViewSpec, 'id'>, after?: string) => string | null;
  /** Adds views as one linked group (at most MAX_GROUP) and selects the first; returns the
   * group's id, or null when the canvas is full. */
  addGroup: (vs: Omit<ViewSpec, 'id' | 'group'>[]) => string | null;
  /** Closes a view; a group left with one view becomes an independent view. */
  removeView: (id: string) => void;
  removeGroup: (group: string) => void;
  /** Changes some parameters; in a group, all its views take the change, except the view
   * type's own keys, which change in this view only. */
  updateParams: (id: string, patch: Record<string, unknown>) => void;
  /** Replaces all parameters (e.g. "Reset to defaults"); in a group as `updateParams`. */
  setParams: (id: string, params: Record<string, unknown>) => void;
  /** Datasets ticked in the file browser, to open as a linked group. */
  ticked: string[];
  tick: (ids: string[], on: boolean) => void;
  /** The remembered settings for a dataset/table/view type, validated; null if none. */
  recall: (key: string) => Record<string, unknown> | null;
  select: (id: string | null) => void;
  selectDataset: (id: string | null) => void;
  reset: () => void;
}

export const MAX_VIEWS = 9; // tiles: an independent view (or a plot) or a linked group
export const MAX_GROUP = 24; // views in a linked group
const MAX_SPECS = MAX_VIEWS * MAX_GROUP; // restored at most
export const MAX_REMEMBERED = 200;
export const STORAGE_KEY = 'canopy-workspace';
const VERSION = 2;

const newId = () => `v${Math.random().toString(36).slice(2, 8)}`;
const newGroupId = () => `g${Math.random().toString(36).slice(2, 8)}`;

/** Tiles the views take: one per independent view, one per group; a plot below its view takes
 * none (it shares the view's tile). */
export function tilesTaken(views: Pick<ViewSpec, 'id' | 'group' | 'params'>[]): number {
  const groups = new Set<string>();
  const ids = new Set(views.map((v) => v.id));
  let n = 0;
  for (const v of views) {
    const follows = v.params.follows;
    if (typeof follows === 'string' && ids.has(follows)) continue;
    if (!v.group) n += 1;
    else if (!groups.has(v.group)) {
      groups.add(v.group);
      n += 1;
    }
  }
  return n;
}

/** The views of `v`'s group (just `v` when it has none). */
export function membersOf<T extends Pick<ViewSpec, 'id' | 'group'>>(views: T[], v: T): T[] {
  return v.group ? views.filter((x) => x.group === v.group) : [v];
}

/** Parameters after a change made in view `target` of a group, for member `member`: the shared
 * keys of `next`, its own keys only where it is the target. */
export function groupParams(
  member: Record<string, unknown>,
  next: Record<string, unknown>,
  own: readonly string[],
  isTarget: boolean,
): Record<string, unknown> {
  if (isTarget) return next;
  const out: Record<string, unknown> = { ...next };
  for (const k of own) {
    if (k in member) out[k] = member[k];
    else delete out[k];
  }
  return out;
}

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
  for (const v of raw.slice(0, MAX_SPECS)) {
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
      ...(typeof r.group === 'string' && r.group ? { group: r.group } : {}),
    });
  }
  // a group restored with one view left is an independent view
  const sizes = new Map<string, number>();
  for (const v of out) if (v.group) sizes.set(v.group, (sizes.get(v.group) ?? 0) + 1);
  for (const v of out) if (v.group && sizes.get(v.group)! < 2) delete v.group;
  // a plot whose view is gone (not restored) goes too
  const ids = new Set(out.map((v) => v.id));
  const kept = out.filter((v) => typeof v.params.follows !== 'string' || ids.has(v.params.follows));
  // a linked group alone (the first, at most MAX_GROUP views), else at most MAX_VIEWS tiles
  // (older or edited saves, review B-L6)
  const group = kept.find((v) => v.group)?.group;
  if (group) return kept.filter((v) => v.group === group).slice(0, MAX_GROUP);
  const out2: ViewSpec[] = [];
  for (const v of kept) if (tilesTaken([...out2, v]) <= MAX_VIEWS) out2.push(v);
  return out2.filter((v) => typeof v.params.follows !== 'string' || out2.some((x) => x.id === v.params.follows));
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

function browserStorage(): Storage | null {
  try {
    if (typeof localStorage !== 'undefined' && localStorage) return localStorage;
  } catch {
    // access throws when storage is blocked
  }
  return null;
}

const WRITE_DELAY_MS = 300;

/** Storage that never throws (a full or blocked store falls back to memory) and writes at most
 * every WRITE_DELAY_MS (a slider drag changes the workspace many times a second). */
export function safeStorage(store: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null) {
  let target = store ?? memoryStorage;
  const pending = new Map<string, string>();
  let timer: ReturnType<typeof setTimeout> | null = null;
  const flush = () => {
    timer = null;
    for (const [k, v] of pending) {
      try {
        target.setItem(k, v);
      } catch {
        target = memoryStorage; // quota exceeded or storage blocked: keep going in memory
        memoryStorage.setItem(k, v);
      }
    }
    pending.clear();
  };
  if (typeof window !== 'undefined') window.addEventListener('pagehide', flush);
  return {
    getItem: (k: string) => {
      if (pending.has(k)) return pending.get(k)!;
      try {
        return target.getItem(k);
      } catch {
        return memoryStorage.getItem(k);
      }
    },
    setItem: (k: string, v: string) => {
      pending.set(k, v);
      timer ??= setTimeout(flush, WRITE_DELAY_MS);
    },
    removeItem: (k: string) => {
      pending.delete(k);
      try {
        target.removeItem(k);
      } catch {
        memoryStorage.removeItem(k);
      }
    },
    flush,
  };
}

const storage = createJSONStorage(() => safeStorage(browserStorage()));

export const useWorkspace = create<Workspace>()(
  persist(
    (set, get) => ({
      views: [],
      remembered: {},
      selected: null,
      browserSelection: null,
      ticked: [],
      tick: (ids, on) =>
        set((s) => {
          const t = new Set(s.ticked);
          for (const id of ids) {
            if (on) t.add(id);
            else t.delete(id);
          }
          // ticking starts a linked opening: the details of a dataset clicked before would show
          // one dataset beside the ticked ones (S7b demo)
          return { ticked: [...t], ...(on ? { browserSelection: null } : {}) };
        }),
      addView: (v, after) => {
        // nothing opens next to a linked group (owner)
        const all = get().views;
        if (all.some((x) => x.group) || tilesTaken([...all, { ...v, id: '(new)' }]) > MAX_VIEWS) return null;
        const id = newId();
        set((s) => {
          const at = after ? s.views.findIndex((x) => x.id === after) + 1 : 0;
          const views = [...s.views];
          views.splice(at > 0 ? at : views.length, 0, { ...v, id });
          return { views, selected: id };
        });
        return id;
      },
      addGroup: (vs) => {
        // a group opens on an empty canvas (owner)
        if (vs.length < 1 || get().views.length > 0) return null;
        const group = newGroupId();
        const added = vs.slice(0, MAX_GROUP).map((v) => ({ ...v, id: newId(), ...(vs.length > 1 ? { group } : {}) }));
        set((s) => ({ views: [...s.views, ...added], selected: added[0].id }));
        return group;
      },
      removeView: (id) =>
        set((s) => {
          const gone = s.views.find((v) => v.id === id);
          // a plot that follows this view (its companion) closes with it
          let views = s.views.filter((v) => v.id !== id && v.params.follows !== id);
          const left = gone?.group ? views.filter((v) => v.group === gone.group) : [];
          if (left.length === 1) views = views.map((v) => (v.id === left[0].id ? { ...v, group: undefined } : v));
          const kept = views.some((v) => v.id === s.selected);
          const next = left[0]?.id ?? views.at(-1)?.id ?? null;
          return { views, selected: kept ? s.selected : next };
        }),
      removeGroup: (group) =>
        set((s) => {
          const ids = new Set(s.views.filter((v) => v.group === group).map((v) => v.id));
          const views = s.views.filter((v) => !ids.has(v.id) && !ids.has(v.params.follows as string));
          const kept = views.some((v) => v.id === s.selected);
          return { views, selected: kept ? s.selected : (views.at(-1)?.id ?? null) };
        }),
      updateParams: (id, patch) => {
        const v = get().views.find((x) => x.id === id);
        if (v) get().setParams(id, { ...v.params, ...patch });
      },
      setParams: (id, params) =>
        set((s) => {
          const v = s.views.find((x) => x.id === id);
          if (!v) return {};
          const own = viewType(v.type)?.ownKeys ?? [];
          let remembered = s.remembered;
          const views = s.views.map((x) => {
            if (x.id !== id && (!v.group || x.group !== v.group)) return x;
            const nv = { ...x, params: v.group ? groupParams(x.params, params, own, x.id === id) : params };
            remembered = remember(remembered, nv);
            return nv;
          });
          return { views, remembered };
        }),
      recall: (key) => {
        const r = get().remembered[key];
        const t = r ? viewType(key.split('|')[0]) : undefined;
        return r && t ? (t.normalise(r.params) as Record<string, unknown> | null) : null;
      },
      select: (id) => set({ selected: id }),
      selectDataset: (id) => set({ browserSelection: id }),
      // (ticks stay: closing the views is the way to open the ticked datasets linked)
      // the dataset clicked in the browser stays: closing the views is often the way to open it
      // (review H3 B-L4)
      reset: () => set({ views: [], selected: null }),
    }),
    {
      name: STORAGE_KEY,
      version: VERSION,
      storage,
      skipHydration: true,
      partialize: (s) => ({ views: s.views, selected: s.selected, remembered: s.remembered }),
      // Older saved versions: keep what can be kept; `merge` validates every view anyway.
      // No migration step is needed: `merge` validates and normalises every saved view (per view
      // type), which is where a format change between versions is handled.
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
