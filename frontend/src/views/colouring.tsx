// Colouring cells by an obs column or a gene, shared by the views that show cells (embedding,
// spatial): the parameters, their checks, the hook that loads values and builds the GPU colour
// table (colour/gpu.ts), the settings fields and the legend. A view keeps these parameters
// under the same names (`ColourParams`), so they are remembered and reset alike.

import { type KeyboardEvent, useCallback, useMemo, useRef, useState } from 'react';

import { useGene, useGenes, useMeta, useObs, useObsLabels } from '../api/queries';
import type { ColumnMeta, DatasetMeta, GeneList, GeneVector } from '../api/types';
import {
  categoryColouring,
  constantColouring,
  displayRange,
  numberColouring,
  robustRange,
} from '../colour/colouring';
import { gpuValues, type ValueColour } from '../colour/gpu';
import { isMissing } from '../colour/missing';
import {
  categoryColours,
  COLORMAP_LABELS,
  colormapGradient,
  type ColormapName,
  COLORMAPS,
  coloursExtended,
  hexToRgb,
  normaliseHex,
  PALETTE_LABELS,
  type PaletteName,
  PALETTES,
  paletteSize,
  resolvePalette,
  type RGB,
  rgbToHex,
} from '../colour/palettes';
import { assignSlots } from '../colour/slots';
import { useWorkspace, type ViewSpec } from '../state/workspace';
import { event } from '../telemetry/telemetry';
import { Conversion } from '../ui/Conversion';
import { ColourInput, Field, type OptionGroup, Select } from '../ui/controls';
import { GeneSearch } from '../ui/GeneSearch';
import type { ViewOutput } from './registry';

/** What the points are coloured by: an obs column, or a gene (its var index name) read from
 * `matrix` (X or a layer; X when absent). */
export interface ColourBy {
  kind: 'obs' | 'gene';
  name: string;
  matrix?: string;
}

export interface ColourParams {
  colorBy: ColourBy | null;
  hidden: string[]; // hidden category names of the current colorBy column
  palette: PaletteName; // categories
  // Colours chosen by hand: column name → category name → "#rrggbb" (over the palette).
  customColours: Record<string, Record<string, string>>;
  colormap: ColormapName; // numbers
  sortByValue: boolean; // numbers: draw higher values on top
  legendCollapsed?: boolean;
  [key: string]: unknown;
}

/** The colouring parameters alone (without the index signature of view parameters). */
export type ColourSettings = Pick<
  ColourParams,
  'colorBy' | 'hidden' | 'palette' | 'customColours' | 'colormap' | 'sortByValue'
>;

export const BASE_RGB: RGB = [80, 110, 160];
// Legend order: 2 before 10, "cluster 9" before "cluster 10" (codes keep the stored order).
export const NATURAL = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
// Columns with more categories than this are listed but not chosen by default.
const DEFAULT_MAX_CATEGORIES = 60;
// Columns with more categories cannot be told apart by colour (and their legend would be huge).
const MAX_COLOUR_CATEGORIES = 5_000;

