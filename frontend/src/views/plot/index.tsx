// Plot view (S7, ADR 0013): two features of a table (obs columns or genes), drawn as the pair
// asks: categories × numbers as violins, categories × categories as stacked bars, numbers ×
// numbers as a zoomable scatter (up to SCATTER_MAX cells) or a density map (more). Violins,
// bars and densities come from server summaries (a few KB, whatever the number of cells);
// the scatter reuses the cached column and gene reads and the GPU colouring of the embedding.
// Category colours are the shared colouring's (palette, dataset colours, colours picked by hand),
// so a category looks the same here as in the embedding.

import { ScatterplotLayer, type ScatterplotLayerProps } from '@deck.gl/layers';
import { type ReactNode, useCallback, useMemo } from 'react';

import { useGene, useGenes, useMeta, useObs, useSummary } from '../../api/queries';
import type { ColumnMeta, DatasetMeta, GeneList, SummaryQuery } from '../../api/types';
import { type ValueColour, ValueColourExtension } from '../../colour/gpu';
import { useWorkspace, type ViewSpec } from '../../state/workspace';
import { Field, formatCount, Help, Select } from '../../ui/controls';
import { GeneSearch } from '../../ui/GeneSearch';
import {
  BASE_RGB,
  ColourFields,
  ColourLegend,
  type ColourLegendData,
  type ColourParams,
  defaultColourParams,
  geneLabel,
  normaliseColourParams,
  useColouring,
} from '../colouring';
import { useLod } from '../lod';
import { type Bounds, MAIN_PANEL, panelLayerId, registerView, type ViewOutput } from '../registry';
import { Density, scatterAxes, StackedBars, Violins } from './charts';

/** A feature: an obs column, or a gene (by name) from X or a layer. */
export type Feature = { kind: 'obs'; name: string } | { kind: 'gene'; name: string; matrix?: string };

export interface PlotParams extends ColourParams {
  /** The view this plot belongs to (an embedding or spatial view): its colour feature is the
   * plot's first feature, its colours and hidden categories are used. null: a free plot. */
  follows: string | null;
  x: Feature | null;
  y: Feature | null;
  log1pX: boolean;
  log1pY: boolean;
  numbersAs: 'auto' | 'points' | 'density';
  proportions: boolean; // bars: shares (else counts)
  aboveZero: boolean; // violins: show the share above 0
  pointSize: number;
}

// Two numbers are drawn as points up to this many cells, else as a density map.
export const SCATTER_MAX = 1_000_000;
const AXES = { left: 48, bottom: 36 }; // room for the scatter's axes
const VALUE_COLOUR = new ValueColourExtension();
const BASE: ValueColour = { lut: new Uint8Array([...BASE_RGB, 255]), mode: 'categories', missing: [...BASE_RGB, 255] };

export type FeatureKind = 'category' | 'number';

/** Whether a feature is a set of categories or a number (null: not in this dataset). */
export function featureKind(f: Feature | null, meta: DatasetMeta | undefined): FeatureKind | null {
  if (!f || !meta) return null;
  if (f.kind === 'gene') return 'number';
  const c = meta.obs.find((x) => x.name === f.name);
  if (!c) return null;
  return c.kind === 'numeric' ? 'number' : 'category';
}

export type PlotKind = 'violin' | 'bars' | 'scatter' | 'density';

/** The plot a pair of features asks for (and which feature holds the categories). */
export function plotKind(
  kx: FeatureKind | null,
  ky: FeatureKind | null,
  n: number,
  numbersAs: PlotParams['numbersAs'],
): { kind: PlotKind; swap: boolean } | null {
  if (!kx || !ky) return null;
  if (kx === 'category' && ky === 'number') return { kind: 'violin', swap: false };
  if (kx === 'number' && ky === 'category') return { kind: 'violin', swap: true };
  if (kx === 'category' && ky === 'category') return { kind: 'bars', swap: false };
  const dense = numbersAs === 'density' || (numbersAs === 'auto' && n > SCATTER_MAX);
  return { kind: dense ? 'density' : 'scatter', swap: false };
}

