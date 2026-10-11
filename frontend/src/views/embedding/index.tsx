// Embedding view (F11a): cells as points in an obsm embedding (UMAP, PCA, …), coloured by an
// obs column (categories with the dataset's or a default palette; numbers with a colormap).
//
// GPU uploads: positions and colours are separate binary attributes with stable identities, so
// a colour change uploads only colours, and point size and opacity (uniforms) upload nothing.

import { ScatterplotLayer, type ScatterplotLayerProps } from '@deck.gl/layers';
import { useCallback, useEffect, useMemo, useRef } from 'react';

import { useEmbedding, useGene, useGenes, useMeta, useObs, useObsLabels, useSubset } from '../../api/queries';
import type { ColumnMeta, DatasetMeta, GeneList, GeneVector } from '../../api/types';
import {
  categoryColouring,
  constantColouring,
  displayRange,
  numberColouring,
  robustRange,
} from '../../colour/colouring';
import { gpuValues, type ValueColour, ValueColourExtension } from '../../colour/gpu';
import {
  categoryColours,
  COLORMAP_LABELS,
  colormapGradient,
  type ColormapName,
  COLORMAPS,
  coloursExtended,
  hexToRgb,
  PALETTE_LABELS,
  type PaletteName,
  PALETTES,
  normaliseHex,
  paletteSize,
  resolvePalette,
  type RGB,
  rgbToHex,
} from '../../colour/palettes';
import { isMissing } from '../../colour/missing';
import { assignSlots } from '../../colour/slots';
import { event } from '../../telemetry/telemetry';
import { Field, formatCount, Help, type OptionGroup, Select, Slider } from '../../ui/controls';
import { CANVAS_BUDGET, drawCount, REORDER_ABOVE, representativeOrder, SUBSET_SIZE, useLod } from '../lod';
import { Conversion } from '../../ui/Conversion';
import { GeneSearch } from '../../ui/GeneSearch';
import { useWorkspace, type ViewSpec } from '../../state/workspace';
import { type Bounds, registerView, type ViewOutput } from '../registry';

/** What the points are coloured by: an obs column, or a gene (its var index name) read from
 * `matrix` (X or a layer; X when absent). */
export interface ColourBy {
  kind: 'obs' | 'gene';
  name: string;
  matrix?: string;
}

export interface EmbeddingParams {
  embedding: string;
  dims: [number, number];
  colorBy: ColourBy | null;
  pointSize: number;
  opacity: number;
  hidden: string[]; // hidden category names of the current colorBy column
  palette: PaletteName; // categories
  // Colours chosen by hand: column name → category name → "#rrggbb" (over the palette).
  customColours: Record<string, Record<string, string>>;
  colormap: ColormapName; // numbers
  sortByValue: boolean; // numbers: draw higher values on top
  allCells: boolean; // large datasets: always draw every cell (else a subset when zoomed out)
  legendCollapsed?: boolean;
  [key: string]: unknown;
}

const BASE_RGB: RGB = [80, 110, 160];
// Legend order: 2 before 10, "cluster 9" before "cluster 10" (codes keep the stored order).
const NATURAL = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
// Columns with more categories than this are listed but not chosen by default.
const DEFAULT_MAX_CATEGORIES = 60;
const VALUE_COLOUR = new ValueColourExtension();

// Columns with more categories cannot be told apart by colour (and their legend would be huge).
const MAX_COLOUR_CATEGORIES = 5_000;

function colourable(c: ColumnMeta): boolean {
  if (c.kind === 'numeric' || c.kind === 'boolean' || c.kind === 'string') return true;
  return c.kind === 'categorical' && c.categories !== null && c.categories.length <= MAX_COLOUR_CATEGORIES;
}

// Names that usually hold the annotation people want to see first.
const PREFERRED = /cell.?type|annotation|cluster|leiden|louvain/i;

/** Default colouring: a categorical column, preferring cell-type/cluster names, then columns
 * that come with colours, then any with few enough categories. */
export function defaultColumn(meta: DatasetMeta): string | null {
  const cats = meta.obs.filter(
    (c) =>
      c.kind === 'categorical' &&
      c.categories &&
      c.categories.length > 1 &&
      c.categories.length <= DEFAULT_MAX_CATEGORIES,
  );
  const rank = (c: ColumnMeta) => (PREFERRED.test(c.name) ? 0 : 2) + (c.colors ? 0 : 1);
  return [...cats].sort((a, b) => rank(a) - rank(b))[0]?.name ?? null;
}

function defaultColourBy(meta: DatasetMeta): ColourBy | null {
  const name = defaultColumn(meta);
  return name ? { kind: 'obs', name } : null;
}

/** Smaller points for more cells: they stay distinct, and drawing cost grows with point area. */
export function defaultPointSize(n: number): number {
  return n < 20_000 ? 3 : n < 200_000 ? 2 : n < 1_000_000 ? 1.2 : 0.8;
}

