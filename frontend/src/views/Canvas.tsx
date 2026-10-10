// The one canvas: a single deck.gl Deck (one WebGL context) with an OrthographicView per open
// view, laid out as a grid. Each view's data and layers come from its view type's hook, run in
// an invisible <ViewRunner>; layers are drawn only in their own view (`layerFilter`).
//
// Each view fits itself to its data, and refits when its cell changes size until the user pans or
// zooms it; from then on pan and zoom are the user's, kept (scaled with the cell) when other views
// open or close. "Fit" refits on demand.

import { type Layer, OrthographicView, type PickingInfo } from '@deck.gl/core';
import DeckGL from '@deck.gl/react';
import { memo, useEffect, useMemo, useRef, useState } from 'react';

import { useWorkspace, type ViewSpec } from '../state/workspace';
import { event, reportEnvironment } from '../telemetry/telemetry';
import { ErrorBoundary } from '../ui/ErrorBoundary';
import { type Cell, fitBounds, gridCells, type OrthoState, resizeView } from './layout';
import { useViewOutputs } from './outputs';
import { type Bounds, type ViewOutput, viewType } from './registry';

const TITLE_ROW = 30; // px kept free for the view's title when fitting
const COMPACT_WIDTH = 480; // cells narrower than this start with the legend folded
const ZOOM_LIMITS = { minZoom: -20, maxZoom: 40 };

