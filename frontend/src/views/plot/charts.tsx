// Charts of the Plot view, drawn in the page (SVG): violins, stacked bars, a density map, and
// the axes laid over a zoomable scatter. They draw server summaries (a few KB), never cells.

import { useEffect, useRef, useState } from 'react';

import type { PlotSummary } from '../../api/types';
import { colormapLut, type ColormapName } from '../../colour/palettes';
import type { Cell, Insets, OrthoState } from '../layout';
import { log1pTicks, niceTicks, tickLabel } from './axes';

type RGB = [number, number, number];
const css = (c: RGB | undefined) => (c ? `rgb(${c.join(',')})` : 'var(--text-muted)');
const percent = (x: number) => `${Math.round(x * 100)}%`;

/** The element's size, followed while it changes. */
export function useBox<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setSize({ width: Math.floor(e.contentRect.width), height: Math.floor(e.contentRect.height) }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}

// Room for the axes around a chart's plotting area (the bottom grows with turned labels).
export const MARGIN = { left: 56, right: 16, top: 16, bottom: 72 };
const ABOVE_ROW = 30; // the row of "above 0" dots and numbers above violins
const LABEL_CHARS = 22;

/** A long label shortened in the middle, so labels that differ at the end stay apart. */
export function shortLabel(c: string, max = LABEL_CHARS): string {
  if (c.length <= max) return c;
  const head = Math.ceil((max - 1) * 0.55);
  return `${c.slice(0, head)}…${c.slice(c.length - (max - 1 - head))}`;
}

/** Room below the plotting area for category labels (turned when slots are narrow). */
export function categoryAxisHeight(labels: string[], slot: number): number {
  if (slot >= 64) return 48;
  const chars = Math.max(0, ...labels.map((c) => shortLabel(c).length));
  return Math.min(Math.max(48, Math.round(chars * 6.5 * Math.sin((40 * Math.PI) / 180)) + 36), 140);
}

/** Indices of labels in their column's own order, as the legend lists them (a category
 * column's categories; text columns come sorted). */
const columnOrder = (labels: string[]) => labels.map((_, i) => i);

/** Ticks of a value axis in its own units: plain, or for log1p-transformed values. */
function valueTicks(lo: number, hi: number, log1p: boolean): { at: number; label: string }[] {
  if (log1p) return log1pTicks(hi).filter((t) => t.at >= lo - 1e-9);
  return niceTicks(lo, hi, 5).map((v) => ({ at: v, label: tickLabel(v) }));
}

function YAxis({
  lo,
  hi,
  log1p,
  height,
  label,
  asPercent = false,
}: {
  lo: number;
  hi: number;
  log1p: boolean;
  height: number;
  label: string;
  asPercent?: boolean;
}) {
  const y = (v: number) => MARGIN.top + (1 - (v - lo) / (hi - lo || 1)) * height;
  const ticks = valueTicks(lo, hi, log1p).map((t) => (asPercent ? { ...t, label: percent(t.at) } : t));
  return (
    <g className="axis">
      <line x1={MARGIN.left} x2={MARGIN.left} y1={MARGIN.top} y2={MARGIN.top + height} />
      {ticks.map((t) => (
        <g key={t.at} transform={`translate(${MARGIN.left},${y(t.at)})`}>
          <line x1={-4} x2={0} />
          <text x={-7} dy="0.32em" textAnchor="end">
            {t.label}
          </text>
        </g>
      ))}
      <text className="label" transform={`translate(14,${MARGIN.top + height / 2}) rotate(-90)`} textAnchor="middle">
        {label}
        {log1p ? ' (log scale)' : ''}
      </text>
    </g>
  );
}

/** Category labels along the bottom, turned when they would overlap; full names on hover. */
function CategoryAxis({ labels, slot, top, height, label }: { labels: string[]; slot: number; top: number; height: number; label: string }) {
  const turn = slot < 64;
  return (
    <g className="axis">
      <line x1={MARGIN.left} x2={MARGIN.left + slot * labels.length} y1={top} y2={top} />
      {labels.map((c, i) => (
        <text
          key={c}
          transform={`translate(${MARGIN.left + (i + 0.5) * slot},${top + 12})${turn ? ' rotate(-40)' : ''}`}
          textAnchor={turn ? 'end' : 'middle'}
          dy={turn ? '0.3em' : '0.6em'}
        >
          {shortLabel(c)}
          {c.length > LABEL_CHARS && <title>{c}</title>}
        </text>
      ))}
      <text className="label" x={MARGIN.left + (slot * labels.length) / 2} y={top + height - 6} textAnchor="middle">
        {label}
      </text>
    </g>
  );
}