/** `colorBy` from saved settings: a column name (older builds) or `{kind, name}`. */
function normaliseColourBy(v: unknown): ColourBy | null {
  if (typeof v === 'string' && v) return { kind: 'obs', name: v };
  if (v && typeof v === 'object') {
    const c = v as Partial<ColourBy>;
    if (c.kind === 'obs' && typeof c.name === 'string' && c.name) return { kind: 'obs', name: c.name };
    if (c.kind === 'gene' && typeof c.name === 'string' && c.name) {
      return typeof c.matrix === 'string' && c.matrix && c.matrix !== 'X'
        ? { kind: 'gene', name: c.name, matrix: c.matrix }
        : { kind: 'gene', name: c.name };
    }
  }
  return null;
}

/** Hand-picked colours from saved settings: only well-formed entries. */
function normaliseCustom(v: unknown): Record<string, Record<string, string>> {
  const out: Record<string, Record<string, string>> = {};
  if (!v || typeof v !== 'object') return out;
  for (const [col, m] of Object.entries(v as Record<string, unknown>)) {
    if (!m || typeof m !== 'object') continue;
    const ok: Record<string, string> = {};
    for (const [label, hex] of Object.entries(m as Record<string, unknown>)) {
      const h = normaliseHex(hex);
      if (h) ok[label] = h;
    }
    if (Object.keys(ok).length) out[col] = ok;
  }
  return out;
}

/** Saved parameters made valid (older builds, hand edits); null if unusable. */
export function normaliseParams(raw: unknown): EmbeddingParams | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.embedding !== 'string' || !r.embedding) return null;
  const num = (v: unknown, lo: number, hi: number, d: number) =>
    typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d;
  const dimsOk =
    Array.isArray(r.dims) &&
    r.dims.length === 2 &&
    r.dims.every((d) => Number.isInteger(d) && d >= 0) &&
    r.dims[0] !== r.dims[1];
  return {
    embedding: r.embedding,
    dims: dimsOk ? (r.dims as [number, number]) : [0, 1],
    colorBy: normaliseColourBy(r.colorBy),
    pointSize: num(r.pointSize, 0.5, 10, 2),
    opacity: num(r.opacity, 0.05, 1, 0.85),
    hidden: Array.isArray(r.hidden) ? r.hidden.filter((h): h is string => typeof h === 'string') : [],
    palette: PALETTES.includes(r.palette as PaletteName) ? (r.palette as PaletteName) : 'auto',
    customColours: normaliseCustom(r.customColours),
    sortByValue: typeof r.sortByValue === 'boolean' ? r.sortByValue : true,
    allCells: typeof r.allCells === 'boolean' ? r.allCells : false,
    colormap: COLORMAPS.includes(r.colormap as ColormapName) ? (r.colormap as ColormapName) : 'viridis',
    legendCollapsed: typeof r.legendCollapsed === 'boolean' ? r.legendCollapsed : undefined,
  };
}