function featureName(f: Feature | null, genes: GeneList | undefined): string {
  if (!f) return '';
  if (f.kind === 'obs') return f.name;
  const i = genes ? genes.names.indexOf(f.name) : -1;
  const label = i >= 0 && genes ? geneLabel(genes, i) : f.name;
  return f.matrix && f.matrix !== 'X' ? `${label} (${f.matrix})` : label;
}

/** The summary request's form of a feature (genes by their index in the gene list). */
function featureSpec(f: Feature, genes: GeneList | undefined): { s: string; m: string } | null {
  if (f.kind === 'obs') return { s: `obs:${f.name}`, m: 'X' };
  const i = genes ? genes.names.indexOf(f.name) : -1;
  return i >= 0 ? { s: `gene:${i}`, m: f.matrix ?? 'X' } : null;
}

function normaliseFeature(v: unknown): Feature | null {
  if (!v || typeof v !== 'object') return null;
  const r = v as Record<string, unknown>;
  if (typeof r.name !== 'string' || !r.name) return null;
  if (r.kind === 'obs') return { kind: 'obs', name: r.name };
  if (r.kind === 'gene') return { kind: 'gene', name: r.name, ...(typeof r.matrix === 'string' ? { matrix: r.matrix } : {}) };
  return null;
}

function normaliseParams(raw: unknown): PlotParams | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const bool = (v: unknown, d: boolean) => (typeof v === 'boolean' ? v : d);
  return {
    ...normaliseColourParams(r),
    follows: typeof r.follows === 'string' && r.follows ? r.follows : null,
    x: normaliseFeature(r.x),
    y: normaliseFeature(r.y),
    log1pX: bool(r.log1pX, false),
    log1pY: bool(r.log1pY, false),
    numbersAs: r.numbersAs === 'points' || r.numbersAs === 'density' ? r.numbersAs : 'auto',
    proportions: bool(r.proportions, true),
    aboveZero: bool(r.aboveZero, true),
    pointSize: typeof r.pointSize === 'number' && r.pointSize > 0 && r.pointSize <= 10 ? r.pointSize : 2,
    legendCollapsed: typeof r.legendCollapsed === 'boolean' ? r.legendCollapsed : undefined,
  };
}

/** Defaults: the dataset's main categorical column against its first numeric column (a violin),
 * else two numbers, else two categorical columns. */
export function defaultFeatures(meta: DatasetMeta): { x: Feature | null; y: Feature | null } {
  const cats = meta.obs.filter((c) => c.kind === 'categorical' && (c.categories?.length ?? 0) > 1 && (c.categories?.length ?? 0) <= 200);
  const nums = meta.obs.filter((c) => c.kind === 'numeric');
  const named = (cs: ColumnMeta[]) => cs.find((c) => /type|cluster|leiden|louvain|annot/i.test(c.name)) ?? cs[0];
  const cat = cats.length ? named(cats) : undefined;
  if (cat && nums.length) return { x: { kind: 'obs', name: cat.name }, y: { kind: 'obs', name: nums[0].name } };
  if (nums.length > 1) return { x: { kind: 'obs', name: nums[0].name }, y: { kind: 'obs', name: nums[1].name } };
  if (cats.length > 1) return { x: { kind: 'obs', name: cats[0].name }, y: { kind: 'obs', name: cats[1].name } };
  return { x: cat ? { kind: 'obs', name: cat.name } : null, y: null };
}

// ---------------------------------------------------------------- the view

/** One feature's values per cell (numbers), for the scatter: an obs column or a gene. */
function useValues(spec: ViewSpec<PlotParams>, f: Feature | null, version: string | undefined, genes: GeneList | undefined) {
  const obs = useObs(spec.datasetId, spec.table, f?.kind === 'obs' ? f.name : null, version);
  const index = f?.kind === 'gene' && genes ? genes.names.indexOf(f.name) : -1;
  const gene = useGene(spec.datasetId, spec.table, index >= 0 ? index : null, f?.kind === 'gene' ? (f.matrix ?? 'X') : 'X', version);
  if (f?.kind === 'obs') return { values: obs.data?.data as ArrayLike<number> | undefined, error: obs.error };
  return { values: gene.data?.values as ArrayLike<number> | undefined, error: gene.error };
}