// ---------------------------------------------------------------- violins

export function Violins({
  summary,
  colours,
  hidden,
  xLabel,
  yLabel,
  showAboveZero,
  zeroSpike,
}: {
  summary: Extract<PlotSummary, { kind: 'violin' }>;
  colours: Map<string, RGB>;
  hidden: Set<string>;
  xLabel: string;
  yLabel: string;
  showAboveZero: boolean;
  /** a gene: most cells are 0, drawn apart (see below) */
  zeroSpike: boolean;
}) {
  const [ref, box] = useBox<HTMLDivElement>();
  const shown = columnOrder(summary.categories)
    .map((i) => ({ c: summary.categories[i], g: summary.groups[i] }))
    .filter((x) => !hidden.has(x.c) && x.g.n > 0);
  const plotW = Math.max(box.width - MARGIN.left - MARGIN.right, 50);
  const slot = Math.max(28, plotW / Math.max(shown.length, 1));
  const width = MARGIN.left + slot * shown.length + MARGIN.right;
  const bottom = categoryAxisHeight(
    shown.map((x) => x.c),
    slot,
  );
  // the share of cells above 0 gets a row of its own above the violins: a dot whose area is the
  // share, and the number (owner, full review 2: it was easy to miss)
  const above = showAboveZero ? ABOVE_ROW : 0;
  const height = Math.max(box.height - MARGIN.top - bottom - above, 60);
  const [lo, hi] = summary.range;
  const y = (v: number) => MARGIN.top + (1 - (v - lo) / (hi - lo || 1)) * height;
  const step = (hi - lo) / summary.bins;
  const full = slot * 0.42;
  // a gene's zeros fall in the bin holding 0: drawn as a flat bar whose width is the share of
  // cells at exactly 0, so the outline above is scaled to the expressing cells and stays
  // readable. The bin's positive values (0 < v < its upper edge) stay in the outline (review
  // full-2 B-H2: they were dropped, two thirds of the expressing cells of a counts gene).
  const zeroBin = zeroSpike && lo <= 0 && hi > 0 ? Math.min(Math.floor((-lo / (hi - lo)) * summary.bins), summary.bins - 1) : -1;
  // statistics on a log scale are of log1p values: shown as values again (quantiles commute
  // with the transform; review full-2 B-M2)
  const shown_ = (v: number) => tickLabel(summary.log1p ? Math.expm1(v) : v);
  return (
    <div ref={ref} className="chart" data-testid="violins">
      {box.width > 0 && (
        <svg width={Math.max(width, box.width)} height={box.height} role="img" aria-label={`Violins of ${yLabel} by ${xLabel}`}>
          <g transform={`translate(0,${above})`}>
          <YAxis lo={lo} hi={hi} log1p={summary.log1p} height={height} label={yLabel} />
          {shown.map(({ c, g }, i) => {
            const cx = MARGIN.left + (i + 0.5) * slot;
            const hist = outlineCounts(g.hist, zeroBin, g.n_zero);
            const peak = Math.max(...hist, 1);
            // the outline covers only the bins this group has (no hairline through others)
            const first = hist.findIndex((v) => v > 0);
            const last = hist.length - 1 - [...hist].reverse().findIndex((v) => v > 0);
            const half = (k: number) => (hist[k] / peak) * full;
            const run = first < 0 ? [] : hist.slice(first, last + 1).map((_, j) => first + j);
            const right = run.map((k) => `${cx + half(k)},${y(lo + (k + 0.5) * step)}`);
            const left = run.map((k) => `${cx - half(k)},${y(lo + (k + 0.5) * step)}`).reverse();
            const ends =
              run.length > 0 ? { from: `${cx},${y(lo + first * step)}`, to: `${cx},${y(lo + (last + 1) * step)}` } : null;
            const zeros = zeroBin >= 0 ? (g.n_zero !== undefined && g.n ? g.n_zero / g.n : 1 - (g.frac_above_zero ?? 0)) : 0;
            const colour = css(colours.get(c));
            return (
              <g key={c} data-testid="violin">
                <title>
                  {`${c}: ${g.n.toLocaleString()} cells · median ${shown_(g.median ?? 0)} · quartiles ${shown_(g.q1 ?? 0)}–${shown_(g.q3 ?? 0)} · ${percent(g.frac_above_zero ?? 0)} above 0`}
                </title>
                {ends && (
                  <polygon
                    points={[ends.from, ...right, ends.to, ...left].join(' ')}
                    fill={colour}
                    fillOpacity={0.75}
                    stroke={colour}
                    strokeWidth={0.75}
                    strokeLinejoin="round"
                  />
                )}
                {zeros > 0 && (
                  <rect
                    data-testid="violin-zeros"
                    x={cx - zeros * full}
                    width={2 * zeros * full}
                    y={y(lo + (zeroBin + 1) * step)}
                    height={Math.max(y(lo + zeroBin * step) - y(lo + (zeroBin + 1) * step), 2)}
                    fill={colour}
                    fillOpacity={0.45}
                  />
                )}
                {g.q1 !== undefined && g.q3 !== undefined && (
                  <rect x={cx - 3} width={6} y={y(g.q3)} height={Math.max(y(g.q1) - y(g.q3), 1)} fill="var(--text)" opacity={0.75} />
                )}
                {g.median !== undefined && <line x1={cx - 5} x2={cx + 5} y1={y(g.median)} y2={y(g.median)} stroke="var(--surface)" strokeWidth={2} />}
                {showAboveZero && (
                  <g transform={`translate(0,${-above})`} data-testid="above-zero">
                    <circle cx={cx} cy={11} r={Math.max(Math.sqrt(g.frac_above_zero ?? 0) * 10, (g.frac_above_zero ?? 0) > 0 ? 1.5 : 0)} fill={colour} />
                    <text className="above-zero" x={cx} y={MARGIN.top + above - 6} textAnchor="middle">
                      {percent(g.frac_above_zero ?? 0)}
                    </text>
                  </g>
                )}
              </g>
            );
          })}
          <CategoryAxis labels={shown.map((x) => x.c)} slot={slot} top={MARGIN.top + height} height={bottom} label={xLabel} />
          {(summary.n_below_zero_at_zero ?? 0) > 0 && (
            <text className="axis-note" x={MARGIN.left + 4} y={MARGIN.top + 10} data-testid="below-zero-note">
              {`${summary.n_below_zero_at_zero!.toLocaleString()} values below 0 drawn at 0 (log scale)`}
            </text>
          )}
          </g>
        </svg>
      )}
    </div>
  );
}