function computeBounds(pos: Float32Array): Bounds | null {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (let i = 0; i < pos.length; i += 2) {
    const x = pos[i];
    const y = pos[i + 1];
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  return Number.isFinite(x0) ? [x0, y0, x1, y1] : null;
}

export type EmbeddingLegendData =
  | {
      kind: 'categories';
      name: string;
      labels: string[];
      colours: RGB[];
      counts: number[];
      order: number[]; // natural order of the labels
      extended: boolean; // more categories than palette colours: interpolated colours added
      custom: boolean[]; // colour chosen by hand
    }
  | { kind: 'numeric'; name: string; range: [number, number]; colormap: ColormapName };

/** The parameters this dataset can actually show; a note when a saved choice no longer exists. */
export function effectiveParams(
  p: EmbeddingParams,
  meta: DatasetMeta | undefined,
  genes?: GeneList,
): { p: EmbeddingParams; notice?: string } {
  if (!meta) return { p };
  const notes: string[] = [];
  let out = p;
  const emb = meta.embeddings.find((e) => e.key === p.embedding);
  if (!emb && meta.embeddings.length) {
    notes.push(`embedding “${p.embedding}” not found, showing ${meta.embeddings[0].key}`);
    out = { ...out, embedding: meta.embeddings[0].key, dims: [0, 1] };
  } else if (emb && Math.max(...p.dims) >= emb.dims) {
    out = { ...out, dims: [0, 1] };
  }
  if (p.colorBy?.kind === 'gene') {
    const { name } = p.colorBy;
    let matrix = meta.matrices.find((m) => m.name === (p.colorBy!.matrix ?? 'X'));
    if (!matrix) {
      notes.push(`values “${p.colorBy.matrix}” not found, using X`);
      matrix = meta.matrices.find((m) => m.name === 'X');
      out = { ...out, colorBy: { kind: 'gene', name } };
    }
    if (!matrix || matrix.access === 'needs_conversion') {
      notes.push('gene expression in this dataset needs a one-time conversion first');
      out = { ...out, colorBy: null, hidden: [] };
    } else if (genes && !genes.names.includes(name)) {
      notes.push(`gene “${name}” not found`);
      out = { ...out, colorBy: null, hidden: [] };
    }
  } else if (p.colorBy && !meta.obs.some((c) => c.name === p.colorBy!.name)) {
    notes.push(`column “${p.colorBy.name}” not found`);
    out = { ...out, colorBy: null, hidden: [] };
  }
  return { p: out, notice: notes.length ? `Saved settings changed: ${notes.join('; ')}.` : undefined };
}

/** A gene's readable name: its symbol when the index holds ids. */
export function geneLabel(genes: GeneList, i: number): string {
  return genes.symbols?.[i] || genes.names[i];
}

/** Colour scale of a gene: from 0 (or the lowest value, if negative) to the 99th percentile of
 * its non-zero values, so a few extreme cells do not wash out the rest. */
export function geneRange(g: GeneVector): [number, number] {
  const lo = Math.min(0, g.min);
  const hi = g.p99 > lo ? g.p99 : g.max > lo ? g.max : lo + 1;
  return [lo, hi];
}

function useEmbeddingView(spec: ViewSpec<EmbeddingParams>): ViewOutput {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const version = meta.data?.version;
  const wantsGene = spec.params.colorBy?.kind === 'gene';
  const genes = useGenes(spec.datasetId, spec.table, version, wantsGene);
  const { p, notice } = useMemo(
    () => effectiveParams(spec.params, meta.data, genes.data),
    [spec.params, meta.data, genes.data],
  );
  const emb = useEmbedding(spec.datasetId, spec.table, p.embedding, p.dims, version);
  const positions = emb.data?.data as Float32Array | undefined;
  const n = positions ? positions.length / 2 : null;
  const column = (p.colorBy?.kind === 'obs' && meta.data?.obs.find((c) => c.name === p.colorBy!.name)) || null;
  const obs = useObs(spec.datasetId, spec.table, column ? column.name : null, version);
  const labelsQ = useObsLabels(spec.datasetId, spec.table, column?.kind === 'string' ? column.name : null, version);
  const geneIndexOf = useMemo(() => (genes.data ? new Map(genes.data.names.map((g, i) => [g, i])) : null), [genes.data]);
  const geneChoice = p.colorBy?.kind === 'gene' ? p.colorBy : null;
  const geneIndex = geneChoice && geneIndexOf ? (geneIndexOf.get(geneChoice.name) ?? null) : null;
  const geneQ = useGene(spec.datasetId, spec.table, geneIndex, geneChoice?.matrix ?? 'X', version);
  const gene = geneQ.data && geneQ.data.values.length === n ? geneQ.data : undefined;
  const values = obs.data && obs.data.data.length === n ? obs.data : undefined;
  // What the colour legend and tooltips call the colouring: a column, or a gene (its symbol).
  const colourName = column
    ? column.name
    : geneChoice && geneIndex !== null
      ? geneLabel(genes.data!, geneIndex) + (geneChoice.matrix ? ` (${geneChoice.matrix})` : '')
      : null;

  // Keep the corrected parameters, so the notice is shown only once.
  useEffect(() => {
    if (notice) update(spec.id, p);
  }, [notice]); // eslint-disable-line react-hooks/exhaustive-deps

  // Values for the GPU and the range of numbers: once per loaded column.
  const prepared = useMemo(() => {
    const t0 = performance.now();
    let out: { attr: { value: Float32Array; size: number }; range: [number, number] | null } | null = null;
    if (gene) {
      // decoded in the GPU's form already (missing = MISSING_VALUE): no copy
      out = { attr: { value: gene.values, size: 1 }, range: geneRange(gene) };
    } else if (values) {
      const gpu = gpuValues(values.data, values.kind === 'numeric');
      out = { attr: { value: gpu, size: 1 }, range: values.kind === 'numeric' ? robustRange(values.data) : null };
    }
    if (out) event('colour_values_prepared', performance.now() - t0, { n: out.attr.value.length });
    return out;
  }, [values, gene]);

  const labels = useMemo<string[] | null>(() => {
    if (!column || !values || values.kind === 'numeric') return null;
    if (values.kind === 'boolean') return ['false', 'true'];
    if (column.kind === 'string') return labelsQ.data?.categories ?? null;
    return column.categories;
  }, [column, values, labelsQ.data]);
  const order = useMemo(
    () => (labels ? labels.map((_, i) => i).sort((a, b) => NATURAL.compare(labels[a], labels[b])) : null),
    [labels],
  );

  // Category colours: by name (a stable hash of the label, see colour/slots.ts), or the
  // dataset's own.
  // The dataset's own colours (uns['<col>_colors']) are the default whenever it has them; for a
  // text column they come with its labels.
  const given =
    column?.kind === 'categorical' || column?.kind === 'boolean'
      ? column.colors
      : column?.kind === 'string'
        ? (labelsQ.data?.colors ?? null)
        : null;
  const palette = labels ? resolvePalette(p.palette, labels.length, given) : null;
  const slots = useMemo(
    () => (labels && order && palette && palette !== 'dataset' ? assignSlots(labels, order, paletteSize(palette, labels.length)) : []),
    [labels, order, palette],
  );
  // Colours picked by hand (in the legend) replace the palette's, per column and category.
  const custom = column ? p.customColours[column.name] : undefined;
  const customKey = custom ? JSON.stringify(custom) : '';
  const colours = useMemo(() => {
    if (!labels || !palette) return null;
    const base = categoryColours(palette, slots, given);
    const picked: Record<string, string> = customKey ? JSON.parse(customKey) : {};
    return base.map((c, i) => (picked[labels[i]] ? hexToRgb(picked[labels[i]]) : c));
  }, [labels, palette, slots, given, customKey]);

  const hiddenKey = p.hidden.join('\u0000');
  const coloured = !!column || geneIndex !== null;
  const valueColour = useMemo<ValueColour | null>(() => {
    if (!coloured) return constantColouring(BASE_RGB);
    if (!prepared) return null;
    if (prepared.range) return { ...numberColouring(p.colormap, prepared.range), sortByValue: p.sortByValue };
    if (!labels || !colours) return null;
    const hidden = new Set(hiddenKey ? hiddenKey.split('\u0000') : []);
    return categoryColouring(colours, labels.map((l) => hidden.has(l)));
  }, [coloured, prepared, labels, colours, p.colormap, p.sortByValue, hiddenKey]);

  const bounds = useMemo(() => (positions ? computeBounds(positions) : null), [positions]);
  // Stable attribute objects: deck.gl uploads an attribute only when its object changes.
  const posAttr = useMemo(() => (positions ? { value: positions, size: 2 } : null), [positions]);
  const valueAttr = coloured && prepared ? prepared.attr : null;
  const data = useMemo(
    () =>
      posAttr && n !== null
        ? { length: n, attributes: { getPosition: posAttr, ...(valueAttr ? { getColourValue: valueAttr } : {}) } }
        : null,
    [posAttr, valueAttr, n],
  );
  const baseData = useMemo(
    () => (posAttr && n !== null ? { length: n, attributes: { getPosition: posAttr } } : null),
    [posAttr, n],
  );

  const legend = useMemo<EmbeddingLegendData | undefined>(() => {
    if (gene && colourName && prepared?.range) {
      return { kind: 'numeric', name: colourName, range: displayRange(prepared.range, p.colormap), colormap: p.colormap };
    }
    if (!column || !values) return undefined;
    if (values.kind === 'numeric') {
      return prepared?.range
        ? { kind: 'numeric', name: column.name, range: displayRange(prepared.range, p.colormap), colormap: p.colormap }
        : undefined;
    }
    if (!labels || !colours || !order || !palette) return undefined;
    return {
      kind: 'categories',
      name: column.name,
      labels,
      colours,
      counts: countCodes(values.data, labels.length),
      order,
      extended: coloursExtended(palette, labels.length),
      custom: labels.map((l) => !!custom?.[l]),
    };
  }, [gene, colourName, column, values, labels, colours, order, palette, prepared, p.colormap, custom]);

  const hiddenSet = useMemo(() => new Set(p.hidden), [p.hidden]);
  const tooltip = useCallback((i: number): string | null => {
    if (gene && colourName) {
      const v = gene.values[i];
      return `${colourName}: ${isMissing(v) ? 'NA' : formatValue(v)}`;
    }
    if (!column || !values) return null;
    const v = values.data[i];
    if (values.kind === 'numeric') return `${column.name}: ${Number.isFinite(v) ? formatValue(v) : 'NA'}`;
    const label = v >= 0 ? labels?.[v] : undefined;
    if (label !== undefined && hiddenSet.has(label)) return null; // hidden cells are not reported
    return `${column.name}: ${label ?? 'NA'}`;
  }, [gene, colourName, column, values, labels, hiddenSet]);

  // What is drawn: the current colouring when ready; while a new one loads (or if it failed),
  // the last one shown, else the points in the base colour. The plot never goes blank.
  const fatal = meta.error ?? emb.error;
  const colourError = obs.error ?? labelsQ.error ?? genes.error ?? geneQ.error;
  const ready = !!data && !!valueColour && (!coloured || !!valueAttr);
  const current = useMemo<Shown | null>(
    () => (ready ? { data: data!, valueColour: valueColour!, legend, tooltip, n: n! } : null),
    [ready, data, valueColour, legend, tooltip, n],
  );
  const lastGood = useRef<Shown | null>(null);
  if (current) lastGood.current = current;
  const shown =
    current ??
    (lastGood.current && lastGood.current.n === n ? lastGood.current : null) ??
    (baseData ? { data: baseData, valueColour: BASE_COLOURING, legend: undefined, tooltip: () => null, n: n! } : null);

  // Large datasets (views/lod.ts): the cells in a representative order, drawn as a prefix whose
  // length follows the canvas budget, the zoom and the hidden categories. The order needs the
  // server's subset, asked for as soon as the cell count is known.
  const total = n ?? 0;
  const nObs = meta.data?.n_obs ?? 0;
  const reorder = !p.allCells && nObs > REORDER_ABOVE;
  const sub = useSubset(spec.datasetId, spec.table, reorder ? Math.min(nObs, SUBSET_SIZE) : null, version);
  const drawOrder = useMemo(
    () => (reorder && sub.data && total ? representativeOrder(sub.data.data as Int32Array, total) : null),
    [reorder, sub.data, total],
  );
  const share = useLod((s) => s.visible[spec.id] ?? 1);
  const allowance = useLod((s) => s.allowance[spec.id] ?? Infinity);
  const hiddenShare = useMemo(() => {
    const lg = shown?.legend;
    if (!lg || lg.kind !== 'categories' || !p.hidden.length || !total) return 0;
    const hidden = new Set(p.hidden);
    return lg.labels.reduce((a, l, i) => a + (hidden.has(l) ? lg.counts[i] : 0), 0) / total;
  }, [shown?.legend, p.hidden, total]);
  const count = drawOrder ? drawCount(total, share, allowance, 1 - hiddenShare) : total;
  const shownData = shown?.data;
  const drawData = useMemo(
    () => (shownData && drawOrder ? prefixOf(shownData as PointData, drawOrder, count) : shownData),
    [shownData, drawOrder, count],
  );
  // Fewer points look lighter: grow them a little (at most 1.5×) to keep the density similar.
  const sizeBoost = count < total ? Math.min(1.5, (total / count) ** 0.25) : 1;

  const layers = useMemo(
    () =>
      shown
        ? [
            new ScatterplotLayer({
              id: `${spec.id}:points`,
              data: drawData,
              getFillColor: [...BASE_RGB, 255],
              getRadius: 1,
              radiusScale: p.pointSize * sizeBoost,
              radiusUnits: 'pixels',
              opacity: p.opacity,
              stroked: false,
              pickable: true,
              extensions: [VALUE_COLOUR],
              valueColour: shown.valueColour,
              // Only this layer uses depth (for "higher values on top"); every other layer
              // draws without it (the canvas default), so nothing drawn later is hidden.
              parameters: shown.valueColour.sortByValue
                ? { depthCompare: 'less-equal', depthWriteEnabled: true }
                : { depthCompare: 'always', depthWriteEnabled: false },
            } as ScatterplotLayerProps & { valueColour: ValueColour }),
          ]
        : [],
    [spec.id, drawData, shown?.valueColour, p.pointSize, p.opacity, sizeBoost], // eslint-disable-line react-hooks/exhaustive-deps
  );

  // Until the order of a large dataset is known nothing is drawn (drawing all of it first would
  // upload and draw every cell once for nothing).
  const waitingForOrder = reorder && !drawOrder && !sub.error;
  const status: ViewOutput['status'] = fatal
    ? 'error'
    : (current || (colourError && shown)) && !waitingForOrder
      ? 'ready'
      : 'loading';
  const t0 = useRef(performance.now());
  const reported = useRef(false);
  useEffect(() => {
    if (status === 'ready' && !reported.current) {
      reported.current = true;
      event('view_ready', performance.now() - t0.current, { type: 'embedding', n: n ?? 0, mode: meta.data?.mode ?? '' });
    }
  }, [status, n, meta.data?.mode]);
  // Time from choosing a colouring to its first draw, for every change (not only the first).
  const colourKey = JSON.stringify(p.colorBy);
  const asked = useRef<{ key: string; at: number } | null>(null);
  if (asked.current?.key !== colourKey) asked.current = { key: colourKey, at: performance.now() };
  useEffect(() => {
    if (current && asked.current && asked.current.at > 0) {
      event('colouring_shown', performance.now() - asked.current.at, {
        kind: p.colorBy?.kind ?? 'none',
        n: n ?? 0,
      });
      asked.current.at = 0;
    }
  }, [current, p.colorBy?.kind, n]);

  const retry = () => {
    for (const q of [obs, labelsQ, genes, geneQ]) if (q.error) void q.refetch();
  };

  return {
    layers: waitingForOrder ? [] : layers,
    bounds,
    status,
    error: fatal ? String((fatal as Error).message) : undefined,
    notice,
    problem: colourError
      ? { message: `Colouring failed: ${(colourError as Error).message}`, retry }
      : undefined,
    tooltip: shown ? (drawOrder ? (i: number) => shown.tooltip(drawOrder[i]) : shown.tooltip) : () => null,
    caption: drawOrder && count < total ? `${formatCount(count)} of ${formatCount(total)} cells` : undefined,
    n: n ?? 0,
    total,
    legend: shown?.legend,
  };
}

interface PointData {
  length: number;
  attributes: Record<string, { value: Float32Array; size: number }>;
}

// Copies in the representative order per (source array, order): stable objects, so the GPU
// upload happens once per colouring; changing how many are drawn uploads nothing.
const reordered = new WeakMap<Float32Array, WeakMap<Int32Array, { value: Float32Array; size: number }>>();

function inOrder(attr: { value: Float32Array; size: number }, order: Int32Array) {
  let byOrder = reordered.get(attr.value);
  if (!byOrder) reordered.set(attr.value, (byOrder = new WeakMap()));
  let out = byOrder.get(order);
  if (!out) {
    const { value, size } = attr;
    const v = new Float32Array(order.length * size);
    for (let k = 0; k < order.length; k += 1) for (let d = 0; d < size; d += 1) v[k * size + d] = value[order[k] * size + d];
    out = { value: v, size };
    byOrder.set(order, out);
  }
  return out;
}

/** The first `count` cells of `order`: the same reordered buffers, a shorter length. */
function prefixOf(data: PointData, order: Int32Array, count: number): PointData {
  const attributes: PointData['attributes'] = {};
  for (const [k, a] of Object.entries(data.attributes)) attributes[k] = inOrder(a, order);
  return { length: count, attributes };
}

interface Shown {
  data: object;
  valueColour: ValueColour;
  legend: EmbeddingLegendData | undefined;
  tooltip: (i: number) => string | null;
  n: number;
}

const BASE_COLOURING = constantColouring(BASE_RGB);

function countCodes(codes: ArrayLike<number>, k: number): number[] {
  const counts = new Array<number>(k).fill(0);
  for (let i = 0; i < codes.length; i += 1) {
    const v = codes[i];
    if (v >= 0 && v < k) counts[v] += 1;
  }
  return counts;
}

/** Whole numbers as they are (counts), others with 4 significant digits. */
export function formatValue(v: number): string {
  return Number.isInteger(v) ? String(v) : Number(v).toPrecision(4);
}

function EmbeddingPanel({ spec }: { spec: ViewSpec<EmbeddingParams> }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const p = spec.params;
  const readable = meta.data?.matrices.filter((x) => x.access !== 'needs_conversion') ?? [];
  const genes = useGenes(spec.datasetId, spec.table, meta.data?.version, readable.length > 0);
  const textColumn =
    p.colorBy?.kind === 'obs' && meta.data?.obs.find((c) => c.name === p.colorBy!.name && c.kind === 'string');
  const textLabels = useObsLabels(spec.datasetId, spec.table, textColumn ? textColumn.name : null, meta.data?.version);
  if (!meta.data) return <p className="muted">{meta.error ? String(meta.error.message) : 'Loading…'}</p>;
  const m = meta.data;
  const geneChoice = p.colorBy?.kind === 'gene' ? p.colorBy : null;
  const geneIdx = geneChoice && genes.data ? genes.data.names.indexOf(geneChoice.name) : -1;

  const emb = m.embeddings.find((e) => e.key === p.embedding);
  const dimOptions = Array.from({ length: emb?.dims ?? 2 }, (_, i) => ({ value: String(i), label: `${i + 1}` }));
  const colourGroups: OptionGroup[] = [
    {
      label: 'Categories',
      options: m.obs
        .filter((c) => colourable(c) && c.kind !== 'numeric')
        .map((c) => ({
          value: `obs:${c.name}`,
          label: c.name,
          hint: c.kind === 'boolean' ? 'yes/no' : c.kind === 'string' ? 'text' : `${c.categories?.length}`,
        })),
    },
    {
      label: 'Numbers',
      options: m.obs.filter((c) => c.kind === 'numeric').map((c) => ({ value: `obs:${c.name}`, label: c.name })),
    },
  ].filter((g) => g.options.length);
  if (geneChoice) {
    // the chosen gene, so the Colour by list can show it
    const label = genes.data && geneIdx >= 0 ? geneLabel(genes.data, geneIdx) : geneChoice.name;
    colourGroups.unshift({ label: 'Gene', options: [{ value: `gene:${geneChoice.name}`, label, hint: 'gene' }] });
  }
  // A new gene keeps the chosen matrix; the first time, X if it can be read, else the first
  // readable layer.
  const defaultMatrix = readable.some((x) => x.name === 'X') ? 'X' : readable[0]?.name;
  const pickGene = (name: string) => {
    const matrix = geneChoice ? (geneChoice.matrix ?? 'X') : defaultMatrix;
    update(spec.id, { colorBy: matrix && matrix !== 'X' ? { kind: 'gene', name, matrix } : { kind: 'gene', name }, hidden: [] });
  };
  const matrixName = geneChoice?.matrix ?? 'X';
  const colorColumn = p.colorBy?.kind === 'obs' ? m.obs.find((c) => c.name === p.colorBy!.name) : undefined;
  const ownColours = colorColumn?.kind === 'string' ? textLabels.data?.colors : colorColumn?.colors;
  const nCategories = colorColumn?.kind === 'string' ? textLabels.data?.categories?.length : colorColumn?.categories?.length;
  const hasOwnColours = !!ownColours && ownColours.length === nCategories;
  const paletteOptions = PALETTES.filter((x) => x !== 'dataset' || hasOwnColours).map((x) => ({
    value: x,
    label: PALETTE_LABELS[x],
    hint: x === 'auto' ? (hasOwnColours ? 'dataset’s own' : 'default') : undefined,
  }));

  return (
    <div className="panel-fields">
      <Field
        label="Embedding"
        help={
          <p>
            The cell coordinates stored with the dataset (in <code>obsm</code>), e.g. a UMAP, t-SNE or PCA. Those with more
            than two dimensions let you choose which two to plot.
          </p>
        }
      >
        <Select
          label="Embedding"
          testId="embedding-select"
          value={p.embedding}
          onChange={(v) => v && update(spec.id, { embedding: v, dims: [0, 1] })}
          groups={[{ label: 'Embeddings', options: m.embeddings.map((e) => ({ value: e.key, label: e.key, hint: `${e.dims}D` })) }]}
        />
      </Field>
      {emb && emb.dims > 2 && (
        <div className="field-row">
          <Field label="x dimension">
            <Select
              label="x dimension"
              value={String(p.dims[0])}
              onChange={(v) => v !== null && Number(v) !== p.dims[1] && update(spec.id, { dims: [Number(v), p.dims[1]] })}
              groups={[{ label: 'dims', options: dimOptions }]}
            />
          </Field>
          <Field label="y dimension">
            <Select
              label="y dimension"
              value={String(p.dims[1])}
              onChange={(v) => v !== null && Number(v) !== p.dims[0] && update(spec.id, { dims: [p.dims[0], Number(v)] })}
              groups={[{ label: 'dims', options: dimOptions }]}
            />
          </Field>
        </div>
      )}
      <Field label="Colour by">
        <Select
          label="Colour by"
          testId="colour-select"
          value={p.colorBy ? `${p.colorBy.kind}:${p.colorBy.name}` : null}
          allowNone="None"
          onChange={(v) => {
            if (v?.startsWith('gene:')) return; // the gene already chosen
            update(spec.id, { colorBy: v ? { kind: 'obs', name: v.slice(4) } : null, hidden: [] });
          }}
          groups={colourGroups}
        />
      </Field>
      <Field
        label="Gene"
        help={
          <>
          <p>
            Colour the cells by a gene’s expression. Type part of a gene symbol or id. Cells with higher values are drawn
            on top; the colour scale ends at the 99th percentile of the cells that express the gene.
          </p>
          {m.matrices.length > 1 && (
            <p>
              Values come from <code>X</code>; once a gene is chosen, “Values from” switches to one of the dataset’s
              layers ({m.matrices.filter((x) => x.name !== 'X').map((x) => x.name).join(', ')}).
            </p>
          )}
          </>
        }
      >
        {readable.length === 0 ? (
          <div data-testid="genes-need-conversion">
            <p className="muted small">
              Gene expression in this dataset needs a one-time conversion first: its matrix is stored cell by cell and
              is too large to load whole.
            </p>
            <Conversion key={spec.datasetId} datasetId={spec.datasetId} compact />
          </div>
        ) : genes.data ? (
          <GeneSearch genes={genes.data} onPick={pickGene} testId="gene-search" />
        ) : (
          <p className="muted small">{genes.error ? String(genes.error.message) : 'Loading genes…'}</p>
        )}
      </Field>
      {geneChoice && m.matrices.length > 1 && (
        <Field
          label="Values from"
          help={
            <p>
              The matrix the expression values are read from: <code>X</code>, or one of the dataset’s layers (e.g. raw
              counts or normalised values). Layers that need conversion first are not listed.
            </p>
          }
        >
          <Select
            label="Values from"
            testId="matrix-select"
            value={matrixName}
            onChange={(v) => v && update(spec.id, { colorBy: v === 'X' ? { kind: 'gene', name: geneChoice.name } : { kind: 'gene', name: geneChoice.name, matrix: v } })}
            groups={[{ label: 'matrices', options: readable.map((x) => ({ value: x.name, label: x.name, hint: x.dtype })) }]}
          />
        </Field>
      )}
      {(colorColumn?.kind === 'numeric' || geneChoice) && (
        <label className="check" data-testid="sort-by-value">
          <input
            type="checkbox"
            checked={p.sortByValue}
            onChange={(e) => update(spec.id, { sortByValue: e.target.checked })}
          />
          Higher values on top
        </label>
      )}
      {(colorColumn?.kind === 'numeric' || geneChoice) && (
        <Field label="Colour map">
          <Select
            label="Colour map"
            value={p.colormap}
            onChange={(v) => v && update(spec.id, { colormap: v })}
            testId="colormap-select"
            groups={[{ label: 'maps', options: COLORMAPS.map((c) => ({ value: c, label: COLORMAP_LABELS[c] })) }]}
          />
        </Field>
      )}
      {colorColumn && colorColumn.kind !== 'numeric' && (
        <Field
          label="Palette"
          help={
            <>
              <p>
                Colours follow the category <em>name</em>: a cell type keeps its colour in every view and dataset that uses
                the same palette.
              </p>
              <p>
                “From the dataset” (the automatic choice when the data brings colours, e.g. from scanpy) uses the
                colours saved with <em>this</em> dataset; choose Default to compare datasets by name.
              </p>
              <p>
                With more categories than the palette has colours, colours in between are added, so no two categories
                share a colour (some look alike; the colour-blind safe palette is then no longer fully safe).
              </p>
              <p>Click a colour in the legend to choose your own; it is remembered for this dataset.</p>
            </>
          }
        >
          <Select
            label="Palette"
            testId="palette-select"
            value={p.palette === 'dataset' && !hasOwnColours ? 'auto' : p.palette}
            onChange={(v) => v && update(spec.id, { palette: v })}
            groups={[{ label: 'palettes', options: paletteOptions }]}
          />
        </Field>
      )}
      <Field label="Point size">
        <Slider
          label="Point size"
          value={p.pointSize}
          min={0.5}
          max={10}
          step={0.1}
          onChange={(v) => update(spec.id, { pointSize: v })}
          format={(v) => `${v.toFixed(1)} px radius`}
        />
      </Field>
      <Field label="Opacity">
        <Slider
          label="Opacity"
          value={p.opacity}
          min={0.05}
          max={1}
          step={0.05}
          onChange={(v) => update(spec.id, { opacity: v })}
          format={(v) => `${Math.round(v * 100)}%`}
        />
      </Field>
      {m.n_obs > REORDER_ABOVE && (
        <label className="check" data-testid="all-cells">
          <input type="checkbox" checked={p.allCells} onChange={(e) => update(spec.id, { allCells: e.target.checked })} />
          Show all cells
          <Help label="About “Show all cells”">
            <p>
              To stay smooth, the canvas draws about {formatCount(CANVAS_BUDGET)} cells on screen at a time, shared by
              the open views. A large dataset then shows representative cells (every category of its main annotation
              included) when you look at all of it, more as you zoom in, and all of them when only a part is in view or
              when you hide other categories. The note next to the title says how many are drawn. Tick this to always
              draw every cell.
            </p>
          </Help>
        </label>
      )}
      <p className="muted small">
        {m.n_obs.toLocaleString()} cells · {m.n_vars.toLocaleString()} genes ·{' '}
        {m.mode === 'memory' ? 'loaded in memory' : m.mode === 'converted' ? 'converted copy' : 'read on demand'}
      </p>
    </div>
  );
}

function ColourBar({ colormap, range }: { colormap: ColormapName; range: [number, number] }) {
  const gradient = useMemo(() => colormapGradient(colormap), [colormap]);
  const fmt = (v: number) =>
    Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0) ? v.toExponential(1) : v.toPrecision(3);
  return (
    <div className="colourbar">
      <div className="colourbar-ramp" style={{ background: gradient }} />
      <div className="colourbar-labels">
        <span>{fmt(range[0])}</span>
        <span>{fmt(range[1])}</span>
      </div>
    </div>
  );
}