/** Positions of the cells with both values, in the unit square, and their cell indices. */
export function scatterPositions(
  xs: ArrayLike<number>,
  ys: ArrayLike<number>,
  logX: boolean,
  logY: boolean,
  aspect = 1,
): { pos: Float32Array; cells: Int32Array; xr: [number, number]; yr: [number, number] } {
  const t = (v: number, log: boolean) => (log ? Math.log1p(Math.max(v, 0)) : v);
  const ok = (v: number) => Number.isFinite(v) && v > -1e30; // MISSING_VALUE is -inf-like
  let x0 = Infinity;
  let x1 = -Infinity;
  let y0 = Infinity;
  let y1 = -Infinity;
  let n = 0;
  for (let i = 0; i < xs.length; i += 1) {
    if (!ok(xs[i]) || !ok(ys[i])) continue;
    const a = t(xs[i], logX);
    const b = t(ys[i], logY);
    if (a < x0) x0 = a;
    if (a > x1) x1 = a;
    if (b < y0) y0 = b;
    if (b > y1) y1 = b;
    n += 1;
  }
  if (!n) return { pos: new Float32Array(0), cells: new Int32Array(0), xr: [0, 1], yr: [0, 1] };
  if (x1 === x0) x1 = x0 + 1;
  if (y1 === y0) y1 = y0 + 1;
  const pos = new Float32Array(2 * n);
  const cells = new Int32Array(n);
  let k = 0;
  for (let i = 0; i < xs.length; i += 1) {
    if (!ok(xs[i]) || !ok(ys[i])) continue;
    pos[2 * k] = ((t(xs[i], logX) - x0) / (x1 - x0)) * aspect;
    pos[2 * k + 1] = (t(ys[i], logY) - y0) / (y1 - y0);
    cells[k] = i;
    k += 1;
  }
  return { pos, cells, xr: [x0, x1], yr: [y0, y1] };
}

function useRgbOf(legend: ColourLegendData | undefined): Map<string, [number, number, number]> {
  return useMemo(() => {
    const m = new Map<string, [number, number, number]>();
    if (legend?.kind === 'categories') legend.labels.forEach((l, i) => m.set(l, legend.colours[i] as [number, number, number]));
    return m;
  }, [legend]);
}

/** The view a plot follows, if it is still open. */
function useFollowed(id: string | null): ViewSpec | null {
  return useWorkspace((s) => (id ? (s.views.find((v) => v.id === id) ?? null) : null));
}

