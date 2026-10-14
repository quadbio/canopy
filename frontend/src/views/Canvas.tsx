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
//
// A linked group (S7b) takes one tile: its views form a grid inside it under one title row,
// with one legend (their legends merged). They are fitted together at one scale and pan and
// zoom together, as the panels of one view.

import { type Layer, OrthographicView, type PickingInfo } from '@deck.gl/core';
import { PathLayer } from '@deck.gl/layers';
import DeckGL from '@deck.gl/react';
import { memo, type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { useWorkspace, type ViewSpec } from '../state/workspace';
import { deckErrorFields, event, reportEnvironment } from '../telemetry/telemetry';
import { Conversion } from '../ui/Conversion';
import { ErrorBoundary } from '../ui/ErrorBoundary';
import { showSettings } from '../ui/panels';
import type { ColourLegendData } from './colouring';
import { type Cell, type Insets, type OrthoState, resizeView, tileCells } from './layout';
import { mergeLegends } from './linked';
import { allowances, BudgetTuner, snappedExtent, useLod, visibleShare } from './lod';
import { useViewOutputs } from './outputs';
import { atZoom, type FitLayout, fitLayout, linkedStates, panelRects } from './panels';
import { type Bounds, type Link, ownerOf, panelKey, panelsOf, type ViewOutput, viewType } from './registry';
import { type Polygon, useSelection } from './selection';

const TITLE_ROW = 30; // px kept free for the view's title when fitting
const PANEL_TITLE = 18; // px kept free at the top of each panel for its title
const LEGEND_SPACE = 250; // px kept free at the right for an open legend
const COMPACT_WIDTH = 480; // cells narrower than this start with the legend folded
const ZOOM_LIMITS = { minZoom: -20, maxZoom: 40 };

/** Where a view's panels go and what is kept free when fitting them. */
interface Layout extends FitLayout {
  cell: Cell; // keys: panel keys (deck.gl view ids); fitInsets: per panel
  unit: string; // what is fitted, moved and linked together: the view, or its linked group
  scaleKey: string; // views of a group with the same key are fitted at one scale
  free: boolean; // a view of an unlocked group: zoomed and panned on its own
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
  // a view of a linked group has no legend of its own (the group's is beside them)
  const legendOpen = !spec.group && !!out?.legend && !((spec.params.legendCollapsed as boolean | undefined) ?? compact);
  const right = legendOpen ? LEGEND_SPACE : 0;
  const many = panels.length > 1;
  return {
    cell,
    unit: spec.group ?? spec.id,
    scaleKey: out?.scaleKey ?? spec.id,
    free: !!spec.group && spec.params.unlocked === true,
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

/** The layouts of a linked group's views as one: their panels linked (pan and zoom), and fitted
 * together at one scale. */
function combined(ls: Layout[]): Layout {
  if (ls.length === 1) return ls[0];
  const keys = ls.map((l) => l.fitKey ?? '');
  return {
    ...ls[0],
    keys: ls.flatMap((l) => l.keys),
    rects: ls.flatMap((l) => l.rects),
    bounds: ls.flatMap((l) => l.bounds),
    titles: ls.flatMap((l) => l.titles),
    link: 'pan+zoom',
    sameScale: true,
    sameCoordinates: false,
    fitKey: keys.some(Boolean) ? keys.join('|') : undefined,
  };
}

/** Fitted view states of a linked group's views: at one scale among the views showing the same
 * coordinates (`scaleKey`: the same embedding, or spatial coordinates), each on its own
 * otherwise (owner, S7b review option 2). Only the classes of `only` (all if absent). */
function fitGroup(members: Layout[], only?: Set<Layout>): Record<string, OrthoState> {
  const classes = new Map<string, Layout[]>();
  for (const m of members) classes.set(m.scaleKey, [...(classes.get(m.scaleKey) ?? []), m]);
  const out: Record<string, OrthoState> = {};
  for (const ls of classes.values()) {
    if (only && !ls.some((m) => only.has(m))) continue;
    Object.assign(out, fitLayout(combined(ls)));
  }
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
      out.needsConversion,
      out.notice,
      out.problem?.message,
      out.legend,
      out.caption,
      out.total,
      out.content,
      out.margins,
      out.fitKey,
      out.lasso,
      out.scaleKey,
      out.naCells,
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
  lassoOn,
  onLasso,
  panels,
  overlays,
}: {
  spec: ViewSpec;
  cell: Cell;
  output: ViewOutput | undefined;
  selected: boolean;
  onFit: (unit: string) => void;
  /** Lasso mode of this view on or off (views that can select cells). */
  lassoOn: boolean;
  onLasso: (id: string | null) => void;
  panels: { key: string; title?: string; rect: Cell }[];
  /** Panels drawing an overlay (axes), with their current view states. */
  overlays?: { key: string; rect: Cell; state: OrthoState; taken: Insets; draw: (s: OrthoState, r: Cell, t: Insets) => ReactNode }[];
}) {
  const select = useWorkspace((s) => s.select);
  const remove = useWorkspace((s) => s.removeView);
  const chosen = useSelection((s) => s.selections[spec.id]?.count ?? null);
  const clearSelection = useSelection((s) => s.set);
  const t = viewType(spec.type);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    if (output?.notice) setNotice(output.notice);
  }, [output?.notice]);
  const compact = cell.width < COMPACT_WIDTH;
  return (
    <div
      className={`view-cell${selected ? ' selected' : ''}${spec.group ? ' member' : ''}`}
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
        {chosen !== null && (
          <span className="selection-chip" data-testid="selection-chip">
            {chosen.toLocaleString()} selected
            <button className="link" onClick={() => clearSelection(spec.id, null)} aria-label="Clear the selection" title="Clear the selection">
              ×
            </button>
          </span>
        )}
        <span className="view-header-actions">
          {output?.lasso && output.status === 'ready' && (
            <button
              className={`icon-button${lassoOn ? ' on' : ''}`}
              onClick={() => onLasso(lassoOn ? null : spec.id)}
              aria-pressed={lassoOn}
              aria-label="Lasso: select cells"
              title="Lasso: draw around cells to select them (Shift: add to the selection; Esc: stop)"
              data-testid="lasso"
            >
              <LassoIcon />
            </button>
          )}
          {!spec.group && <SettingsButton id={spec.id} label={spec.type === 'plot' ? 'Plot settings' : 'View settings'} />}
          {!spec.group && (
            <button className="icon-button" onClick={() => onFit(spec.id)} aria-label="Fit to data" title="Fit to data">
              ⤢
            </button>
          )}
          <button
            className="icon-button"
            onClick={() => remove(spec.id)}
            aria-label={spec.group ? 'Remove from the linked views' : 'Close view'}
            title={spec.group ? 'Remove this dataset from the linked views' : 'Close view'}
          >
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
      {t?.Legend && output?.status === 'ready' && !spec.group && (
        <div className={`view-legend${compact ? ' compact' : ''}`}>
          <ErrorBoundary scope="view" fallback={() => null}>
            <t.Legend spec={spec} output={output} compact={compact} />
          </ErrorBoundary>
        </div>
      )}
    </div>
  );
});

