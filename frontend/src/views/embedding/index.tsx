// Embedding view (F11a): cells as points in an obsm embedding (UMAP, PCA, …), coloured by an
// obs column (categories with the dataset's or a default palette; numbers with a colormap).
//
// GPU uploads: positions and colours are separate binary attributes with stable identities, so
// a colour change uploads only colours, and point size and opacity (uniforms) upload nothing.

import { ScatterplotLayer, type ScatterplotLayerProps } from '@deck.gl/layers';
import { useEffect, useMemo, useRef } from 'react';

import { useEmbedding, useGene, useGenes, useMeta, useObs, useObsLabels } from '../../api/queries';
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
  coloursRepeat,
  PALETTE_LABELS,
  type PaletteName,
  PALETTES,
  paletteSize,
  resolvePalette,
  type RGB,
} from '../../colour/palettes';
import { assignSlots } from '../../colour/slots';
import { event } from '../../telemetry/telemetry';
import { Field, type OptionGroup, Select, Slider } from '../../ui/controls';
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
  colormap: ColormapName; // numbers
  legendCollapsed?: boolean;
  [key: string]: unknown;
}

const BASE_RGB: RGB = [80, 110, 160];
// Legend order: 2 before 10, "cluster 9" before "cluster 10" (codes keep the stored order).
const NATURAL = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
// Columns with more categories than this are listed but not chosen by default.
const DEFAULT_MAX_CATEGORIES = 60;
const VALUE_COLOUR = new ValueColourExtension();

function colourable(c: ColumnMeta): boolean {
  if (c.kind === 'numeric' || c.kind === 'boolean' || c.kind === 'string') return true;
  return c.kind === 'categorical' && c.categories !== null;
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
      repeats: boolean;
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
      out = { attr: { value: gpuValues(gene.values, true), size: 1 }, range: geneRange(gene) };
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

  // Category colours: by name (slots shared by all views), or the dataset's own.
  const given = column?.kind === 'categorical' ? column.colors : null;
  const palette = labels ? resolvePalette(p.palette, labels.length, given) : null;
  const slotTable = useWorkspace((s) => s.labelSlots);
  const addLabelSlots = useWorkspace((s) => s.addLabelSlots);
  const known = useMemo(() => new Map(slotTable), [slotTable]);
  const assigned = useMemo(
    () =>
      labels && order && palette && palette !== 'dataset'
        ? assignSlots(labels, order, known, paletteSize(palette))
        : null,
    [labels, order, palette, known],
  );
  useEffect(() => {
    if (assigned?.added.length) addLabelSlots(assigned.added);
  }, [assigned, addLabelSlots]);
  const colours = useMemo(
    () => (labels && palette ? categoryColours(palette, assigned?.slots ?? [], given) : null),
    [labels, palette, assigned, given],
  );

  const hiddenKey = p.hidden.join('\u0000');
  const coloured = !!column || geneIndex !== null;
  const valueColour = useMemo<ValueColour | null>(() => {
    if (!coloured) return constantColouring(BASE_RGB);
    if (!prepared) return null;
    if (prepared.range) return { ...numberColouring(p.colormap, prepared.range), sortByValue: true };
    if (!labels || !colours) return null;
    const hidden = new Set(hiddenKey ? hiddenKey.split('\u0000') : []);
    return categoryColouring(colours, labels.map((l) => hidden.has(l)));
  }, [coloured, prepared, labels, colours, p.colormap, hiddenKey]);

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

  const layers = useMemo(
    () =>
      data && valueColour
        ? [
            new ScatterplotLayer({
              id: `${spec.id}:points`,
              data,
              getFillColor: [...BASE_RGB, 255],
              getRadius: 1,
              radiusScale: p.pointSize,
              radiusUnits: 'pixels',
              opacity: p.opacity,
              stroked: false,
              pickable: true,
              extensions: [VALUE_COLOUR],
              valueColour,
            } as ScatterplotLayerProps & { valueColour: ValueColour }),
          ]
        : [],
    [spec.id, data, valueColour, p.pointSize, p.opacity],
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
    const counts = new Array<number>(labels.length).fill(0);
    for (const v of values.data) if (v >= 0 && v < counts.length) counts[v] += 1;
    return {
      kind: 'categories',
      name: column.name,
      labels,
      colours,
      counts,
      order,
      repeats: coloursRepeat(palette, labels.length),
    };
  }, [gene, colourName, column, values, labels, colours, order, palette, prepared, p.colormap]);

  const error = meta.error ?? emb.error ?? obs.error ?? labelsQ.error ?? genes.error ?? geneQ.error;
  const drawable = !!positions && !!valueColour && (!coloured || !!valueAttr);
  const status: ViewOutput['status'] = error ? 'error' : drawable ? 'ready' : 'loading';
  const t0 = useRef(performance.now());
  const reported = useRef(false);
  useEffect(() => {
    if (status === 'ready' && !reported.current) {
      reported.current = true;
      event('view_ready', performance.now() - t0.current, { type: 'embedding', n: n ?? 0, mode: meta.data?.mode ?? '' });
    }
  }, [status, n, meta.data?.mode]);

  const hiddenSet = useMemo(() => new Set(p.hidden), [p.hidden]);
  const tooltip = (i: number): string | null => {
    if (gene && colourName) {
      const v = gene.values[i];
      return `${colourName}: ${Number.isFinite(v) ? Number(v).toPrecision(4) : 'NA'}`;
    }
    if (!column || !values) return null;
    const v = values.data[i];
    if (values.kind === 'numeric') return `${column.name}: ${Number.isFinite(v) ? Number(v).toPrecision(4) : 'NA'}`;
    const label = v >= 0 ? labels?.[v] : undefined;
    if (label !== undefined && hiddenSet.has(label)) return null; // hidden cells are not reported
    return `${column.name}: ${label ?? 'NA'}`;
  };

  return {
    layers: drawable ? layers : [],
    bounds,
    status,
    error: error ? String((error as Error).message) : undefined,
    notice,
    tooltip,
    n: n ?? 0,
    legend,
  };
}