export function colourable(c: ColumnMeta): boolean {
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

export function defaultColourBy(meta: DatasetMeta): ColourBy | null {
  const name = defaultColumn(meta);
  return name ? { kind: 'obs', name } : null;
}

/** `colorBy` from saved settings: a column name (older builds) or `{kind, name}`. */
export function normaliseColourBy(v: unknown): ColourBy | null {
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
export function normaliseCustom(v: unknown): Record<string, Record<string, string>> {
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

/** The colouring parameters from saved settings, made valid. */
export function normaliseColourParams(r: Record<string, unknown>): ColourSettings {
  return {
    colorBy: normaliseColourBy(r.colorBy),
    hidden: Array.isArray(r.hidden) ? r.hidden.filter((h): h is string => typeof h === 'string') : [],
    palette: PALETTES.includes(r.palette as PaletteName) ? (r.palette as PaletteName) : 'auto',
    customColours: normaliseCustom(r.customColours),
    sortByValue: typeof r.sortByValue === 'boolean' ? r.sortByValue : true,
    colormap: COLORMAPS.includes(r.colormap as ColormapName) ? (r.colormap as ColormapName) : 'viridis',
  };
}

/** Default colouring parameters for a dataset. */
export function defaultColourParams(meta: DatasetMeta): ColourSettings {
  return {
    colorBy: defaultColourBy(meta),
    hidden: [],
    palette: 'auto',
    customColours: {},
    colormap: 'viridis',
    sortByValue: true,
  };
}

/** The colouring this dataset can actually show; notes when a saved choice no longer exists. */
export function effectiveColouring<P extends ColourParams>(p: P, meta: DatasetMeta, genes?: GeneList): { p: P; notes: string[] } {
  const notes: string[] = [];
  let out = p;
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
  return { p: out, notes };
}

export type ColourLegendData =
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

export function countCodes(codes: ArrayLike<number>, k: number): number[] {
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

export interface Colouring {
  /** Whether a column or gene is chosen (else one base colour). */
  coloured: boolean;
  /** One value per cell for the GPU (`getColourValue`), when loaded. */
  values: { value: Float32Array; size: number } | null;
  valueColour: ValueColour | null;
  legend: ColourLegendData | undefined;
  /** Text for cell `i` (null for hidden cells). */
  tooltip: (i: number) => string | null;
  error: unknown;
  retry: () => void;
  /** The colouring's name (column, or gene symbol and matrix). */
  name: string | null;
}

/** Loads what the cells are coloured by and builds the GPU colour table. `n`: the number of
 * cells the values must have (values of another length are ignored while they load). */
export function useColouring(
  datasetId: string,
  table: string | null,
  p: ColourParams,
  n: number | null,
): Colouring {
  const meta = useMeta(datasetId, table);
  const version = meta.data?.version;
  const genes = useGenes(datasetId, table, version, p.colorBy?.kind === 'gene');
  const column = (p.colorBy?.kind === 'obs' && meta.data?.obs.find((c) => c.name === p.colorBy!.name)) || null;
  const obs = useObs(datasetId, table, column ? column.name : null, version);
  const labelsQ = useObsLabels(datasetId, table, column?.kind === 'string' ? column.name : null, version);
  const geneIndexOf = useMemo(() => (genes.data ? new Map(genes.data.names.map((g, i) => [g, i])) : null), [genes.data]);
  const geneChoice = p.colorBy?.kind === 'gene' ? p.colorBy : null;
  const geneIndex = geneChoice && geneIndexOf ? (geneIndexOf.get(geneChoice.name) ?? null) : null;
  const geneQ = useGene(datasetId, table, geneIndex, geneChoice?.matrix ?? 'X', version);
  const gene = geneQ.data && geneQ.data.values.length === n ? geneQ.data : undefined;
  const values = obs.data && obs.data.data.length === n ? obs.data : undefined;
  // What the colour legend and tooltips call the colouring: a column, or a gene (its symbol).
  const colourName = column
    ? column.name
    : geneChoice && geneIndex !== null
      ? geneLabel(genes.data!, geneIndex) + (geneChoice.matrix ? ` (${geneChoice.matrix})` : '')
      : null;

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
  // dataset's own (uns['<col>_colors'], the default whenever it has them; for a text column
  // they come with its labels).
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

  const legend = useMemo<ColourLegendData | undefined>(() => {
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

  const error = obs.error ?? labelsQ.error ?? genes.error ?? geneQ.error;
  const retry = useCallback(() => {
    for (const q of [obs, labelsQ, genes, geneQ]) if (q.error) void q.refetch();
  }, [obs, labelsQ, genes, geneQ]);
  return {
    coloured,
    values: coloured && prepared ? prepared.attr : null,
    valueColour,
    legend,
    tooltip,
    error,
    retry,
    name: colourName,
  };
}

/** The colouring settings: Colour by, Gene, Values from, the colour map or palette. */
export function ColourFields({ spec }: { spec: ViewSpec<ColourParams> }) {
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
    <>
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
    </>
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

/** One Tab stop for a list of rows with two controls each (colour, name): the arrow keys move
 * between rows (up, down) and controls (left, right). */
function useRoving() {
  const [at, setAt] = useState<[number, number]>([0, 1]);
  const ref = useRef<HTMLUListElement>(null);
  const tab = (row: number, col: number) => (row === at[0] && col === at[1] ? 0 : -1);
  const onKeyDown = (e: KeyboardEvent) => {
    const rows = ref.current?.querySelectorAll('li').length ?? 0;
    const moves: Record<string, [number, number]> = { ArrowDown: [1, 0], ArrowUp: [-1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    const m = moves[e.key];
    if (!m || !rows) return;
    e.preventDefault();
    const next: [number, number] = [Math.min(rows - 1, Math.max(0, at[0] + m[0])), Math.min(1, Math.max(0, at[1] + m[1]))];
    setAt(next);
    const li = ref.current?.querySelector(`li[data-row="${next[0]}"]`);
    (li?.querySelectorAll<HTMLElement>('input, button')[next[1]])?.focus();
  };
  return { ref, tab, onKeyDown };
}

export function ColourLegend({
  spec,
  output,
  compact,
}: {
  spec: ViewSpec<ColourParams>;
  output: ViewOutput;
  compact: boolean;
}) {
  const update = useWorkspace((s) => s.updateParams);
  const roving = useRoving();
  const p = spec.params;
  const data = output.legend as ColourLegendData | undefined;
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
        <p className="legend-note">More categories than the palette has colours: in-between colours added (extended palette).</p>
      )}
      {!collapsed && (
        <ul className="legend-list" onKeyDown={(e) => roving.onKeyDown(e)} ref={roving.ref}>
          {data.order
            .filter((i) => data.counts[i] > 0)
            .map((i, row) => {
              const c = data.labels[i];
              return (
                <li key={c} className={`legend-row${hidden.has(c) ? ' off' : ''}`} data-row={row}>
                  <ColourInput
                    label={`Colour of ${c}`}
                    value={rgbToHex(data.colours[i])}
                    onChange={(hex) => pick(c, hex)}
                    tabIndex={roving.tab(row, 0)}
                    swatchClass={`swatch${data.custom[i] ? ' custom' : ''}`}
                  />
                  <button
                    className={`legend-item${hidden.has(c) ? ' off' : ''}`}
                    onClick={() => toggle(c)}
                    aria-pressed={!hidden.has(c)}
                    title={hidden.has(c) ? 'Show' : 'Hide'}
                    tabIndex={roving.tab(row, 1)}
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