/** A lasso: a loop of rope with its tail (owner: the usual lasso icon). */
function GearIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" aria-hidden="true">
      <path d="M6.9 1.5h2.2l.4 1.8 1.2.7 1.8-.6 1.1 1.9-1.4 1.3v1.4l1.4 1.3-1.1 1.9-1.8-.6-1.2.7-.4 1.8H6.9l-.4-1.8-1.2-.7-1.8.6-1.1-1.9 1.4-1.3V7.3L2.4 6l1.1-1.9 1.8.6 1.2-.7z" />
      <circle cx="8" cy="8" r="2" />
    </svg>
  );
}

/** The gear of a view's (or a group's) title row: its settings, in the panel at the right. */
function SettingsButton({ id, label }: { id: string; label: string }) {
  const select = useWorkspace((s) => s.select);
  return (
    <button
      className="icon-button"
      onClick={() => {
        select(id);
        showSettings();
      }}
      aria-label={label}
      title={label}
      data-testid="view-settings"
    >
      <GearIcon />
    </button>
  );
}

function LockIcon({ open }: { open: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
      <rect x="3" y="7.5" width="10" height="7" rx="1.5" />
      <path d={open ? 'M5.5 7.5V5a2.5 2.5 0 0 1 4.9-.7' : 'M5.5 7.5V5a2.5 2.5 0 0 1 5 0v2.5'} />
    </svg>
  );
}

function LassoIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true">
      <ellipse cx="8.5" cy="6" rx="6" ry="4" strokeDasharray="2.2 1.6" />
      <path d="M4.5 9.2c-.9.9-1 2.1-.2 2.8.9.7 2.2.3 2.4-.8.2-1-.9-1.6-1.6-.9-.8.8-.9 2.4-.4 3.7" />
    </svg>
  );
}

/** Drawing a lasso over a view's cell: the pointer's path (canvas pixels, from the canvas's own
 * corner: `origin`) until it is released. `onDone` says whether the lasso was used (false: it
 * started outside every panel, and lasso mode stays on with a hint). */