function EmbeddingPanel({ spec }: { spec: ViewSpec<EmbeddingParams> }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const p = spec.params;
  const readable = meta.data?.matrices.filter((x) => x.access !== 'needs_conversion') ?? [];
  const genes = useGenes(spec.datasetId, spec.table, meta.data?.version, readable.length > 0);
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
  const pickGene = (name: string) =>
    update(spec.id, { colorBy: { kind: 'gene', name, ...(geneChoice?.matrix ? { matrix: geneChoice.matrix } : {}) }, hidden: [] });
  const matrixName = geneChoice?.matrix ?? 'X';
  const colorColumn = p.colorBy?.kind === 'obs' ? m.obs.find((c) => c.name === p.colorBy!.name) : undefined;
  const hasOwnColours = !!colorColumn?.colors && colorColumn.colors.length === colorColumn.categories?.length;
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
          <p>
            Colour the cells by a gene’s expression. Type part of a gene symbol or id. Cells with higher values are drawn
            on top; the colour scale ends at the 99th percentile of the cells that express the gene.
          </p>
        }
      >
        {readable.length === 0 ? (
          <p className="muted small" data-testid="genes-need-conversion">
            Gene expression in this dataset needs a one-time conversion first (its matrix is stored cell by cell and
            is too large to load whole).
          </p>
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
              <p>“From the dataset” uses the colours saved with the data (e.g. by scanpy), when there is one per category.</p>
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
          format={(v) => `${v.toFixed(1)} px`}
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
      <p className="muted small">
        {m.n_obs.toLocaleString()} cells · {m.n_vars.toLocaleString()} genes ·{' '}
        {m.mode === 'memory' ? 'loaded in memory' : 'read on demand'}
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
  return (
    <div className={`legend${collapsed ? ' collapsed' : ''}`} data-testid="legend">
      <div className="legend-title">
        {title}
        {!collapsed && (
          <button className="link" onClick={() => update(spec.id, { hidden: hidden.size ? [] : data.labels })}>
            {hidden.size ? 'show all' : 'hide all'}
          </button>
        )}
      </div>
      {!collapsed && data.repeats && <p className="legend-note">More categories than colours: some colours repeat.</p>}
      {!collapsed && (
        <ul className="legend-list">
          {data.order
            .filter((i) => data.counts[i] > 0)
            .map((i) => {
              const c = data.labels[i];
              return (
                <li key={c}>
                  <button
                    className={`legend-item${hidden.has(c) ? ' off' : ''}`}
                    onClick={() => toggle(c)}
                    aria-pressed={!hidden.has(c)}
                    title={hidden.has(c) ? 'Show' : 'Hide'}
                  >
                    <span className="swatch" style={{ background: `rgb(${data.colours[i].join(',')})` }} />
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
      colormap: 'viridis',
    };
  },
  normalise: normaliseParams,
  useView: useEmbeddingView,
  Panel: EmbeddingPanel,
  Legend: EmbeddingLegend,
});
