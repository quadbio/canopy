// The one canvas: a single deck.gl Deck (one WebGL context) with an OrthographicView per panel
// of each open view; views are laid out as a grid, and a view with several panels (e.g. one per
// section) as a grid inside its cell. Each view's data and layers come from its view type's
// hook, run in an invisible <ViewRunner>; layers are drawn only in their own panel
// (`layerFilter`, by layer id prefix).
//
// Each view fits itself to its data, and refits when its cell changes size (or its legend opens
// or closes) until the user pans or zooms it; from then on pan and zoom are the user's, kept
// (scaled with the cell) when other views open or close. "Fit" refits on demand. The panels of a
// view pan and zoom together (`link`); fitted together they share one zoom, so sizes compare.

import { type Layer, OrthographicView, type PickingInfo } from '@deck.gl/core';
import DeckGL from '@deck.gl/react';
import { memo, type ReactNode, useEffect, useMemo, useRef, useState } from 'react';

import { useWorkspace, type ViewSpec } from '../state/workspace';
import { deckErrorFields, event, reportEnvironment } from '../telemetry/telemetry';
import { Conversion } from '../ui/Conversion';
import { ErrorBoundary } from '../ui/ErrorBoundary';
import { type Cell, gridCells, type Insets, type OrthoState, resizeView } from './layout';
import { allowances, snappedExtent, useLod, visibleShare } from './lod';
import { useViewOutputs } from './outputs';
import { type FitLayout, fitLayout, linkedStates, panelRects } from './panels';
import { type Bounds, type Link, ownerOf, panelKey, panelsOf, type ViewOutput, viewType } from './registry';

const TITLE_ROW = 30; // px kept free for the view's title when fitting
const PANEL_TITLE = 18; // px kept free at the top of each panel for its title
const LEGEND_SPACE = 250; // px kept free at the right for an open legend
const COMPACT_WIDTH = 480; // cells narrower than this start with the legend folded
const ZOOM_LIMITS = { minZoom: -20, maxZoom: 40 };

/** Where a view's panels go and what is kept free when fitting them. */
interface Layout extends FitLayout {
  cell: Cell; // keys: panel keys (deck.gl view ids); fitInsets: per panel
  link: Link;
  titles: (string | undefined)[];
  fitKey: string | undefined;
}

/** Insets plus a view's own margins (e.g. room for axes). */
function withMargins(base: Insets, m: Insets | undefined): Insets {
  if (!m) return base;
  return {
    top: (base.top ?? 0) + (m.top ?? 0),
    right: (base.right ?? 0) + (m.right ?? 0),
    bottom: (base.bottom ?? 0) + (m.bottom ?? 0),
    left: (base.left ?? 0) + (m.left ?? 0),
  };
}

function layoutOf(spec: ViewSpec, cell: Cell, out: ViewOutput | undefined): Layout {
  const panels = panelsOf(out);
  const compact = cell.width < COMPACT_WIDTH;
  const legendOpen = !!out?.legend && !((spec.params.legendCollapsed as boolean | undefined) ?? compact);
  const right = legendOpen ? LEGEND_SPACE : 0;
  const many = panels.length > 1;
  return {
    cell,
    keys: panels.map((p) => panelKey(spec.id, p.id)),
    rects: panelRects(panels.length, cell, { top: TITLE_ROW, right }),
    fitInsets: withMargins(many ? { top: PANEL_TITLE } : { top: TITLE_ROW, right }, out?.margins),
    link: out?.link ?? 'pan+zoom',
    sameScale: out?.sameScale ?? true,
    sameCoordinates: !!out?.sameCoordinates && many,
    flipY: !!out?.flipY,
    bounds: panels.map((p) => p.bounds),
    titles: panels.map((p) => p.title),
    fitKey: out?.fitKey,
  };
}