function LassoOverlay({
  cell,
  origin,
  onDone,
  onCancel,
}: {
  cell: Cell;
  origin: () => DOMRect | undefined;
  onDone: (points: Polygon, add: boolean) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [points, setPoints] = useState<Polygon>([]);
  const path = useRef<Polygon>([]); // the stroke as it is drawn (state may lag behind, B-L1)
  // a hint under the lasso: it started outside every panel, or the cells could not be read
  const [hint, setHint] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // the cells inside are being read: no second lasso
  const drawing = useRef(false);
  const stop = () => {
    drawing.current = false;
    path.current = [];
    setPoints([]);
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      // Esc in a field or list (e.g. a Select) is theirs (B-L2)
      if (e.key === 'Escape' && !(t && t.closest('input, select, textarea, [role="listbox"], [role="combobox"]'))) onCancel();
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [onCancel]);
  // canvas pixels: the overlay starts below the view's title row, so positions are taken from
  // the canvas's corner, not the overlay's (B-H1: they were 30 px too high)
  const at = (e: React.PointerEvent) => {
    const box = origin();
    return [e.clientX - (box?.left ?? 0), e.clientY - (box?.top ?? 0)] as [number, number];
  };
  const top = cell.y + TITLE_ROW;
  return (
    <div
      className="lasso-overlay"
      data-testid="lasso-overlay"
      role="application"
      aria-label="Lasso: drag around cells to select them (Shift: add; Esc: stop)"
      style={{ left: cell.x, top, width: cell.width, height: cell.height - TITLE_ROW }}
      onPointerDown={(e) => {
        if (busy) return; // review H3 B-M3: two lassos over one slow read would overwrite each other
        (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
        drawing.current = true;
        setHint(null);
        path.current = [at(e)];
        setPoints(path.current);
      }}
      onPointerMove={(e) => {
        if (!drawing.current) return;
        const p = at(e);
        const last = path.current[path.current.length - 1];
        if (last && Math.hypot(p[0] - last[0], p[1] - last[1]) < 3) return;
        path.current = [...path.current, p];
        setPoints(path.current);
      }}
      onPointerUp={(e) => {
        if (!drawing.current) return;
        const done = path.current;
        stop();
        if (done.length < 3) return;
        setBusy(true);
        onDone(done, e.shiftKey)
          .then((used) => setHint(used ? null : 'Start the lasso inside a panel.'))
          // a read that failed (e.g. the server restarting): said here, lasso mode stays on
          // to try again (review H3 B-M3)
          .catch((err: unknown) => setHint(`The cells could not be read (${err instanceof Error ? err.message : String(err)}); draw again, or Esc.`))
          .finally(() => setBusy(false));
      }}
      // a gesture the browser took over (touch, pen) or a lost capture ends the stroke (B-L8)
      onPointerCancel={stop}
      onLostPointerCapture={() => drawing.current && stop()}
      aria-busy={busy}
    >
      {(hint || busy) && (
        <p className="lasso-hint" data-testid="lasso-hint" role="status">
          {busy ? 'Selecting…' : hint}
        </p>
      )}
      <svg width="100%" height="100%">
        <polyline points={points.map(([x, y]) => `${x - cell.x},${y - top}`).join(' ')} className="lasso-path" data-testid="lasso-path" />
      </svg>
    </div>
  );
}

/** A linked group's tile: its title row (fit, close) and its legend; its views are drawn in
 * the cells below. */
const GroupTile = memo(function GroupTile({
  group,
  first,
  n,
  tile,
  legend,
  selected,
  onFit,
}: {
  group: string;
  first: ViewSpec;
  n: number;
  tile: Cell;
  legend: ColourLegendData | undefined;
  selected: boolean;
  onFit: (unit: string) => void;
}) {
  const select = useWorkspace((s) => s.select);
  const removeGroup = useWorkspace((s) => s.removeGroup);
  const updateParams = useWorkspace((s) => s.updateParams);
  const unlocked = first.params.unlocked === true;
  // locking again links the views showing the same coordinates, from one fitted scale (owner;
  // the canvas refits a group whenever it is locked again, also by Reset: review H3 B-L1)
  const toggleLock = () => updateParams(first.id, { unlocked: !unlocked });
  const t = viewType(first.type);
  const compact = tile.width < COMPACT_WIDTH;
  const output = useMemo<ViewOutput>(() => ({ layers: [], bounds: null, status: 'ready', legend }), [legend]);
  return (
    <div
      className={`group-tile${selected ? ' selected' : ''}`}
      style={{ left: tile.x, top: tile.y, width: tile.width, height: tile.height }}
      data-testid="group-tile"
      data-group={group}
    >
      <div className="view-header">
        <button className="view-title" onClick={() => select(first.id)} title="Linked views: settings are shared">
          Linked · {n} datasets
        </button>
        {legend?.kind === 'categories' && legend.regenerated && (
          <span className="view-caption" data-testid="colours-regenerated" title="The datasets’ own colours disagree (the same colour for different categories, or different colours for one category): palette colours are used for all.">
            colours regenerated
          </span>
        )}
        <span className="view-header-actions">
          <button
            className={`icon-button${unlocked ? ' on' : ''}`}
            onClick={toggleLock}
            // one fixed label, pressed (and highlighted) while unlocked (review H3 B-L5)
            aria-pressed={unlocked}
            aria-label="Move each view on its own"
            title={
              unlocked
                ? 'Each view zooms and pans on its own. Click to link the views showing the same coordinates again (they return to one fitted scale).'
                : 'Views showing the same coordinates zoom and pan together. Click to move each on its own.'
            }
            data-testid="group-lock"
          >
            <LockIcon open={unlocked} />
          </button>
          <SettingsButton id={first.id} label="Settings of the linked views" />
          <button className="icon-button" onClick={() => onFit(group)} aria-label="Fit to data" title="Fit to data">
            ⤢
          </button>
          <button className="icon-button" onClick={() => removeGroup(group)} aria-label="Close linked views" title="Close the linked views">
            ×
          </button>
        </span>
      </div>
      {t?.Legend && legend && (
        <div className={`view-legend${compact ? ' compact' : ''}`}>
          <ErrorBoundary scope="view" fallback={() => null}>
            <t.Legend spec={first} output={output} compact={compact} />
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

/** What the canvas showed while frames were counted (for the `frames` event). */
interface FrameContext {
  views: number;
  cells: number; // cells of the open views, in all
  budget: number;
  mpx: number; // drawing buffer, million device pixels
}

/** Frame times while the canvas redraws (interaction, loading): one `frames` event per 10 s
 * with frames, median and 95th percentile intervals and the long ones (> 50 ms), and the same
 * for the frames drawn while the canvas moved (pan, zoom), with what it showed. Intervals over
 * 250 ms are pauses, not frames. Returns each interval, for the budget tuner. */
class FrameStats {
  private last = 0;
  private gaps: number[] = [];
  private moving: number[] = [];
  private since = performance.now();

  frame(moving: boolean, context: () => FrameContext): number {
    const now = performance.now();
    const gap = now - this.last;
    this.last = now;
    if (gap > 0 && gap <= 250) {
      this.gaps.push(gap);
      if (moving) this.moving.push(gap);
    }
    if (now - this.since >= 10_000) this.flush(now, context);
    return gap;
  }

  private flush(now: number, context: () => FrameContext) {
    this.since = now;
    const g = this.gaps.sort((a, b) => a - b);
    const m = this.moving.sort((a, b) => a - b);
    this.gaps = [];
    this.moving = [];
    if (g.length < 10 && !g.some((x) => x > 50)) return; // a stall is reported however few frames
    const q = (a: number[], f: number) => (a.length ? Math.round(a[Math.min(a.length - 1, Math.floor(f * a.length))] * 10) / 10 : 0);
    event('frames', null, {
      n: g.length,
      median_ms: q(g, 0.5),
      p95_ms: q(g, 0.95),
      over_50ms: g.filter((x) => x > 50).length,
      n_moving: m.length,
      moving_median_ms: q(m, 0.5),
      moving_p95_ms: q(m, 0.95),
      ...context(),
    });
  }
}

// Hover picking draws every point once more, off screen (~10 ms at 1 M points on an M2): while
// the pointer moves, at most one pick per HOVER_PICK_MS, and one where it stops.
const HOVER_PICK_MS = 100;

/** The top-level layer a (sub-)layer belongs to. */
function rootOf(layer: Layer): Layer {
  let l = layer;
  while (l.parent) l = l.parent as Layer;
  return l;
}

const NO_PANELS: { key: string; title?: string; rect: Cell }[] = [];
// a selection's outline layers, made once (B-L4)
const outlineCache = new WeakMap<object, Layer[]>();
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
  // linked groups: their legends merged, and room for them when open
  const groupLegends = useMemo(() => {
    // each view's last legend, also while it reloads (counts stay, B-L8); the datasets without
    // the column as one NA line (B-M5)
    const by: Record<string, (ColourLegendData | undefined)[]> = {};
    const na: Record<string, { cells: number; datasets: number }> = {};
    for (const v of views) {
      if (!v.group) continue;
      const out = outputs[v.id];
      (by[v.group] ??= []).push(out?.status === 'error' ? undefined : (out?.legend as ColourLegendData | undefined));
      if (out?.naCells !== undefined) {
        const m = (na[v.group] ??= { cells: 0, datasets: 0 });
        m.cells += out.naCells;
        m.datasets += 1;
      }
    }
    return Object.fromEntries(Object.entries(by).map(([g, ls]) => [g, mergeLegends(ls, na[g])]));
  }, [views, outputs]);
  const firstOfGroup = useMemo(() => {
    const m: Record<string, ViewSpec> = {};
    for (const v of views) if (v.group && !m[v.group]) m[v.group] = v;
    return m;
  }, [views]);
  // a plot belonging to a view shares that view's tile (the bottom third); a linked group's
  // views share one tile
  const followKey = views.map((v) => `${v.id}>${typeof v.params.follows === 'string' ? v.params.follows : ''}>${v.group ?? ''}`).join(',');
  const legendOpenKey = Object.keys(groupLegends)
    .map((g) => `${g}:${groupLegends[g] && !(firstOfGroup[g]?.params.legendCollapsed as boolean | undefined) ? 1 : 0}`)
    .join(',');
  const tiles = useMemo(() => {
    const right = Object.fromEntries(legendOpenKey ? legendOpenKey.split(',').map((x) => [x.split(':')[0], x.endsWith(':1') ? LEGEND_SPACE : 0]) : []);
    const first = tileCells(
      views.map((v) => ({ id: v.id, follows: typeof v.params.follows === 'string' ? v.params.follows : null, group: v.group })),
      size.width,
      size.height,
      2,
      TITLE_ROW,
      right,
    );
    // a narrow group tile starts with its legend folded (as a view's)
    for (const g of Object.keys(first.groups)) {
      if (right[g] && first.groups[g].width < COMPACT_WIDTH && firstOfGroup[g]?.params.legendCollapsed === undefined) right[g] = 0;
    }
    return tileCells(
      views.map((v) => ({ id: v.id, follows: typeof v.params.follows === 'string' ? v.params.follows : null, group: v.group })),
      size.width,
      size.height,
      2,
      TITLE_ROW,
      right,
    );
  }, [followKey, size, legendOpenKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const cells = tiles.cells;
  const [viewStates, setViewStates] = useState<Record<string, OrthoState>>({});
  const [lassoing, setLassoing] = useState<string | null>(null);
  const selections = useSelection((s) => s.selections);
  const setSelection = useSelection((s) => s.set);
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
  const tuner = useRef(new BudgetTuner());
  const inputAt = useRef(0); // when the first input of the frame being drawn came
  // the display's frame interval (judges slow and fast): measured once the page has settled
  // (not while it loads), and again on another screen (a change of pixel ratio or size)
  useEffect(() => {
    let id = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const measure = () => {
      cancelAnimationFrame(id);
      const ts: number[] = [];
      const step = (t: number) => {
        ts.push(t);
        if (ts.length < 21) id = requestAnimationFrame(step);
        else {
          const gaps = ts.slice(1).map((x, i) => x - ts[i]).sort((a, b) => a - b);
          tuner.current.setPeriod(gaps[10]);
        }
      };
      id = requestAnimationFrame(step);
    };
    const later = () => {
      clearTimeout(timer);
      timer = setTimeout(measure, 2000);
    };
    later();
    window.addEventListener('resize', later);
    return () => {
      cancelAnimationFrame(id);
      clearTimeout(timer);
      window.removeEventListener('resize', later);
    };
  }, []);
  const moving = useRef(0); // until when the canvas counts as moving (pan, zoom)
  const draggingNow = useRef(false);
  const deckRef = useRef<{ deck?: { pickObject: (o: { x: number; y: number; radius: number }) => PickingInfo | null } } | null>(null);
  const hoverGate = useRef<{ open: boolean; last: number; timer: ReturnType<typeof setTimeout> | null }>({ open: true, last: 0, timer: null });
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
    for (const v of views) if (v.group) open.add(v.group);
    for (const id of touched.current) if (!open.has(id)) touched.current.delete(id);
    for (const id of Object.keys(outputs)) if (!open.has(id)) drop(id);
    for (const id of Object.keys(useSelection.getState().selections)) if (!open.has(id)) useSelection.getState().set(id, null);
  }, [views, outputs, drop, layouts]);

  // What is fitted and moved together: a view, or a linked group (its views' layouts as one).
  const units = useMemo(() => {
    const by = new Map<string, Layout[]>();
    for (const l of layouts) if (l) by.set(l.unit, [...(by.get(l.unit) ?? []), l]);
    return [...by.entries()].map(([unit, ls]) => ({ unit, members: ls, l: combined(ls) }));
  }, [layouts]);

  // Fit when a view's data first arrives or changes; follow size changes otherwise. A linked
  // group is looked at view by view: only views with new data (or a new cell, not yet moved) are
  // refitted, with the others of their scale; after the user has moved the group, a refitted
  // view joins it where the others are (the same steps from its fit), so nobody's zoom is lost.
  useEffect(() => {
    const updates: Record<string, OrthoState> = {};
    units.forEach(({ unit, members, l: whole }) => {
      // moved by the user: only once something of it was fitted (a wheel turned while the views
      // still load is no position to keep; review H3 B-M1)
      const user = touched.current.has(unit) && members.some((m) => m.keys.some((k) => fits.current[k]));
      const needs = new Set<Layout>();
      const resized: { key: string; rect: Cell }[] = [];
      const insetsOf = (l: Layout) => `${JSON.stringify(l.fitInsets)}|${l.sameScale}|${l.sameCoordinates}`;
      for (const l of members) {
        if (l.cell.width < 10) continue;
        const insets = insetsOf(l);
        l.keys.forEach((key, j) => {
          const b = l.bounds[j];
          const prev = placed.current[key];
          const moved = prev && (prev.rect.width !== l.rects[j].width || prev.rect.height !== l.rects[j].height || prev.insets !== insets);
          // new data: a new fit key, or (without a key) new bounds; more of the same data
          // (new bounds, same key) refits only a view the user has not moved
          const keyed = l.fitKey !== undefined && fittedKey.current[key] !== undefined;
          const newData = keyed ? fittedKey.current[key] !== l.fitKey || (fitted.current[key] !== b && !user) : fitted.current[key] !== b;
          if (b && (newData || (moved && !user))) needs.add(l);
          else if (moved && viewStates[key]) resized.push({ key, rect: l.rects[j] });
        });
      }
      if (needs.size) {
        const needKeys = new Set([...needs].flatMap((l) => l.keys));
        if (!user || members.length === 1) {
          // not moved by the user (or a single view): fit, with the others of its scale
          const fresh = members.length === 1 ? fitLayout(whole) : fitGroup(members, needs);
          Object.assign(fits.current, fresh);
          for (const [k, st] of Object.entries(fresh)) if (!user || needKeys.has(k)) updates[k] = st;
        } else {
          // the group was moved: a refitted view takes the scale of the others showing the same
          // coordinates (their fits stay, so linking stays smooth) and joins them where they are;
          // with none (every view of its coordinates is new), the views of those coordinates are
          // fitted together, at one scale (review H3 B-M1); a view of an unlocked group alone
          const alone: Layout[] = [];
          for (const l of needs) {
            const peer = l.free
              ? undefined
              : members.find((m) => m.scaleKey === l.scaleKey && !needs.has(m) && fits.current[m.keys[0]] && viewStates[m.keys[0]]);
            if (!peer) {
              alone.push(l);
              continue;
            }
            const zoom = fits.current[peer.keys[0]].zoom;
            // the target is worked out again at the peers' zoom: insets shift it by zoom
            // (review H3 B-L2)
            const mine = atZoom(l, zoom);
            Object.assign(fits.current, mine);
            const ref = peer.keys[0];
            const joined = linkedStates('pan+zoom', ref, viewStates[ref], mine, fits.current);
            for (const k of Object.keys(mine)) updates[k] = joined[k];
          }
          const together = fitGroup(alone.filter((l) => !l.free));
          for (const l of alone.filter((x) => x.free)) Object.assign(together, fitLayout(l));
          Object.assign(fits.current, together);
          Object.assign(updates, together);
        }
        for (const l of needs) {
          l.keys.forEach((key, j) => {
            if (l.bounds[j]) {
              fitted.current[key] = l.bounds[j]!;
              fittedKey.current[key] = l.fitKey;
            }
          });
        }
      }
      for (const { key, rect } of resized) {
        if (updates[key]) continue;
        updates[key] = resizeView(viewStates[key], placed.current[key].rect, rect);
        if (fits.current[key]) fits.current[key] = resizeView(fits.current[key], placed.current[key].rect, rect);
      }
      for (const l of members) l.keys.forEach((key, j) => (placed.current[key] = { rect: l.rects[j], insets: insetsOf(l) }));
    });
    if (Object.keys(updates).length) setViewStates((s) => ({ ...s, ...updates }));
  }, [units]); // eslint-disable-line react-hooks/exhaustive-deps

  // stable (the memoised cells and group tiles take it): reads the current units from a ref,
  // or every pan frame would redraw every cell and the group's legend (review H3 B-M4)
  const unitsRef = useRef(units);
  useLayoutEffect(() => {
    unitsRef.current = units;
  }, [units]);
  const fit = useCallback((unit: string) => {
    const u = unitsRef.current.find((x) => x.unit === unit);
    if (!u) return;
    touched.current.delete(unit);
    const states = u.members.length === 1 ? fitLayout(u.l) : fitGroup(u.members);
    Object.assign(fits.current, states);
    setViewStates((s) => ({ ...s, ...states }));
  }, []);

  // a group locked again (its button, or Reset to defaults) returns to one fitted scale
  const wasUnlocked = useRef<Record<string, boolean>>({});
  useEffect(() => {
    const now: Record<string, boolean> = {};
    for (const v of views) if (v.group) now[v.group] = v.params.unlocked === true;
    for (const [g, unlocked] of Object.entries(now)) if (wasUnlocked.current[g] && !unlocked) fit(g);
    wasUnlocked.current = now;
  }, [views, fit]);

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
    // the outlines of each view's lassos, while its coordinates are those they were drawn in
    for (const v of views) {
      const sel = selections[v.id];
      if (!sel || outputs[v.id]?.lasso?.key !== sel.key) continue;
      let outlines = outlineCache.get(sel);
      if (!outlines) outlineCache.set(sel, (outlines = sel.polygons.map((pg, k) =>
          new PathLayer({
            id: `${panelKey(v.id, pg.panel)}:lasso-${k}`,
            data: [{ path: [...pg.points, pg.points[0]] }],
            getPath: (d: { path: Polygon }) => d.path,
            getColor: [230, 80, 20, 230],
            getWidth: 1.5,
            widthUnits: 'pixels',
            pickable: false,
          }) as unknown as Layer,
      )));
      all.push(...outlines);
    }
    const fresh = all.map((l) => (shown.current.has(l) || !l.internalState ? l : l.clone({})));
    shown.current = new Set(fresh);
    return dragging ? fresh.map((l) => l.clone({ pickable: false })) : fresh;
  }, [views, outputs, dragging, selections]);
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
  const budget = useLod((s) => s.budget);
  const setBudget = useLod((s) => s.setBudget);
  const totalsKey = views.map((v) => `${v.id}:${outputs[v.id]?.total ?? 0}`).join(',');
  const totalCells = views.reduce((a, v) => a + (outputs[v.id]?.total ?? 0), 0);
  useEffect(() => {
    setAllowance(allowances(Object.fromEntries(views.map((v) => [v.id, outputs[v.id]?.total ?? 0])), budget));
  }, [totalsKey, budget, setAllowance]); // eslint-disable-line react-hooks/exhaustive-deps
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
  const tipAt = useRef({ x: 0, y: 0 }); // where the shown text was picked
  // the trailing pick does not outlive the canvas
  useEffect(
    () => () => {
      const t = hoverGate.current.timer;
      if (t !== null) clearTimeout(t);
    },
    [],
  );
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
    tipAt.current = { ...pointer.current };
    placeTip();
  };

  // The pick deck.gl was kept from making while the pointer moved (HOVER_PICK_MS), made where
  // the pointer is now.
  const pickNow = () => {
    const g = hoverGate.current;
    g.timer = null;
    g.last = performance.now();
    g.open = true;
    const deck = deckRef.current?.deck;
    if (!deck || draggingNow.current) return;
    const { x, y } = pointer.current;
    const info = deck.pickObject({ x, y, radius: 3 });
    showTip(info ? tooltip(info) : null);
  };

  // A lasso drawn over view `id`: its points in the coordinates of the panel it started in, the
  // cells inside selected (Shift: added to the selection). False: it started outside every
  // panel (lasso mode stays on).
  const lassoDone = async (id: string, screen: Polygon, add: boolean): Promise<boolean> => {
    const out = outputs[id];
    const deck = deckRef.current?.deck as unknown as {
      getViewports: () => { id: string; x: number; y: number; width: number; height: number; unproject: (p: number[]) => number[] }[];
    } | undefined;
    if (!out?.lasso || !deck) {
      setLassoing((cur) => (cur === id ? null : cur));
      return true;
    }
    const [sx, sy] = screen[0];
    const vp = deck
      .getViewports()
      .find((p) => p.id.startsWith(`${id}/`) && sx >= p.x && sx <= p.x + p.width && sy >= p.y && sy <= p.y + p.height);
    if (!vp) return false;
    const poly = screen.map(([x, y]) => vp.unproject([x - vp.x, y - vp.y]).slice(0, 2) as [number, number]);
    const panel = ownerOf(vp.id).panel;
    const old = useSelection.getState().selections[id];
    const keep = add && old && old.key === out.lasso.key && old.mask.length === out.lasso.n;
    const mask = keep ? old.mask.slice() : new Uint8Array(out.lasso.n);
    const t0 = performance.now();
    const added = await out.lasso.select(panel, poly, mask); // a failure goes to the overlay
    // only this view's lasso mode ends: another view's may have been turned on meanwhile
    setLassoing((cur) => (cur === id ? null : cur));
    // the view may have closed while the cells were read (spatial: their centres, B-L3)
    if (!useWorkspace.getState().views.some((v) => v.id === id)) return true;
    const count = (keep ? old.count : 0) + added;
    event('lasso', performance.now() - t0, { n: out.lasso.n, selected: count, added: keep ? 1 : 0 });
    setSelection(id, { mask, count, key: out.lasso.key, polygons: [...(keep ? old.polygons : []), { panel, points: poly }] });
    return true;
  };
  // lasso mode ends with its view (closed, or no longer able to select, B-L2)
  useEffect(() => {
    if (lassoing && !outputs[lassoing]?.lasso) setLassoing(null);
  }, [lassoing, outputs]);

  return (
    <div
      className="canvas"
      ref={ref}
      data-testid="canvas"
      onPointerMove={(e) => {
        const box = ref.current!.getBoundingClientRect();
        pointer.current = { x: e.clientX - box.left, y: e.clientY - box.top };
        placeTip();
        // deck.gl picks on its next frame: let that pick through, or keep it out and pick once
        // the pointer has rested (or HOVER_PICK_MS after the last pick)
        const g = hoverGate.current;
        const now = performance.now();
        if (now - g.last >= HOVER_PICK_MS) {
          g.open = true;
          g.last = now;
        } else {
          g.open = false;
          if (g.timer === null) g.timer = setTimeout(pickNow, HOVER_PICK_MS - (now - g.last));
          // until the next pick, the old text would follow the pointer onto other cells (B-L11)
          const { x, y } = pointer.current;
          if (Math.hypot(x - tipAt.current.x, y - tipAt.current.y) > 4) showTip(null);
        }
      }}
      onPointerLeave={() => {
        const g = hoverGate.current;
        if (g.timer !== null) clearTimeout(g.timer);
        g.timer = null;
        g.open = true;
        showTip(null);
      }}
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
            deckRef.current = r as typeof deckRef.current;
            // For tests and debugging in the browser console (`__canopyDeck.deck`).
            (window as unknown as { __canopyDeck?: unknown }).__canopyDeck = r ?? undefined;
          }}
          views={deckViews}
          viewState={viewState}
          onViewStateChange={({ viewId, viewState: vs, interactionState }) => {
            const { target, zoom } = vs as unknown as OrthoState;
            const owner = ownerOf(viewId).view;
            const unit = views.find((v) => v.id === owner)?.group ?? owner;
            if (interactionState && Object.values(interactionState).some(Boolean)) {
              touched.current.add(unit);
              moving.current = performance.now() + 300; // the frames of this move (and its end)
              if (!inputAt.current) inputAt.current = performance.now();
            }
            // in a linked group, only the views showing the same coordinates move together: a
            // UMAP and spatial coordinates have nothing in common (and y points the other way);
            // in an unlocked group, each view on its own
            const u = units.find((x) => x.unit === unit);
            const mineL = u?.members.find((m) => m.keys.includes(viewId));
            const l =
              u && mineL && u.members.length > 1
                ? mineL.free
                  ? mineL
                  : combined(u.members.filter((m) => m.scaleKey === mineL.scaleKey))
                : u?.l;
            setViewStates((s) => {
              if (!l || l.keys.length < 2) return { ...s, [viewId]: { target, zoom } };
              const mine = Object.fromEntries(l.keys.filter((k) => s[k]).map((k) => [k, s[k]]));
              return { ...s, ...linkedStates(l.link, viewId, { target, zoom }, mine, fits.current) };
            });
          }}
          onInteractionStateChange={(s) => {
            draggingNow.current = !!s.isDragging;
            setDragging(!!s.isDragging);
            if (s.isDragging) showTip(null);
          }}
          layers={layers}
          // by the top-level layer's id: sub-layers (e.g. Viv's image tiles) have their own ids
          // (hover picks are kept out while the pointer moves fast: HOVER_PICK_MS)
          layerFilter={({ layer, viewport, renderPass }) =>
            (renderPass !== 'picking:hover' || hoverGate.current.open) && rootOf(layer).id.startsWith(`${viewport.id}:`)
          }
          onHover={(info) => {
            if (hoverGate.current.open) showTip(tooltip(info));
          }}
          // a few pixels of tolerance: cells are small when zoomed out
          pickingRadius={3}
          onClick={(info) => info.viewport && select(ownerOf(info.viewport.id).view)}
          onAfterRender={() => {
            const isMoving = performance.now() < moving.current;
            frames.current.frame(isMoving, () => ({
              views: views.length,
              cells: totalCells,
              budget,
              mpx: Math.round((size.width * size.height * window.devicePixelRatio ** 2) / 1e5) / 10,
            }));
            // the time from a move's first input to its frame on screen: short when drawing keeps
            // up, however slowly the pointer moves; long when inputs queue behind slow frames
            // (the gap between frames would measure the pointer's pace; review H3 B-M2)
            const latency = inputAt.current ? performance.now() - inputAt.current : 0;
            inputAt.current = 0;
            // only while it is dragged with the whole budget on screen: then points are the cost
            if (isMoving && draggingNow.current && totalCells > budget && latency > 0) {
              const next = tuner.current.frame(latency, budget);
              if (next !== null) {
                event('lod_budget', null, { from: budget, to: next, cells: totalCells, views: views.length });
                setBudget(next);
              }
            }
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
      {Object.entries(tiles.groups).map(([g, tile]) => (
        <GroupTile
          key={g}
          group={g}
          first={firstOfGroup[g]}
          n={views.filter((v) => v.group === g).length}
          tile={tile}
          legend={groupLegends[g]}
          selected={views.some((v) => v.group === g && v.id === selected)}
          onFit={fit}
        />
      ))}
      {views.map((v, i) =>
        cells[i] ? (
          <ErrorBoundary key={v.id} scope="view" fallback={() => null}>
            <ViewCell
              spec={v}
              cell={cells[i]}
              output={outputs[v.id]}
              selected={v.id === selected}
              onFit={fit}
              lassoOn={lassoing === v.id}
              onLasso={setLassoing}
              panels={panelsFor(layouts[i])}
              overlays={overlaysFor(outputs[v.id], layouts[i], viewStates)}
            />
          </ErrorBoundary>
        ) : null,
      )}
      {lassoing && cells[views.findIndex((v) => v.id === lassoing)] && (
        <LassoOverlay
          cell={cells[views.findIndex((v) => v.id === lassoing)]}
          origin={() => ref.current?.getBoundingClientRect()}
          onDone={(pts, add) => lassoDone(lassoing, pts, add)}
          onCancel={() => setLassoing(null)}
        />
      )}
      <div ref={tipRef} className="hover-tip" data-testid="hover-tip" role="tooltip" style={{ display: 'none' }} />
    </div>
  );
}
