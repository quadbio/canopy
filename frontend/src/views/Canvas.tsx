// The one canvas: a single deck.gl Deck (one WebGL context) with an OrthographicView per open
// view, laid out as a grid. Each view's data and layers come from its view type's hook, run in
// an invisible <ViewRunner>; layers are drawn only in their own view (`layerFilter`).

import { type Layer, OrthographicView, type PickingInfo } from '@deck.gl/core';
import DeckGL from '@deck.gl/react';
import { useEffect, useMemo, useRef, useState } from 'react';

import { useWorkspace, type ViewSpec } from '../state/workspace';
import { event, reportEnvironment } from '../telemetry/telemetry';
import { fitBounds, gridCells } from './layout';
import { useViewOutputs } from './outputs';
import { type Bounds, viewType } from './registry';

type OrthoState = { target: [number, number, number]; zoom: number; minZoom?: number; maxZoom?: number };

function ViewRunner({ spec }: { spec: ViewSpec }) {
  const t = viewType(spec.type);
  const out = t!.useView(spec);
  const publish = useViewOutputs((s) => s.publish);
  const drop = useViewOutputs((s) => s.drop);
  useEffect(() => publish(spec.id, out), [spec.id, out.layers, out.bounds, out.status, out.error]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => drop(spec.id), [spec.id, drop]);
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

export function Canvas() {
  const views = useWorkspace((s) => s.views);
  const selected = useWorkspace((s) => s.selected);
  const select = useWorkspace((s) => s.select);
  const remove = useWorkspace((s) => s.removeView);
  const outputs = useViewOutputs((s) => s.outputs);
  const [ref, size] = useSize<HTMLDivElement>();
  const cells = useMemo(() => gridCells(views.length, size.width, size.height, 2), [views.length, size]);
  const [viewStates, setViewStates] = useState<Record<string, OrthoState>>({});
  const fitted = useRef<Record<string, { bounds: Bounds; width: number; height: number }>>({});

  // Fit each view to its data when the data (bounds) arrives or changes, and when its cell is
  // resized (views added or removed, window resized).
  useEffect(() => {
    const updates: Record<string, OrthoState> = {};
    views.forEach((v, i) => {
      const b = outputs[v.id]?.bounds ?? null;
      const cell = cells[i];
      const prev = fitted.current[v.id];
      if (!b || !cell || cell.width < 10) return;
      if (prev && prev.bounds === b && prev.width === cell.width && prev.height === cell.height) return;
      fitted.current[v.id] = { bounds: b, width: cell.width, height: cell.height };
      updates[v.id] = { ...fitBounds(b, cell), minZoom: -20, maxZoom: 40 };
    });
    if (Object.keys(updates).length) setViewStates((s) => ({ ...s, ...updates }));
  }, [views, outputs, cells]);

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

  const layers = useMemo(() => views.flatMap((v) => (outputs[v.id]?.layers ?? []) as Layer[]), [views, outputs]);
  const viewState = Object.fromEntries(
    views.map((v) => [v.id, viewStates[v.id] ?? { target: [0, 0, 0], zoom: 0 }]),
  );

  const tooltip = (info: PickingInfo) => {
    if (!info.layer || info.index < 0) return null;
    const id = info.layer.id.split(':')[0];
    const text = outputs[id]?.tooltip?.(info.index);
    return text ? { text, className: 'deck-tooltip' } : null;
  };

  return (
    <div className="canvas" ref={ref} data-testid="canvas">
      {views.map((v) => (
        <ViewRunner key={v.id} spec={v} />
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
          onViewStateChange={({ viewId, viewState: vs }) =>
            setViewStates((s) => ({ ...s, [viewId]: vs as unknown as OrthoState }))
          }
          layers={layers}
          layerFilter={({ layer, viewport }) => layer.id.startsWith(`${viewport.id}:`)}
          getTooltip={tooltip}
          onClick={(info) => info.viewport && select(info.viewport.id)}
          onDeviceInitialized={(device) => {
            const info = (device as unknown as { info?: { renderer?: string; vendor?: string; version?: string } }).info;
            reportEnvironment(info ?? null);
          }}
          onError={(e) => event('deck_error', null, { type: e?.name ?? 'Error' })}
          style={{ background: 'var(--canvas-bg)' }}
        />
      )}
      {views.map((v, i) => {
        const cell = cells[i];
        const out = outputs[v.id];
        const t = viewType(v.type);
        if (!cell) return null;
        return (
          <div
            key={v.id}
            className={`view-cell${v.id === selected ? ' selected' : ''}`}
            style={{ left: cell.x, top: cell.y, width: cell.width, height: cell.height }}
            data-testid="view-cell"
            data-status={out?.status ?? 'loading'}
          >
            <div className="view-header">
              <button className="view-title" onClick={() => select(v.id)} title="Select this view">
                {v.title}
              </button>
              {out?.status === 'loading' && <span className="spinner" aria-label="Loading" />}
              <button className="icon-button" onClick={() => remove(v.id)} aria-label="Close view" title="Close view">
                ×
              </button>
            </div>
            {out?.status === 'error' && <div className="view-error">{out.error}</div>}
            {t?.Legend && out?.status === 'ready' && (
              <div className="view-legend">
                <t.Legend spec={v} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