const ViewRunner = memo(function ViewRunner({ spec }: { spec: ViewSpec }) {
  const t = viewType(spec.type)!;
  const out = t.useView(spec);
  const publish = useViewOutputs((s) => s.publish);
  // `tooltip` changes together with `layers` (the drawn order), so it is not listed.
  useEffect(
    () => publish(spec.id, out),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      spec.id,
      out.layers,
      out.bounds,
      out.panels,
      out.link,
      out.sameScale,
      out.flipY,
      out.status,
      out.error,
      out.needsConversion,
      out.notice,
      out.problem?.message,
      out.legend,
      out.caption,
      out.total,
      out.content,
      out.margins,
      out.fitKey,
    ],
  );
  return null;
});

/** Shown in place of a view whose hook crashed: the view reports the error in its cell. */
function RunnerFailed({ id, error }: { id: string; error: Error }) {
  const publish = useViewOutputs((s) => s.publish);
  useEffect(() => publish(id, { layers: [], bounds: null, status: 'error', error: `This view failed: ${error.message}` }), [id, error, publish]);
  return null;
}

function useSize<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) =>
      setSize({ width: Math.round(e.contentRect.width), height: Math.round(e.contentRect.height) }),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}

const ViewCell = memo(function ViewCell({
  spec,
  cell,
  output,
  selected,
  onFit,
  panels,
  overlays,
}: {
  spec: ViewSpec;
  cell: Cell;
  output: ViewOutput | undefined;
  selected: boolean;
  onFit: (id: string) => void;
  panels: { key: string; title?: string; rect: Cell }[];
  /** Panels drawing an overlay (axes), with their current view states. */
  overlays?: { key: string; rect: Cell; state: OrthoState; taken: Insets; draw: (s: OrthoState, r: Cell, t: Insets) => ReactNode }[];
}) {
  const select = useWorkspace((s) => s.select);
  const remove = useWorkspace((s) => s.removeView);
  const t = viewType(spec.type);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    if (output?.notice) setNotice(output.notice);
  }, [output?.notice]);
  const compact = cell.width < COMPACT_WIDTH;
  return (
    <div
      className={`view-cell${selected ? ' selected' : ''}`}
      style={{ left: cell.x, top: cell.y, width: cell.width, height: cell.height }}
      data-testid="view-cell"
      data-status={output?.status ?? 'loading'}
      data-view-id={spec.id}
    >
      {panels.length > 1 &&
        panels.map((p) => (
          <div
            key={p.key}
            className="view-panel-frame"
            data-testid="panel"
            style={{ left: p.rect.x - cell.x, top: p.rect.y - cell.y, width: p.rect.width, height: p.rect.height }}
          >
            {p.title && (
              <span className="view-panel-title" title={p.title}>
                {p.title}
              </span>
            )}
          </div>
        ))}
      {overlays?.map((o) => (
        <div
          key={o.key}
          className="view-overlay"
          style={{ left: o.rect.x - cell.x, top: o.rect.y - cell.y, width: o.rect.width, height: o.rect.height }}
        >
          {o.draw(o.state, o.rect, o.taken)}
        </div>
      ))}
      {output?.content && output.status !== 'error' && (
        <div
          className="view-content"
          data-testid="view-content"
          style={{
            top: TITLE_ROW,
            right: !!output.legend && !((spec.params.legendCollapsed as boolean | undefined) ?? compact) ? LEGEND_SPACE : 0,
          }}
        >
          {output.content}
        </div>
      )}
      <div className="view-header">
        <button className="view-title" onClick={() => select(spec.id)} title={spec.title}>
          {spec.title}
        </button>
        {output?.caption && (
          <span className="view-caption" data-testid="view-caption">
            {output.caption}
          </span>
        )}
        {output?.status === 'loading' && <span className="spinner" aria-label="Loading" />}
        <span className="view-header-actions">
          <button className="icon-button" onClick={() => onFit(spec.id)} aria-label="Fit to data" title="Fit to data">
            ⤢
          </button>
          <button className="icon-button" onClick={() => remove(spec.id)} aria-label="Close view" title="Close view">
            ×
          </button>
        </span>
      </div>
      {output?.status === 'idle' && (
        <div className="view-error view-idle" role="status" data-testid="view-idle">
          {output.error}
        </div>
      )}
      {output?.status === 'error' && (
        <div className="view-error" role="alert" data-testid="view-error">
          {output.error}
          {output.needsConversion && <Conversion datasetId={output.needsConversion} compact />}
        </div>
      )}
      {output?.problem && (
        <div className="view-notice problem" role="alert" data-testid="view-problem">
          {output.problem.message}
          {output.problem.retry && (
            <button className="link" onClick={output.problem.retry}>
              Retry
            </button>
          )}
        </div>
      )}
      {notice && notice !== dismissed && (
        <div className="view-notice" role="status">
          {notice}
          <button className="link" onClick={() => setDismissed(notice)}>
            OK
          </button>
        </div>
      )}
      {t?.Legend && output?.status === 'ready' && (
        <div className={`view-legend${compact ? ' compact' : ''}`}>
          <ErrorBoundary scope="view" fallback={() => null}>
            <t.Legend spec={spec} output={output} compact={compact} />
          </ErrorBoundary>
        </div>
      )}
    </div>
  );
});

