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
import { memo, useEffect, useMemo, useRef, useState } from 'react';

import { useWorkspace, type ViewSpec } from '../state/workspace';
import { event, reportEnvironment } from '../telemetry/telemetry';
import { ErrorBoundary } from '../ui/ErrorBoundary';
import { type Cell, fitBounds, gridCells, type Insets, type OrthoState, resizeView } from './layout';
import { allowances, useLod, visibleShare } from './lod';
import { useViewOutputs } from './outputs';
import { linkedStates, panelRects } from './panels';
import { type Bounds, type Link, ownerOf, panelKey, panelsOf, type ViewOutput, viewType } from './registry';

const TITLE_ROW = 30; // px kept free for the view's title when fitting
const PANEL_TITLE = 18; // px kept free at the top of each panel for its title
const LEGEND_SPACE = 250; // px kept free at the right for an open legend
const COMPACT_WIDTH = 480; // cells narrower than this start with the legend folded
const ZOOM_LIMITS = { minZoom: -20, maxZoom: 40 };

/** Where a view's panels go and what is kept free when fitting them. */
interface Layout {
  cell: Cell;
  keys: string[]; // panel keys (deck.gl view ids)
  rects: Cell[];
  fitInsets: Insets; // per panel
  link: Link;
  sameScale: boolean;
  flipY: boolean;
  bounds: (Bounds | null)[];
  titles: (string | undefined)[];
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
    fitInsets: many ? { top: PANEL_TITLE } : { top: TITLE_ROW, right },
    link: out?.link ?? 'pan+zoom',
    sameScale: out?.sameScale ?? true,
    flipY: !!out?.flipY,
    bounds: panels.map((p) => p.bounds),
    titles: panels.map((p) => p.title),
  };
}

/** Fitted view states of all panels of a view; with `sameScale` they share the smallest zoom. */
function fitLayout(l: Layout): Record<string, OrthoState> {
  const fits = l.keys.map((_, j) => (l.bounds[j] ? fitBounds(l.bounds[j]!, l.rects[j], 0.06, l.fitInsets, l.flipY) : null));
  const out: Record<string, OrthoState> = {};
  const zooms = fits.filter((f): f is OrthoState => !!f).map((f) => f.zoom);
  const common = l.sameScale && zooms.length > 1 ? Math.min(...zooms) : null;
  l.keys.forEach((key, j) => {
    const f = fits[j];
    if (!f) return;
    if (common === null) {
      out[key] = f;
      return;
    }
    // the same centre, at the shared zoom (the inset shift depends on the zoom)
    const b = l.bounds[j]!;
    const scale = 2 ** common;
    const up = (l.fitInsets.top ?? 0) / 2 / scale;
    out[key] = { target: [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2 + (l.flipY ? -up : up), 0], zoom: common };
  });
  return out;
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
      out.notice,
      out.problem?.message,
      out.legend,
      out.caption,
      out.total,
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
}: {
  spec: ViewSpec;
  cell: Cell;
  output: ViewOutput | undefined;
  selected: boolean;
  onFit: (id: string) => void;
  panels: { key: string; title?: string; rect: Cell }[];
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
            {p.title && <span className="view-panel-title">{p.title}</span>}
          </div>
        ))}
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
      {output?.status === 'error' && (
        <div className="view-error" role="alert">
          {output.error}
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

const NO_PANELS: { key: string; title?: string; rect: Cell }[] = [];
const panelCache = new WeakMap<Layout, { key: string; title?: string; rect: Cell }[]>();
/** Stable per layout, so the memoised cell re-renders only when its panels change. */
function panelsFor(l: Layout | null) {
  if (!l || l.keys.length < 2) return NO_PANELS;
  let out = panelCache.get(l);
  if (!out) panelCache.set(l, (out = l.keys.map((key, j) => ({ key, title: l.titles[j], rect: l.rects[j] }))));
  return out;
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
  const fits = useRef<Record<string, OrthoState>>({});
  const placed = useRef<Record<string, { rect: Cell; insets: string }>>({});
  const touched = useRef<Set<string>>(new Set()); // views the user has panned or zoomed
  const [dragging, setDragging] = useState(false);
  const drawn = useRef<Set<string>>(new Set());
  const readyAt = useRef<Record<string, number>>({});

  // Forget closed views and panels.
  useEffect(() => {
    const open = new Set(views.map((v) => v.id));
    const keys = new Set(layouts.flatMap((l) => l?.keys ?? []));
    for (const m of [fitted.current, fits.current, placed.current]) {
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
      const insets = `${JSON.stringify(l.fitInsets)}|${l.sameScale}`;
      let refit = false;
      const resized: string[] = [];
      l.keys.forEach((key, j) => {
        const b = l.bounds[j];
        const prev = placed.current[key];
        const moved = prev && (prev.rect.width !== l.rects[j].width || prev.rect.height !== l.rects[j].height || prev.insets !== insets);
        if (b && (fitted.current[key] !== b || (moved && !touched.current.has(v.id)))) refit = true;
        else if (moved && viewStates[key]) resized.push(key);
      });
      if (refit) {
        for (const [key, st] of Object.entries(fitLayout(l))) {
          updates[key] = st;
          fits.current[key] = st;
        }
        l.keys.forEach((key, j) => {
          if (l.bounds[j]) fitted.current[key] = l.bounds[j]!;
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
  const layers = useMemo(() => {
    const all = views.flatMap((v) => panelsOf(outputs[v.id]).flatMap((p) => p.layers as Layer[]));
    return dragging ? all.map((l) => l.clone({ pickable: false })) : all;
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

  const tooltip = (info: PickingInfo) => {
    if (dragging || !info.layer || info.index < 0) return null;
    const { view, panel } = ownerOf(info.layer.id);
    const text = panelsOf(outputs[view]).find((p) => p.id === panel)?.tooltip?.(info.index);
    return text ? { text, className: 'deck-tooltip' } : null;
  };

  return (
    <div className="canvas" ref={ref} data-testid="canvas">
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
            // For tests and debugging in the browser console (`__dbxDeck.deck`).
            (window as unknown as { __dbxDeck?: unknown }).__dbxDeck = r ?? undefined;
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
          onInteractionStateChange={(s) => setDragging(!!s.isDragging)}
          layers={layers}
          layerFilter={({ layer, viewport }) => layer.id.startsWith(`${viewport.id}:`)}
          getTooltip={tooltip}
          onClick={(info) => info.viewport && select(ownerOf(info.viewport.id).view)}
          onAfterRender={() => {
            for (const v of views) {
              const t0 = readyAt.current[v.id];
              if (t0 !== undefined && !drawn.current.has(v.id)) {
                drawn.current.add(v.id);
                event('view_first_draw', performance.now() - t0, { type: v.type, n: outputs[v.id]?.n ?? 0 });
              }
            }
          }}
          onDeviceInitialized={(device) => {
            const info = (device as unknown as { info?: { renderer?: string; vendor?: string; version?: string } }).info;
            reportEnvironment(info ?? null);
          }}
          onError={(e) => event('deck_error', null, { type: e?.name ?? 'Error' })}
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
            />
          </ErrorBoundary>
        ) : null,
      )}
    </div>
  );
}