function EmbeddingLegend({
  spec,
  output,
  compact,
}: {
  spec: ViewSpec<EmbeddingParams>;
  output: ViewOutput;
  compact: boolean;
}) {
  const update = useWorkspace((s) => s.updateParams);
  const p = spec.params;
  const data = output.legend as EmbeddingLegendData | undefined;
  if (!data) return null;
  const collapsed = p.legendCollapsed ?? compact;
  const title = (
    <button
      className="legend-toggle"
      onClick={() => update(spec.id, { legendCollapsed: !collapsed })}
      aria-expanded={!collapsed}
      title="Collapse or expand the legend"
    >
      {collapsed ? '▸' : '▾'} {data.name}
    </button>
  );
  if (data.kind === 'numeric') {
    return (
      <div className={`legend${collapsed ? ' collapsed' : ''}`} data-testid="legend">
        <div className="legend-title">{title}</div>
        {!collapsed && <ColourBar colormap={data.colormap} range={data.range} />}
      </div>
    );
  }
  const hidden = new Set(p.hidden);
  const toggle = (c: string) =>
    update(spec.id, { hidden: hidden.has(c) ? p.hidden.filter((h) => h !== c) : [...p.hidden, c] });
  const picked = p.customColours[data.name] ?? {};
  const pick = (c: string, hex: string) =>
    update(spec.id, { customColours: { ...p.customColours, [data.name]: { ...picked, [c]: hex } } });
  const resetColours = () => {
    const rest = { ...p.customColours };
    delete rest[data.name];
    update(spec.id, { customColours: rest });
  };
  return (
    <div className={`legend${collapsed ? ' collapsed' : ''}`} data-testid="legend">
      <div className="legend-title">
        {title}
        {!collapsed && data.custom.some(Boolean) && (
          <button className="link" onClick={resetColours} data-testid="reset-colours" title="Back to the palette’s colours">
            reset colours
          </button>
        )}
        {!collapsed && (
          <button className="link" onClick={() => update(spec.id, { hidden: hidden.size ? [] : data.labels })}>
            {hidden.size ? 'show all' : 'hide all'}
          </button>
        )}
      </div>
      {!collapsed && data.extended && (
        <p className="legend-note">More categories than colours: in-between colours added.</p>
      )}
      {!collapsed && (
        <ul className="legend-list">
          {data.order
            .filter((i) => data.counts[i] > 0)
            .map((i) => {
              const c = data.labels[i];
              return (
                <li key={c} className={`legend-row${hidden.has(c) ? ' off' : ''}`}>
                  <label className="swatch-pick" title={`Change the colour of ${c}`}>
                    <input
                      type="color"
                      aria-label={`Colour of ${c}`}
                      value={rgbToHex(data.colours[i])}
                      onChange={(e) => pick(c, e.target.value)}
                    />
                    <span
                      className={`swatch${data.custom[i] ? ' custom' : ''}`}
                      style={{ background: `rgb(${data.colours[i].join(',')})` }}
                    />
                  </label>
                  <button
                    className={`legend-item${hidden.has(c) ? ' off' : ''}`}
                    onClick={() => toggle(c)}
                    aria-pressed={!hidden.has(c)}
                    title={hidden.has(c) ? 'Show' : 'Hide'}
                  >
                    <span className="legend-name">{c}</span>
                    <span className="legend-count">{data.counts[i].toLocaleString()}</span>
                  </button>
                </li>
              );
            })}
        </ul>
      )}
    </div>
  );
}

registerView<EmbeddingParams>({
  type: 'embedding',
  label: 'Embedding',
  create: (meta) => {
    const first = meta.embeddings[0];
    if (!first) return null;
    return {
      embedding: first.key,
      dims: [0, 1],
      colorBy: defaultColourBy(meta),
      pointSize: defaultPointSize(meta.n_obs),
      opacity: 0.85,
      hidden: [],
      palette: 'auto',
      customColours: {},
      colormap: 'viridis',
      sortByValue: true,
      allCells: false,
    };
  },
  normalise: normaliseParams,
  useView: useEmbeddingView,
  Panel: EmbeddingPanel,
  Legend: EmbeddingLegend,
});