/** The counts a gene's violin outline is drawn from: the bin holding 0 keeps its positive values
 * (its count less the cells at exactly 0, which are drawn apart). Without `nZero` (an older
 * server) the whole bin is left out, as before. */
export function outlineCounts(hist: number[], zeroBin: number, nZero: number | undefined): number[] {
  return hist.map((v, k) => (k === zeroBin ? (nZero !== undefined ? Math.max(v - nZero, 0) : 0) : v));
}

// ---------------------------------------------------------------- stacked bars

export function StackedBars({
  summary,
  colours,
  hidden,
  hiddenRows,
  xLabel,
  proportions,
}: {
  summary: Extract<PlotSummary, { kind: 'bars' }>;
  colours: Map<string, RGB>;
  /** stacks (the coloured categories) left out */
  hidden: Set<string>;
  /** bars left out (categories hidden in the view the plot belongs to) */
  hiddenRows: Set<string>;
  xLabel: string;
  proportions: boolean;
}) {
  const [ref, box] = useBox<HTMLDivElement>();
  const cols = columnOrder(summary.columns)
    .map((j) => ({ c: summary.columns[j], j }))
    .filter((x) => !hidden.has(x.c));
  const rows = columnOrder(summary.rows)
    .map((i) => ({ r: summary.rows[i], total: cols.reduce((s, { j }) => s + summary.counts[i][j], 0), i }))
    .filter((x) => x.total > 0 && !hiddenRows.has(x.r));
  const plotW = Math.max(box.width - MARGIN.left - MARGIN.right, 50);
  const slot = Math.max(24, plotW / Math.max(rows.length, 1));
  const width = MARGIN.left + slot * rows.length + MARGIN.right;
  const bottom = categoryAxisHeight(
    rows.map((x) => x.r),
    slot,
  );
  const height = Math.max(box.height - MARGIN.top - bottom, 60);
  const top = proportions ? 1 : Math.max(...rows.map((x) => x.total), 1);
  const y = (v: number) => MARGIN.top + (1 - v / top) * height;
  return (
    <div ref={ref} className="chart" data-testid="bars">
      {box.width > 0 && (
        <svg width={Math.max(width, box.width)} height={box.height} role="img" aria-label={`Composition of ${xLabel}`}>
          <YAxis lo={0} hi={top} log1p={false} height={height} label={proportions ? 'share of cells' : 'cells'} asPercent={proportions} />
          {rows.map(({ r, total, i }, k) => {
            let acc = 0;
            return (
              <g key={r} data-testid="bar">
                {cols.map(({ c, j }) => {
                  const n = summary.counts[i][j];
                  if (!n) return null;
                  const v = proportions ? n / total : n;
                  const y0 = y(acc + v);
                  const h = y(acc) - y0;
                  acc += v;
                  return (
                    <rect key={c} x={MARGIN.left + k * slot + slot * 0.1} width={slot * 0.8} y={y0} height={Math.max(h, 0.5)} fill={css(colours.get(c))}>
                      <title>{`${r} · ${c}: ${n.toLocaleString()} cells (${percent(n / total)})`}</title>
                    </rect>
                  );
                })}
              </g>
            );
          })}
          <CategoryAxis labels={rows.map((x) => x.r)} slot={slot} top={MARGIN.top + height} height={bottom} label={xLabel} />
        </svg>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- density

export const DENSITY_COLORMAP: ColormapName = 'viridis';

export function Density({ summary, xLabel, yLabel }: { summary: Extract<PlotSummary, { kind: 'density' }>; xLabel: string; yLabel: string }) {
  const [ref, box] = useBox<HTMLDivElement>();
  const canvas = useRef<HTMLCanvasElement>(null);
  const n = summary.bins;
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    c.width = n;
    c.height = n;
    const g = c.getContext('2d')!;
    const img = g.createImageData(n, n);
    const lut = colormapLut(DENSITY_COLORMAP);
    const top = Math.log1p(summary.max || 1);
    for (let row = 0; row < n; row += 1) {
      for (let col = 0; col < n; col += 1) {
        const v = summary.counts[row * n + col];
        const o = ((n - 1 - row) * n + col) * 4; // row 0 = lowest y: drawn at the bottom
        if (!v) continue; // empty bins stay transparent
        const k = Math.round((Math.log1p(v) / top) * 255) * 4;
        img.data.set([lut[k], lut[k + 1], lut[k + 2], 255], o);
      }
    }
    g.putImageData(img, 0, 0);
  }, [summary, n]);
  const w = Math.max(box.width - MARGIN.left - MARGIN.right, 50);
  const h = Math.max(box.height - MARGIN.top - MARGIN.bottom + 24, 60);
  const [x0, x1] = summary.x_range;
  const [y0, y1] = summary.y_range;
  return (
    <div ref={ref} className="chart" data-testid="density">
      <canvas
        ref={canvas}
        style={{ position: 'absolute', left: MARGIN.left, top: MARGIN.top, width: w, height: h, imageRendering: 'pixelated' }}
      />
      {box.width > 0 && (
        <svg width={box.width} height={box.height} style={{ position: 'absolute', inset: 0 }} role="img" aria-label={`Density of ${yLabel} against ${xLabel}`}>
          <YAxis lo={y0} hi={y1} log1p={summary.log1p[1]} height={h} label={yLabel} />
          <XAxis lo={x0} hi={x1} log1p={summary.log1p[0]} left={MARGIN.left} width={w} top={MARGIN.top + h} label={xLabel} />
        </svg>
      )}
    </div>
  );
}

function XAxis({ lo, hi, log1p, left, width, top, label }: { lo: number; hi: number; log1p: boolean; left: number; width: number; top: number; label: string }) {
  const x = (v: number) => left + ((v - lo) / (hi - lo || 1)) * width;
  return (
    <g className="axis">
      <line x1={left} x2={left + width} y1={top} y2={top} />
      {valueTicks(lo, hi, log1p).map((t) => (
        <g key={t.at} transform={`translate(${x(t.at)},${top})`}>
          <line y1={0} y2={4} />
          <text y={16} textAnchor="middle">
            {t.label}
          </text>
        </g>
      ))}
      <text className="label" x={left + width / 2} y={top + 34} textAnchor="middle">
        {label}
        {log1p ? ' (log scale)' : ''}
      </text>
    </g>
  );
}

// ---------------------------------------------------------------- scatter axes (an overlay)

/** Axes over a zoomable scatter whose points sit in the unit square: [0, 1] maps to each
 * feature's range (in its own units, or log1p units). Redrawn as the panel pans and zooms. */
export function scatterAxes(
  x: { range: [number, number]; log1p: boolean; label: string; span: number },
  y: { range: [number, number]; log1p: boolean; label: string; span: number },
  margins: { left: number; bottom: number },
) {
  return (state: OrthoState, rect: Cell, taken: Insets) => {
    const s = 2 ** state.zoom;
    const [tx, ty] = state.target;
    // the world square [0,1]² on screen (y grows upwards: the view is not flipped)
    const sx = (wx: number) => (wx - tx) * s + rect.width / 2;
    const sy = (wy: number) => rect.height / 2 - (wy - ty) * s;
    const wx0 = tx - rect.width / 2 / s;
    const wx1 = tx + rect.width / 2 / s;
    const wy0 = ty - rect.height / 2 / s;
    const wy1 = ty + rect.height / 2 / s;
    // world [0, span] is the feature's range (in its own or log1p units)
    const toData = (a: typeof x, w: number) => a.range[0] + (w / a.span) * (a.range[1] - a.range[0]);
    const toWorld = (a: typeof x, v: number) => ((v - a.range[0]) / (a.range[1] - a.range[0] || 1)) * a.span;
    const xt = valueTicks(toData(x, wx0), toData(x, wx1), x.log1p);
    const yt = valueTicks(toData(y, wy0), toData(y, wy1), y.log1p);
    // the axes keep clear of the title row and an open legend
    const left = margins.left;
    const bottom = rect.height - margins.bottom;
    const top = taken.top ?? 0;
    const right = rect.width - (taken.right ?? 0);
    return (
      <svg width={rect.width} height={rect.height} className="axis" data-testid="scatter-axes">
        {/* points panned beyond the axes are covered: the plotting area ends at the axes */}
        <g className="axis-mask" data-testid="axis-mask">
          <rect x={0} y={0} width={left} height={rect.height} />
          <rect x={0} y={bottom} width={rect.width} height={rect.height - bottom} />
          <rect x={0} y={0} width={rect.width} height={top} />
        </g>
        <line x1={left} x2={left} y1={top} y2={bottom} />
        <line x1={left} x2={right} y1={bottom} y2={bottom} />
        {xt.map((t) => {
          const px = sx(toWorld(x, t.at));
          return px < left || px > right ? null : (
            <g key={`x${t.at}`} transform={`translate(${px},${bottom})`}>
              <line y2={4} />
              <text y={16} textAnchor="middle">
                {t.label}
              </text>
            </g>
          );
        })}
        {yt.map((t) => {
          const py = sy(toWorld(y, t.at));
          return py < top || py > bottom ? null : (
            <g key={`y${t.at}`} transform={`translate(${left},${py})`}>
              <line x1={-4} />
              <text x={-7} dy="0.32em" textAnchor="end">
                {t.label}
              </text>
            </g>
          );
        })}
        <text className="label" x={(left + right) / 2} y={rect.height - 4} textAnchor="middle">
          {x.label}
          {x.log1p ? ' (log scale)' : ''}
        </text>
        <text className="label" transform={`translate(12,${(top + bottom) / 2}) rotate(-90)`} textAnchor="middle">
          {y.label}
          {y.log1p ? ' (log scale)' : ''}
        </text>
      </svg>
    );
  };
}