function usePlotView(spec: ViewSpec<PlotParams>): ViewOutput {
  const own = spec.params;
  const followed = useFollowed(own.follows);
  const fp = followed?.params as ColourParams | undefined;
  // the first feature: the followed view's colour (a column or a gene), else the plot's own
  const firstKey = JSON.stringify(own.follows ? (fp?.colorBy ?? null) : own.x);
  const p = useMemo<PlotParams>(
    () => (own.follows ? { ...own, x: (fp?.colorBy as Feature | null | undefined) ?? null } : own),
    [own, firstKey], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const meta = useMeta(spec.datasetId, spec.table);
  const version = meta.data?.version;
  const n = meta.data?.n_obs ?? 0;
  const wantsGenes = p.x?.kind === 'gene' || p.y?.kind === 'gene';
  const genes = useGenes(spec.datasetId, spec.table, version, wantsGenes);
  const kx = featureKind(p.x, meta.data);
  const ky = featureKind(p.y, meta.data);
  const plot = plotKind(kx, ky, n, p.numbersAs);
  const catF = plot ? (plot.kind === 'bars' ? p.y : plot.swap ? p.y : p.x) : null; // coloured categories
  const numF = plot?.kind === 'violin' ? (plot.swap ? p.x : p.y) : null;
  // categories are coloured by their own column (shared palettes and picked colours); a
  // scatter by the user's choice
  // when the categories are the followed view's colour column, its colour settings (palette,
  // colours picked by hand, hidden categories) apply, and its legend controls them
  const usesFollowed = !!fp && plot?.kind !== 'scatter' && catF?.kind === 'obs' && fp.colorBy?.kind === 'obs' && fp.colorBy.name === catF.name;
  const colourParams = useMemo<ColourParams>(
    () =>
      usesFollowed
        ? fp!
        : plot && plot.kind !== 'scatter' && catF?.kind === 'obs'
          ? { ...p, colorBy: { kind: 'obs', name: catF.name } }
          : p,
    [p, fp, usesFollowed, plot?.kind, catF], // eslint-disable-line react-hooks/exhaustive-deps
  );
  const colouring = useColouring(spec.datasetId, spec.table, colourParams, n || null);
  const rgbOf = useRgbOf(colouring.legend);
  const hidden = useMemo(
    () => new Set(colourParams.colorBy?.kind === 'obs' ? colourParams.hidden : []),
    [colourParams.colorBy, colourParams.hidden],
  );

  // ---- summaries (violin, bars, density)
  const q = useMemo<SummaryQuery | null>(() => {
    if (!plot || plot.kind === 'scatter' || !p.x || !p.y) return null;
    const [a, b] = plot.kind === 'violin' ? [catF!, numF!] : [p.x, p.y];
    const sa = featureSpec(a, genes.data);
    const sb = featureSpec(b, genes.data);
    if (!sa || !sb) return null;
    const logs = plot.kind === 'violin' ? `0${(plot.swap ? p.log1pX : p.log1pY) ? 1 : 0}` : plot.kind === 'density' ? `${p.log1pX ? 1 : 0}${p.log1pY ? 1 : 0}` : '';
    return {
      kind: plot.kind,
      x: sa.s,
      y: sb.s,
      ...(sa.m !== 'X' ? { xm: sa.m } : {}),
      ...(sb.m !== 'X' ? { ym: sb.m } : {}),
      bins: plot.kind === 'density' ? 128 : 64,
      ...(logs && logs !== '00' ? { log1p: logs } : {}),
    };
  }, [plot?.kind, plot?.swap, p.x, p.y, p.log1pX, p.log1pY, genes.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const summary = useSummary(spec.datasetId, spec.table, version, q);

  // ---- scatter
  const scatter = plot?.kind === 'scatter';
  const xv = useValues(spec, scatter ? p.x : null, version, genes.data);
  const yv = useValues(spec, scatter ? p.y : null, version, genes.data);
  // the plotting area's shape (panel minus the axes, title row and an open legend): x runs over
  // [0, aspect] so the points fill the panel at one zoom
  const size = useLod((s) => s.size[spec.id]);
  const legendOpen = !(p.legendCollapsed ?? false) && colouring.coloured;
  const aspect = size ? Math.max(0.3, (size[0] - AXES.left - (legendOpen ? 250 : 0)) / Math.max(size[1] - AXES.bottom - 30, 1)) : 1;
  const roundedAspect = Math.round(aspect * 20) / 20; // refit only on real changes of shape
  const pts = useMemo(
    () =>
      scatter && xv.values && yv.values && xv.values.length === n && yv.values.length === n
        ? scatterPositions(xv.values, yv.values, p.log1pX, p.log1pY, roundedAspect)
        : null,
    [scatter, xv.values, yv.values, n, p.log1pX, p.log1pY, roundedAspect],
  );
  const colourAttr = useMemo(() => {
    if (!pts) return null;
    const src = colouring.values?.value;
    const out = new Float32Array(pts.cells.length);
    if (src) for (let k = 0; k < pts.cells.length; k += 1) out[k] = src[pts.cells[k]];
    return { value: out, size: 1 };
  }, [pts, colouring.values]);
  const xName = featureName(p.x, genes.data);
  const yName = featureName(p.y, genes.data);
  const layers = useMemo(() => {
    if (!pts || !colourAttr) return [];
    return [
      new ScatterplotLayer({
        id: panelLayerId(spec.id, MAIN_PANEL, 'points'),
        data: { length: pts.cells.length, attributes: { getPosition: { value: pts.pos, size: 2 }, getColourValue: colourAttr } },
        getFillColor: [...BASE_RGB, 255],
        getRadius: 1,
        radiusScale: p.pointSize,
        radiusUnits: 'pixels',
        stroked: false,
        pickable: true,
        opacity: 0.8,
        extensions: [VALUE_COLOUR],
        valueColour: (colouring.coloured ? colouring.valueColour : null) ?? BASE,
      } as ScatterplotLayerProps & { valueColour: ValueColour }),
    ];
  }, [pts, colourAttr, colouring.valueColour, colouring.coloured, p.pointSize, spec.id]);
  const bounds: Bounds | null = pts ? [0, 0, roundedAspect, 1] : null;
  const overlay = useMemo(
    () =>
      pts
        ? scatterAxes({ range: pts.xr, log1p: p.log1pX, label: xName, span: roundedAspect }, { range: pts.yr, log1p: p.log1pY, label: yName, span: 1 })
        : undefined,
    [pts, p.log1pX, p.log1pY, xName, yName, roundedAspect],
  );
  const tooltip = useCallback(
    (i: number) => {
      if (!pts || !xv.values || !yv.values) return null;
      const cell = pts.cells[i];
      const extra = colouring.coloured ? colouring.tooltip(cell) : null;
      return `${xName}: ${(+xv.values[cell]).toPrecision(4)} · ${yName}: ${(+yv.values[cell]).toPrecision(4)}${extra ? ` · ${extra}` : ''}`;
    },
    [pts, xv.values, yv.values, xName, yName, colouring],
  );

  // ---- output
  const s = summary.data;
  const catName = featureName(catF, genes.data);
  const numName = featureName(numF, genes.data);
  let content: ReactNode;
  if (s?.kind === 'violin' && plot?.kind === 'violin') {
    content = <Violins summary={s} colours={rgbOf} hidden={hidden} xLabel={catName} yLabel={numName} showAboveZero={p.aboveZero && numF?.kind === 'gene'} />;
  } else if (s?.kind === 'bars' && plot?.kind === 'bars') {
    content = <StackedBars summary={s} colours={rgbOf} hidden={hidden} xLabel={xName} proportions={p.proportions} />;
  } else if (s?.kind === 'density' && plot?.kind === 'density') {
    content = <Density summary={s} xLabel={xName} yLabel={yName} />;
  }
  const error = meta.error ?? summary.error ?? xv.error ?? yv.error ?? (genes.error as unknown);
  const missingGene = wantsGenes && genes.data && ((p.x?.kind === 'gene' && !featureSpec(p.x, genes.data)) || (p.y?.kind === 'gene' && !featureSpec(p.y, genes.data)));
  const unset = !!meta.data && (!plot || missingGene || (!!own.follows && !followed));
  const ready = scatter ? !!pts && !!colourAttr : !!content;
  const legend: ColourLegendData | undefined =
    plot?.kind === 'density' && s?.kind === 'density'
      ? { kind: 'numeric', name: 'cells per bin', range: [0, s.max], colormap: 'viridis' }
      : (plot?.kind === 'scatter' && !colouring.coloured) || usesFollowed
        ? undefined // the followed view's legend controls these categories
        : colouring.legend;
  return {
    layers,
    bounds,
    panels: scatter && overlay ? [{ id: MAIN_PANEL, layers, bounds, tooltip, overlay }] : undefined,
    flipY: false,
    margins: scatter ? AXES : undefined,
    fitKey: `${version}|${xName}|${yName}|${p.log1pX}|${p.log1pY}`,
    content: scatter ? undefined : content,
    status: error ? 'error' : unset ? 'error' : ready ? 'ready' : 'loading',
    error: error
      ? String((error as Error).message)
      : unset
        ? own.follows && !followed
          ? 'The view this plot belongs to was closed.'
          : missingGene
            ? 'A chosen gene is not in this dataset.'
            : own.follows && !p.x
              ? 'Colour the view by a column or a gene: that is the first feature of this plot.'
              : own.follows
                ? 'Choose the second feature in the view’s settings (Plot against).'
                : 'Choose two features in the settings (columns or genes).'
        : undefined,
    tooltip,
    caption:
      plot && n
        ? `${plot.kind === 'violin' ? 'violins' : plot.kind === 'bars' ? 'composition' : plot.kind === 'density' ? `density of ${formatCount(n)} cells` : `${formatCount(pts?.cells.length ?? n)} cells`}`
        : undefined,
    n,
    total: scatter ? (pts?.cells.length ?? 0) : 0,
    legend,
  };
}

// ---------------------------------------------------------------- settings

function FeatureField({ spec, which, title }: { spec: ViewSpec<PlotParams>; which: 'x' | 'y'; title?: string }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const m = meta.data;
  const readable = m?.matrices.filter((x) => x.access !== 'needs_conversion') ?? [];
  const genes = useGenes(spec.datasetId, spec.table, m?.version, readable.length > 0);
  if (!m) return null;
  const f = spec.params[which];
  const value = f?.kind === 'obs' ? `obs:${f.name}` : null;
  const set = (next: Feature | null) => update(spec.id, { [which]: next });
  const label = title ?? (which === 'x' ? 'Horizontal (x)' : 'Vertical (y)');
  return (
    <Field
      label={label}
      help={
        <p>
          A column of the cells (categories or numbers) or a gene. Categories against numbers give violins; two category
          columns give the composition (stacked bars); two numbers a scatter, or a density map for very many cells.
        </p>
      }
    >
      <Select
        label={label}
        testId={`plot-${which}`}
        value={value}
        allowNone={f?.kind === 'gene' ? `Gene: ${f.name}` : 'Choose…'}
        onChange={(v) => set(v ? { kind: 'obs', name: v.slice(4) } : f?.kind === 'gene' ? f : null)}
        groups={[
          {
            label: 'Categories',
            options: m.obs
              .filter((c) => c.kind !== 'numeric' && (c.categories?.length ?? 0) <= 200)
              .map((c) => ({ value: `obs:${c.name}`, label: c.name, hint: c.categories ? `${c.categories.length}` : c.kind })),
          },
          { label: 'Numbers', options: m.obs.filter((c) => c.kind === 'numeric').map((c) => ({ value: `obs:${c.name}`, label: c.name })) },
        ]}
      />
      {genes.data && (
        <GeneSearch genes={genes.data} onPick={(g) => set({ kind: 'gene', name: g, ...(f?.kind === 'gene' && f.matrix ? { matrix: f.matrix } : {}) })} testId={`plot-${which}-gene`} />
      )}
      {f?.kind === 'gene' && readable.length > 1 && (
        <Select
          label={`Values of the ${which} gene from`}
          value={f.matrix ?? 'X'}
          onChange={(v) => v && set({ ...f, matrix: v === 'X' ? undefined : v })}
          groups={[{ label: 'matrices', options: readable.map((x) => ({ value: x.name, label: x.name, hint: x.dtype })) }]}
        />
      )}
    </Field>
  );
}

function PlotPanel({ spec }: { spec: ViewSpec<PlotParams> }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const followed = useFollowed(spec.params.follows);
  const fx = (followed?.params as ColourParams | undefined)?.colorBy as Feature | null | undefined;
  const p = spec.params.follows ? { ...spec.params, x: fx ?? null } : spec.params;
  if (!meta.data) return <p className="muted">{meta.error ? String((meta.error as Error).message) : 'Loading…'}</p>;
  const plot = plotKind(featureKind(p.x, meta.data), featureKind(p.y, meta.data), meta.data.n_obs, p.numbersAs);
  const check = (key: keyof PlotParams, text: string, testId: string) => (
    <label className="check" data-testid={testId}>
      <input type="checkbox" checked={!!p[key]} onChange={(e) => update(spec.id, { [key]: e.target.checked })} />
      {text}
    </label>
  );
  const numbersX = featureKind(p.x, meta.data) === 'number';
  const numbersY = featureKind(p.y, meta.data) === 'number';
  return (
    <div className="panel-fields">
      {spec.params.follows ? (
        <>
          <p className="small" data-testid="plot-first">
            Horizontal (x): {p.x ? featureName(p.x, undefined) : 'no colour'}{' '}
            <span className="muted">(the colour of {followed ? `“${followed.title}”` : 'a closed view'})</span>
          </p>
          <FeatureField spec={spec} which="y" />
        </>
      ) : (
        <>
          <FeatureField spec={spec} which="x" />
          <FeatureField spec={spec} which="y" />
          <button
            className="link small"
            onClick={() => update(spec.id, { x: p.y, y: p.x, log1pX: p.log1pY, log1pY: p.log1pX })}
            data-testid="plot-swap"
          >
            swap x and y
          </button>
        </>
      )}
      {numbersX && check('log1pX', 'Log scale for x (log1p)', 'plot-log-x')}
      {numbersY && check('log1pY', 'Log scale for y (log1p)', 'plot-log-y')}
      {plot?.kind === 'violin' && check('aboveZero', 'Show the share of cells above 0 (genes)', 'plot-above-zero')}
      {plot?.kind === 'bars' && check('proportions', 'Shares of each bar (else cell counts)', 'plot-proportions')}
      {(plot?.kind === 'scatter' || plot?.kind === 'density') && (
        <Field label="Draw" help={<p>Points up to {formatCount(SCATTER_MAX)} cells; above that a map of how many cells fall in each small square.</p>}>
          <Select
            label="Draw"
            testId="plot-numbers-as"
            value={p.numbersAs}
            onChange={(v) => v && update(spec.id, { numbersAs: v as PlotParams['numbersAs'] })}
            groups={[
              {
                label: 'as',
                options: [
                  { value: 'auto', label: 'Automatic' },
                  { value: 'points', label: 'Points' },
                  { value: 'density', label: 'Density map' },
                ],
              },
            ]}
          />
        </Field>
      )}
      {plot?.kind === 'scatter' && <ColourFields spec={spec} />}
      <p className="muted small">
        {formatCount(meta.data.n_obs)} cells
        <Help label="About the plot">
          <p>
            Violins, bars and density maps are computed on the server from all cells, so they stay quick for large
            datasets. Category colours are the same as in the embedding (palette, dataset colours, colours you picked).
          </p>
        </Help>
      </p>
    </div>
  );
}

/** "Plot against a second feature" in an embedding or spatial view's settings: ticked, a plot
 * view opens beside it whose first feature is the view's colour and whose second is chosen
 * here; unticked (or the view closed), the plot closes. */
export function PlotCompanionField({ spec }: { spec: ViewSpec }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const companion = useWorkspace((s) => s.views.find((v) => v.type === 'plot' && v.params.follows === spec.id)) as
    | ViewSpec<PlotParams>
    | undefined;
  const addView = useWorkspace((s) => s.addView);
  const removeView = useWorkspace((s) => s.removeView);
  const select = useWorkspace((s) => s.select);
  if (!meta.data) return null;
  const toggle = (on: boolean) => {
    if (!on) {
      if (companion) removeView(companion.id);
      return;
    }
    const id = addView({
      type: 'plot',
      datasetId: spec.datasetId,
      table: spec.table,
      title: `${spec.title} · plot`,
      params: companionParams(meta.data!, spec.id, spec.params as unknown as ColourParams) as never,
    });
    // the settings stay on this view, where the second feature is chosen
    if (id) select(spec.id);
  };
  return (
    <>
      <label className="check" data-testid="plot-companion">
        <input type="checkbox" checked={!!companion} onChange={(e) => toggle(e.target.checked)} />
        Plot against a second feature
        <Help label="About plotting two features">
          <p>
            Opens a plot beside this view: the colour feature above against a second one chosen here. Categories against
            numbers give violins, two category columns the composition (stacked bars), two numbers a scatter or a density
            map. Category colours and hidden categories are this view's.
          </p>
        </Help>
      </label>
      {companion && <FeatureField spec={companion} which="y" title="Plot against" />}
    </>
  );
}

/** A plot belonging to a view (its first feature is that view's colour). */
export function companionParams(meta: DatasetMeta, follows: string, colour: ColourParams): PlotParams {
  return {
    ...defaultColourParams(meta),
    palette: colour.palette,
    customColours: colour.customColours,
    follows,
    x: null,
    y: null,
    log1pX: false,
    log1pY: false,
    numbersAs: 'auto',
    proportions: true,
    aboveZero: true,
    pointSize: 2,
  };
}

registerView<PlotParams>({
  type: 'plot',
  label: 'Plot',
  rank: 20,
  // not opened on its own: a plot belongs to an embedding or spatial view ("Plot against")
  create: () => null,
  normalise: normaliseParams,
  useView: usePlotView,
  Panel: PlotPanel,
  Legend: ColourLegend as never,
});