const ViewRunner = memo(function ViewRunner({ spec }: { spec: ViewSpec }) {
  const t = viewType(spec.type)!;
  const out = t.useView(spec);
  const publish = useViewOutputs((s) => s.publish);
  useEffect(
    () => publish(spec.id, out),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [spec.id, out.layers, out.bounds, out.status, out.error, out.notice, out.legend],
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
}: {
  spec: ViewSpec;
  cell: Cell;
  output: ViewOutput | undefined;
  selected: boolean;
  onFit: (id: string) => void;
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
      <div className="view-header">
        <button className="view-title" onClick={() => select(spec.id)} title={spec.title}>
          {spec.title}
        </button>
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

export function Canvas() {
  const views = useWorkspace((s) => s.views);
  const selected = useWorkspace((s) => s.selected);
  const select = useWorkspace((s) => s.select);
  const outputs = useViewOutputs((s) => s.outputs);
  const drop = useViewOutputs((s) => s.drop);
  const [ref, size] = useSize<HTMLDivElement>();
  const cells = useMemo(() => gridCells(views.length, size.width, size.height, 2), [views.length, size]);
  const [viewStates, setViewStates] = useState<Record<string, OrthoState>>({});
  // What each view was last fitted to, and the cell size its view state belongs to.
  const fitted = useRef<Record<string, Bounds>>({});
  const cellOf = useRef<Record<string, Cell>>({});
  const touched = useRef<Set<string>>(new Set()); // views the user has panned or zoomed
  const [dragging, setDragging] = useState(false);
  const drawn = useRef<Set<string>>(new Set());
  const readyAt = useRef<Record<string, number>>({});

  // Forget closed views.
  useEffect(() => {
    const open = new Set(views.map((v) => v.id));
    for (const id of Object.keys(fitted.current)) if (!open.has(id)) delete fitted.current[id];
    for (const id of Object.keys(cellOf.current)) if (!open.has(id)) delete cellOf.current[id];
    for (const id of touched.current) if (!open.has(id)) touched.current.delete(id);
    for (const id of Object.keys(outputs)) if (!open.has(id)) drop(id);
  }, [views, outputs, drop]);

  // Fit when a view's data first arrives or changes; follow cell size changes otherwise.
  useEffect(() => {
    const updates: Record<string, OrthoState> = {};
    views.forEach((v, i) => {
      const cell = cells[i];
      const b = outputs[v.id]?.bounds ?? null;
      if (!cell || cell.width < 10) return;
      const prevCell = cellOf.current[v.id];
      const resized = prevCell && (prevCell.width !== cell.width || prevCell.height !== cell.height);
      if (b && (fitted.current[v.id] !== b || (resized && !touched.current.has(v.id)))) {
        fitted.current[v.id] = b;
        updates[v.id] = fitBounds(b, cell, 0.06, TITLE_ROW);
      } else if (resized && viewStates[v.id]) {
        updates[v.id] = resizeView(viewStates[v.id], prevCell, cell);
      }
      cellOf.current[v.id] = cell;
    });
    if (Object.keys(updates).length) setViewStates((s) => ({ ...s, ...updates }));
  }, [views, outputs, cells]); // eslint-disable-line react-hooks/exhaustive-deps

  const fit = (id: string) => {
    const i = views.findIndex((v) => v.id === id);
    const b = outputs[id]?.bounds;
    if (i >= 0 && b && cells[i]) {
      touched.current.delete(id);
      setViewStates((s) => ({ ...s, [id]: fitBounds(b, cells[i], 0.06, TITLE_ROW) }));
    }
  };

  const deckViews = useMemo(
    () =>
      views.map(
        (v, i) =>
          new OrthographicView({
            id: v.id,
            x: cells[i]?.x ?? 0,
            y: cells[i]?.y ?? 0,
            width: cells[i]?.width ?? 1,
            height: cells[i]?.height ?? 1,
            flipY: false,
            controller: { inertia: false, scrollZoom: { smooth: false, speed: 0.02 } },
          }),
      ),
    [views, cells],
  );

  // While dragging, nothing is pickable: hover picking reads pixels back from the GPU, which
  // is wasted work during a pan.
  const layers = useMemo(() => {
    const all = views.flatMap((v) => (outputs[v.id]?.layers ?? []) as Layer[]);
    return dragging ? all.map((l) => l.clone({ pickable: false })) : all;
  }, [views, outputs, dragging]);
  const viewState = Object.fromEntries(
    views.map((v) => [v.id, { ...(viewStates[v.id] ?? { target: [0, 0, 0], zoom: 0 }), ...ZOOM_LIMITS }]),
  );

  useEffect(() => {
    for (const v of views) {
      if (outputs[v.id]?.status === 'ready' && readyAt.current[v.id] === undefined) readyAt.current[v.id] = performance.now();
    }
  }, [views, outputs]);

  // The current zoom of each view as a data attribute (for tests and debugging; no re-render).
  useEffect(() => {
    for (const [id, st] of Object.entries(viewStates)) {
      const el = ref.current?.querySelector<HTMLElement>(`[data-view-id="${id}"]`);
      if (el) el.dataset.zoom = st.zoom.toFixed(3);
    }
  }, [viewStates, ref]);

  const tooltip = (info: PickingInfo) => {
    if (dragging || !info.layer || info.index < 0) return null;
    const id = info.layer.id.split(':')[0];
    const text = outputs[id]?.tooltip?.(info.index);
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
          views={deckViews}
          viewState={viewState}
          onViewStateChange={({ viewId, viewState: vs, interactionState }) => {
            const { target, zoom } = vs as unknown as OrthoState;
            if (interactionState && Object.values(interactionState).some(Boolean)) touched.current.add(viewId);
            setViewStates((s) => ({ ...s, [viewId]: { target, zoom } }));
          }}
          onInteractionStateChange={(s) => setDragging(!!s.isDragging)}
          layers={layers}
          layerFilter={({ layer, viewport }) => layer.id.startsWith(`${viewport.id}:`)}
          getTooltip={tooltip}
          onClick={(info) => info.viewport && select(info.viewport.id)}
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
          style={{ background: 'var(--canvas-bg)' }}
        />
      )}
      {views.map((v, i) =>
        cells[i] ? (
          <ErrorBoundary key={v.id} scope="view" fallback={() => null}>
            <ViewCell spec={v} cell={cells[i]} output={outputs[v.id]} selected={v.id === selected} onFit={fit} />
          </ErrorBoundary>
        ) : null,
      )}
    </div>
  );
}