// deck.gl reports a broken layer on every frame: one event per kind of error and 30 s, counted
const deckErrors = new Map<string, { at: number; n: number }>();
function reportDeckError(e: Error | null | undefined) {
  const f = deckErrorFields(e);
  const key = `${f.layer}|${f.message}`;
  const now = performance.now();
  const seen = deckErrors.get(key);
  if (seen && now - seen.at < 30_000) {
    seen.n += 1;
    return;
  }
  deckErrors.set(key, { at: now, n: 0 });
  event('deck_error', null, { ...f, repeats: seen?.n ?? 0 });
}

/** Frame times while the canvas redraws (interaction, loading): one `frames` event per 10 s
 * with frames, median and 95th percentile intervals and the long ones (> 50 ms). Intervals over
 * 250 ms are pauses, not frames. */
class FrameStats {
  private last = 0;
  private gaps: number[] = [];
  private since = performance.now();

  frame() {
    const now = performance.now();
    const gap = now - this.last;
    this.last = now;
    if (gap > 0 && gap <= 250) this.gaps.push(gap);
    if (now - this.since >= 10_000) this.flush(now);
  }

  private flush(now: number) {
    this.since = now;
    const g = this.gaps.sort((a, b) => a - b);
    this.gaps = [];
    if (g.length < 10 && !g.some((x) => x > 50)) return; // a stall is reported however few frames
    const q = (f: number) => g[Math.min(g.length - 1, Math.floor(f * g.length))];
    event('frames', null, { n: g.length, median_ms: Math.round(q(0.5) * 10) / 10, p95_ms: Math.round(q(0.95) * 10) / 10, over_50ms: g.filter((x) => x > 50).length });
  }
}

/** The top-level layer a (sub-)layer belongs to. */
function rootOf(layer: Layer): Layer {
  let l = layer;
  while (l.parent) l = l.parent as Layer;
  return l;
}

const NO_PANELS: { key: string; title?: string; rect: Cell }[] = [];
const panelCache = new WeakMap<Layout, { key: string; title?: string; rect: Cell }[]>();
/** Stable per layout, so the memoised cell re-renders only when its panels change. */
function panelsFor(l: Layout | null) {
  if (!l || l.keys.length < 2) return NO_PANELS;
  let out = panelCache.get(l);
  if (!out) panelCache.set(l, (out = l.keys.map((key, j) => ({ key, title: l.titles[j], rect: l.rects[j] }))));
  return out;
}

/** Panels with an overlay (axes) and their current view states (others: none, so their cells do
 * not re-render while panning). */
function overlaysFor(out: ViewOutput | undefined, l: Layout | null, states: Record<string, OrthoState>) {
  if (!out || !l) return undefined;
  const panels = panelsOf(out);
  if (!panels.some((p) => p.overlay)) return undefined;
  return panels.flatMap((p, j) => {
    const st = states[l.keys[j]];
    // the panel's insets without the view's own margins: what the title and legend take
    const m = out.margins ?? {};
    const f = l.fitInsets;
    const taken = { top: (f.top ?? 0) - (m.top ?? 0), right: (f.right ?? 0) - (m.right ?? 0) };
    return p.overlay && st ? [{ key: l.keys[j], rect: l.rects[j], state: st, taken, draw: p.overlay }] : [];
  });
}

export function Canvas() {
  const views = useWorkspace((s) => s.views);
  const selected = useWorkspace((s) => s.selected);
  const select = useWorkspace((s) => s.select);
  const outputs = useViewOutputs((s) => s.outputs);
  const drop = useViewOutputs((s) => s.drop);
  const [ref, size] = useSize<HTMLDivElement>();
  const cells = useMemo(() => gridCells(views.length, size.width, size.height, 2), [views.length, size]);
  const [viewStates, setViewStates] = useState<Record<string, OrthoState>>({});
  const layouts = useMemo(
    () => views.map((v, i) => (cells[i] ? layoutOf(v, cells[i], outputs[v.id]) : null)),
    [views, cells, outputs],
  );
  // Per panel: the bounds it was last fitted to, its fitted view state (linked panels follow
  // relative to it), and the rectangle and insets its view state belongs to.
  const fitted = useRef<Record<string, Bounds>>({});
  const fittedKey = useRef<Record<string, string | undefined>>({});
  const fits = useRef<Record<string, OrthoState>>({});
  const placed = useRef<Record<string, { rect: Cell; insets: string }>>({});
  const touched = useRef<Set<string>>(new Set()); // views the user has panned or zoomed
  const [dragging, setDragging] = useState(false);
  const drawn = useRef<Set<string>>(new Set());
  const frames = useRef(new FrameStats());
  // panels per view, reported when they change (e.g. Split by)
  const panelCounts = layouts.map((l) => l?.keys.length ?? 0).join(',');
  useEffect(() => {
    layouts.forEach((l, i) => {
      if (l && l.keys.length > 1) event('panels', null, { type: views[i]?.type ?? '', n: l.keys.length, same_coordinates: l.sameCoordinates, same_scale: l.sameScale });
    });
  }, [panelCounts]); // eslint-disable-line react-hooks/exhaustive-deps
  const readyAt = useRef<Record<string, number>>({});

  // Forget closed views and panels.
  useEffect(() => {
    const open = new Set(views.map((v) => v.id));
    const keys = new Set(layouts.flatMap((l) => l?.keys ?? []));
    for (const m of [fitted.current, fittedKey.current, fits.current, placed.current]) {
      for (const k of Object.keys(m)) if (!keys.has(k)) delete m[k];
    }
    for (const id of touched.current) if (!open.has(id)) touched.current.delete(id);
    for (const id of Object.keys(outputs)) if (!open.has(id)) drop(id);
  }, [views, outputs, drop, layouts]);

  // Fit when a view's data first arrives or changes; follow size changes otherwise.
  useEffect(() => {
    const updates: Record<string, OrthoState> = {};
    views.forEach((v, i) => {
      const l = layouts[i];
      if (!l || l.cell.width < 10) return;
      const insets = `${JSON.stringify(l.fitInsets)}|${l.sameScale}|${l.sameCoordinates}`;
      let refit = false;
      const resized: string[] = [];
      const user = touched.current.has(v.id);
      l.keys.forEach((key, j) => {
        const b = l.bounds[j];
        const prev = placed.current[key];
        const moved = prev && (prev.rect.width !== l.rects[j].width || prev.rect.height !== l.rects[j].height || prev.insets !== insets);
        // new data: a new fit key, or (without a key) new bounds; more of the same data
        // (new bounds, same key) refits only a view the user has not moved
        const keyed = l.fitKey !== undefined && fittedKey.current[key] !== undefined;
        const newData = keyed ? fittedKey.current[key] !== l.fitKey || (fitted.current[key] !== b && !user) : fitted.current[key] !== b;
        if (b && (newData || (moved && !user))) refit = true;
        else if (moved && viewStates[key]) resized.push(key);
      });
      if (refit) {
        for (const [key, st] of Object.entries(fitLayout(l))) {
          updates[key] = st;
          fits.current[key] = st;
        }
        l.keys.forEach((key, j) => {
          if (l.bounds[j]) {
            fitted.current[key] = l.bounds[j]!;
            fittedKey.current[key] = l.fitKey;
          }
        });
      } else {
        for (const key of resized) {
          const j = l.keys.indexOf(key);
          updates[key] = resizeView(viewStates[key], placed.current[key].rect, l.rects[j]);
          if (fits.current[key]) fits.current[key] = resizeView(fits.current[key], placed.current[key].rect, l.rects[j]);
        }
      }
      l.keys.forEach((key, j) => (placed.current[key] = { rect: l.rects[j], insets }));
    });
    if (Object.keys(updates).length) setViewStates((s) => ({ ...s, ...updates }));
  }, [views, layouts]); // eslint-disable-line react-hooks/exhaustive-deps

  const fit = (id: string) => {
    const l = layouts[views.findIndex((v) => v.id === id)];
    if (!l) return;
    touched.current.delete(id);
    const states = fitLayout(l);
    Object.assign(fits.current, states);
    setViewStates((s) => ({ ...s, ...states }));
  };

  const deckViews = useMemo(
    () =>
      layouts.flatMap((l) =>
        l
          ? l.keys.map(
              (key, j) =>
                new OrthographicView({
                  id: key,
                  x: l.rects[j].x,
                  y: l.rects[j].y,
                  width: l.rects[j].width,
                  height: l.rects[j].height,
                  flipY: l.flipY,
                  controller: { inertia: false, scrollZoom: { smooth: false, speed: 0.02 } },
                }),
            )
          : [],
      ),
    [layouts],
  );

  // While dragging, nothing is pickable: hover picking reads pixels back from the GPU, which
  // is wasted work during a pan.
  // deck.gl releases a layer's GPU buffers when it leaves the canvas. A view may hand back the
  // same layer object later (e.g. its single-panel layers after "Split by" returns to None);
  // drawn again it would have no buffers (blank, S6 feedback). Such a returning object is
  // replaced by a fresh copy.
  const shown = useRef<Set<Layer>>(new Set());
  const layers = useMemo(() => {
    const all = views.flatMap((v) => panelsOf(outputs[v.id]).flatMap((p) => p.layers as Layer[]));
    const fresh = all.map((l) => (shown.current.has(l) || !l.internalState ? l : l.clone({})));
    shown.current = new Set(fresh);
    return dragging ? fresh.map((l) => l.clone({ pickable: false })) : fresh;
  }, [views, outputs, dragging]);
  const viewState = Object.fromEntries(
    deckViews.map((dv) => [dv.id, { ...(viewStates[dv.id] ?? { target: [0, 0, 0], zoom: 0 }), ...ZOOM_LIMITS }]),
  );

  useEffect(() => {
    for (const v of views) {
      if (outputs[v.id]?.status === 'ready' && readyAt.current[v.id] === undefined) readyAt.current[v.id] = performance.now();
    }
  }, [views, outputs]);

  // How much of each view's data is on screen, for level of detail (views/lod.ts): the largest
  // share among its panels.
  const setVisible = useLod((s) => s.setVisible);
  const setAllowance = useLod((s) => s.setAllowance);
  const forgetLod = useLod((s) => s.forget);
  useEffect(() => {
    views.forEach((v, i) => {
      const l = layouts[i];
      if (!l) return;
      let share = 0;
      l.keys.forEach((key, j) => {
        const b = l.bounds[j];
        const st = viewStates[key];
        if (b && st) share = Math.max(share, visibleShare(b, l.rects[j].width, l.rects[j].height, st.zoom));
      });
      if (share > 0) setVisible(v.id, share);
    });
  }, [views, layouts, viewStates, setVisible]);
  // The area each view shows (its first panel), for views that read only data in view.
  const setExtent = useLod((s) => s.setExtent);
  useEffect(() => {
    views.forEach((v, i) => {
      const l = layouts[i];
      const st = l && viewStates[l.keys[0]];
      if (l && st) setExtent(v.id, snappedExtent(st.target, st.zoom, l.rects[0].width, l.rects[0].height));
    });
  }, [views, layouts, viewStates, setExtent]);
  const setSize = useLod((s) => s.setSize);
  useEffect(() => {
    views.forEach((v, i) => {
      // the area its data is fitted into: the first panel less the title row, an open legend
      // and the view's own margins (axes), so views never re-derive the layout's constants
      const l = layouts[i];
      const r = l?.rects[0];
      if (!r) return;
      const { top = 0, right = 0, bottom = 0, left = 0 } = l.fitInsets;
      setSize(v.id, Math.max(r.width - left - right, 1), Math.max(r.height - top - bottom, 1));
    });
  }, [views, layouts, setSize]);
  // One point budget for the whole canvas, shared by the views in proportion to their cells.
  const totalsKey = views.map((v) => `${v.id}:${outputs[v.id]?.total ?? 0}`).join(',');
  useEffect(() => {
    setAllowance(allowances(Object.fromEntries(views.map((v) => [v.id, outputs[v.id]?.total ?? 0]))));
  }, [totalsKey, setAllowance]); // eslint-disable-line react-hooks/exhaustive-deps
  // Forget the views that were closed (only those).
  const openIds = useRef<string[]>([]);
  useEffect(() => {
    const now = views.map((v) => v.id);
    const gone = openIds.current.filter((id) => !now.includes(id));
    if (gone.length) forgetLod(gone);
    openIds.current = now;
  }, [views, forgetLod]);

  // The current zoom of each view (its first panel) as a data attribute (for tests and
  // debugging; no re-render).
  useEffect(() => {
    layouts.forEach((l, i) => {
      const st = l && viewStates[l.keys[0]];
      const el = ref.current?.querySelector<HTMLElement>(`[data-view-id="${views[i]?.id}"]`);
      if (el && st) el.dataset.zoom = st.zoom.toFixed(3);
    });
  }, [viewStates, layouts, views, ref]);

  // hovers per 10 s (telemetry `hover`): pointer moves, cells hit, values shown
  const hovers = useRef({ moves: 0, picked: 0, shown: 0 });
  useEffect(() => {
    const t = setInterval(() => {
      const h = hovers.current;
      if (h.moves) event('hover', null, { ...h });
      hovers.current = { moves: 0, picked: 0, shown: 0 };
    }, 10_000);
    return () => clearInterval(t);
  }, []);
  const tooltip = (info: PickingInfo) => {
    if (dragging) return null;
    hovers.current.moves += 1;
    if (!info.layer || info.index < 0) return null;
    hovers.current.picked += 1;
    const root = rootOf(info.layer).id;
    const { view, panel } = ownerOf(root);
    // the view's "Show values on hover" (on unless switched off)
    if (views.find((v) => v.id === view)?.params.hover === false) return null;
    const text = panelsOf(outputs[view]).find((p) => p.id === panel)?.tooltip?.(info.index, root);
    if (text) hovers.current.shown += 1;
    return text ?? null;
  };
  // The hover text is drawn here at the pointer, not by deck.gl: deck.gl placed its tooltip off
  // screen on high-density (Retina) displays (S6 feedback). Positioned without re-rendering.
  const tipRef = useRef<HTMLDivElement>(null);
  const pointer = useRef({ x: 0, y: 0 });
  const placeTip = () => {
    const el = tipRef.current;
    const box = ref.current?.getBoundingClientRect();
    if (!el || !box) return;
    const { x, y } = pointer.current;
    const right = x + 16 + el.offsetWidth > box.width; // keep it inside the canvas
    const below = y + 16 + el.offsetHeight > box.height;
    el.style.left = `${right ? x - 12 - el.offsetWidth : x + 14}px`;
    el.style.top = `${below ? y - 12 - el.offsetHeight : y + 14}px`;
  };
  const showTip = (text: string | null) => {
    const el = tipRef.current;
    if (!el) return;
    if (!text) {
      el.style.display = 'none';
      return;
    }
    if (el.textContent !== text) el.textContent = text;
    el.style.display = 'block';
    placeTip();
  };

  return (
    <div
      className="canvas"
      ref={ref}
      data-testid="canvas"
      onPointerMove={(e) => {
        const box = ref.current!.getBoundingClientRect();
        pointer.current = { x: e.clientX - box.left, y: e.clientY - box.top };
        placeTip();
      }}
      onPointerLeave={() => showTip(null)}
    >
      {views.map((v) => (
        <ErrorBoundary key={v.id} scope="view" fallback={(e) => <RunnerFailed id={v.id} error={e} />}>
          <ViewRunner spec={v} />
        </ErrorBoundary>
      ))}
      {views.length === 0 && (
        <div className="empty">
          <h2>No views open</h2>
          <p>Pick a dataset in the file browser and choose “Open”.</p>
        </div>
      )}
      {views.length > 0 && size.width > 0 && (
        <DeckGL
          ref={(r) => {
            // For tests and debugging in the browser console (`__canopyDeck.deck`).
            (window as unknown as { __canopyDeck?: unknown }).__canopyDeck = r ?? undefined;
          }}
          views={deckViews}
          viewState={viewState}
          onViewStateChange={({ viewId, viewState: vs, interactionState }) => {
            const { target, zoom } = vs as unknown as OrthoState;
            const owner = ownerOf(viewId).view;
            if (interactionState && Object.values(interactionState).some(Boolean)) touched.current.add(owner);
            const l = layouts[views.findIndex((v) => v.id === owner)];
            setViewStates((s) => {
              if (!l || l.keys.length < 2) return { ...s, [viewId]: { target, zoom } };
              const mine = Object.fromEntries(l.keys.filter((k) => s[k]).map((k) => [k, s[k]]));
              return { ...s, ...linkedStates(l.link, viewId, { target, zoom }, mine, fits.current) };
            });
          }}
          onInteractionStateChange={(s) => {
            setDragging(!!s.isDragging);
            if (s.isDragging) showTip(null);
          }}
          layers={layers}
          // by the top-level layer's id: sub-layers (e.g. Viv's image tiles) have their own ids
          layerFilter={({ layer, viewport }) => rootOf(layer).id.startsWith(`${viewport.id}:`)}
          onHover={(info) => showTip(tooltip(info))}
          // a few pixels of tolerance: cells are small when zoomed out
          pickingRadius={3}
          onClick={(info) => info.viewport && select(ownerOf(info.viewport.id).view)}
          onAfterRender={() => {
            frames.current.frame();
            for (const v of views) {
              const t0 = readyAt.current[v.id];
              if (t0 !== undefined && !drawn.current.has(v.id)) {
                drawn.current.add(v.id);
                event('view_first_draw', performance.now() - t0, { type: v.type, view: v.id, dataset: v.datasetId, n: outputs[v.id]?.n ?? 0 });
              }
            }
          }}
          onDeviceInitialized={(device) => {
            const info = (device as unknown as { info?: { renderer?: string; vendor?: string; version?: string } }).info;
            reportEnvironment(info ?? null);
          }}
          onError={reportDeckError}
          // No depth test by default: layers draw in order. A layer that needs depth (the points'
          // "higher values on top") sets it on itself, so it never hides layers drawn later.
          parameters={{ depthCompare: 'always', depthWriteEnabled: false }}
          style={{ background: 'var(--canvas-bg)' }}
        />
      )}
      {views.map((v, i) =>
        cells[i] ? (
          <ErrorBoundary key={v.id} scope="view" fallback={() => null}>
            <ViewCell
              spec={v}
              cell={cells[i]}
              output={outputs[v.id]}
              selected={v.id === selected}
              onFit={fit}
              panels={panelsFor(layouts[i])}
              overlays={overlaysFor(outputs[v.id], layouts[i], viewStates)}
            />
          </ErrorBoundary>
        ) : null,
      )}
      <div ref={tipRef} className="hover-tip" data-testid="hover-tip" role="tooltip" style={{ display: 'none' }} />
    </div>
  );
}
