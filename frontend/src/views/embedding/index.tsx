// Embedding view (F11a): cells as points in an obsm embedding (UMAP, PCA, …), coloured by an
// obs column (categories with the dataset's or a default palette; numbers with a colormap).
//
// GPU uploads: positions and colours are separate binary attributes with stable identities, so
// a colour change uploads only colours, and point size and opacity (uniforms) upload nothing.

import { ScatterplotLayer } from '@deck.gl/layers';
import { useEffect, useMemo, useRef, useState } from 'react';

import { useEmbedding, useMeta, useObs, useObsLabels } from '../../api/queries';
import type { ColumnMeta, DatasetMeta } from '../../api/types';
import { computeColoursAsync } from '../../colour/client';
import type { ColourSpec } from '../../colour/compute';
import { categoryColors, colormapLut, type ColormapName, coloursRepeat, type RGB } from '../../colour/palettes';
import { event } from '../../telemetry/telemetry';
import { Field, type OptionGroup, Select, Slider } from '../../ui/controls';
import { useWorkspace, type ViewSpec } from '../../state/workspace';
import { type Bounds, registerView, type ViewOutput } from '../registry';

export interface EmbeddingParams {
  embedding: string;
  dims: [number, number];
  colorBy: string | null;
  pointSize: number;
  opacity: number;
  hidden: string[]; // hidden category names of the current colorBy column
  colormap: ColormapName;
  legendCollapsed?: boolean;
  [key: string]: unknown;
}

const BASE_RGB: RGB = [80, 110, 160];
// Legend order: 2 before 10, "cluster 9" before "cluster 10" (codes keep the stored order).
const NATURAL = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
// Columns with more categories than this are listed but not chosen by default.
const DEFAULT_MAX_CATEGORIES = 60;
const COLORMAPS: ColormapName[] = ['viridis', 'magma'];

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

/** Smaller points for more cells: they stay distinct, and drawing cost grows with point area. */
export function defaultPointSize(n: number): number {
  return n < 20_000 ? 3 : n < 200_000 ? 2 : n < 1_000_000 ? 1.2 : 0.8;
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
    colorBy: typeof r.colorBy === 'string' ? r.colorBy : null,
    pointSize: num(r.pointSize, 0.5, 10, 2),
    opacity: num(r.opacity, 0.05, 1, 0.85),
    hidden: Array.isArray(r.hidden) ? r.hidden.filter((h): h is string => typeof h === 'string') : [],
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
  if (p.colorBy && !meta.obs.some((c) => c.name === p.colorBy)) {
    notes.push(`column “${p.colorBy}” not found`);
    out = { ...out, colorBy: null, hidden: [] };
  }
  return { p: out, notice: notes.length ? `Saved settings changed: ${notes.join('; ')}.` : undefined };
}

function useEmbeddingView(spec: ViewSpec<EmbeddingParams>): ViewOutput {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const { p, notice } = useMemo(() => effectiveParams(spec.params, meta.data), [spec.params, meta.data]);
  const version = meta.data?.version;
  const emb = useEmbedding(spec.datasetId, spec.table, p.embedding, p.dims, version);
  const positions = emb.data?.data as Float32Array | undefined;
  const n = positions ? positions.length / 2 : null;
  const column = meta.data?.obs.find((c) => c.name === p.colorBy) ?? null;
  const obs = useObs(spec.datasetId, spec.table, column ? column.name : null, version);
  const labelsQ = useObsLabels(spec.datasetId, spec.table, column?.kind === 'string' ? column.name : null, version);
  const values = obs.data && obs.data.data.length === n ? obs.data : undefined;

  // Keep the corrected parameters, so the notice is shown only once.
  useEffect(() => {
    if (notice) update(spec.id, p);
  }, [notice]); // eslint-disable-line react-hooks/exhaustive-deps

  const labels = useMemo<string[] | null>(() => {
    if (!column || !values || values.kind === 'numeric') return null;
    if (values.kind === 'boolean') return ['false', 'true'];
    if (column.kind === 'string') return labelsQ.data?.categories ?? null;
    return column.categories;
  }, [column, values, labelsQ.data]);
  const palette = useMemo(
    () => (labels ? categoryColors(labels.length, column?.kind === 'categorical' ? column.colors : null) : null),
    [labels, column],
  );
  const hiddenKey = p.hidden.join('\u0000');

  // Colours (in the worker). Opacity is a layer property, so it is not part of this.
  const colourSpec = useMemo<ColourSpec | null>(() => {
    if (n === null) return null;
    if (!column) return { kind: 'constant', n, rgb: BASE_RGB };
    if (!values) return null;
    if (values.kind === 'numeric') {
      return { kind: 'continuous', values: values.data as Float32Array, colormap: p.colormap, range: null };
    }
    if (!labels || !palette) return null;
    const hidden = new Set(hiddenKey ? hiddenKey.split('\u0000') : []);
    return { kind: 'categorical', codes: values.data as Int8Array, colors: palette, hidden: labels.map((c) => hidden.has(c)) };
  }, [n, column, values, labels, palette, p.colormap, hiddenKey]);
  const [colours, setColours] = useState<{ rgba: Uint8Array; range: [number, number] | null } | null>(null);
  useEffect(() => {
    if (!colourSpec) return;
    let live = true;
    computeColoursAsync(colourSpec).then((r) => live && setColours(r));
    return () => {
      live = false;
    };
  }, [colourSpec]);

  const bounds = useMemo(() => (positions ? computeBounds(positions) : null), [positions]);
  // Stable attribute objects: deck.gl uploads an attribute only when its object changes.
  const posAttr = useMemo(() => (positions ? { value: positions, size: 2 } : null), [positions]);
  const rgba = colours && n !== null && colours.rgba.length === n * 4 ? colours.rgba : null;
  const colAttr = useMemo(() => (rgba ? { value: rgba, size: 4, normalized: true } : null), [rgba]);
  const data = useMemo(
    () =>
      posAttr && n !== null
        ? { length: n, attributes: { getPosition: posAttr, ...(colAttr ? { getFillColor: colAttr } : {}) } }
        : null,
    [posAttr, colAttr, n],
  );

  const layers = useMemo(
    () =>
      data
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
            }),
          ]
        : [],
    [spec.id, data, p.pointSize, p.opacity],
  );

  const legend = useMemo<EmbeddingLegendData | undefined>(() => {
    if (!column || !values) return undefined;
    if (values.kind === 'numeric') {
      return colours?.range ? { kind: 'numeric', name: column.name, range: colours.range, colormap: p.colormap } : undefined;
    }
    if (!labels || !palette) return undefined;
    const counts = new Array<number>(labels.length).fill(0);
    for (const v of values.data) if (v >= 0 && v < counts.length) counts[v] += 1;
    const order = labels.map((_, i) => i).sort((a, b) => NATURAL.compare(labels[a], labels[b]));
    const given = column.kind === 'categorical' ? column.colors : null;
    return {
      kind: 'categories',
      name: column.name,
      labels,
      colours: palette,
      counts,
      order,
      repeats: coloursRepeat(labels.length, given),
    };
  }, [column, values, labels, palette, colours?.range, p.colormap]);

  const error = meta.error ?? emb.error ?? obs.error ?? labelsQ.error;
  const status: ViewOutput['status'] = error ? 'error' : positions && rgba ? 'ready' : 'loading';
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
    if (!column || !values) return null;
    const v = values.data[i];
    if (values.kind === 'numeric') return `${column.name}: ${Number.isFinite(v) ? Number(v).toPrecision(4) : 'NA'}`;
    const label = v >= 0 ? labels?.[v] : undefined;
    if (label !== undefined && hiddenSet.has(label)) return null; // hidden cells are not reported
    return `${column.name}: ${label ?? 'NA'}`;
  };

  return {
    layers,
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
  if (!meta.data) return <p className="muted">{meta.error ? String(meta.error.message) : 'Loading…'}</p>;
  const m = meta.data;
  const emb = m.embeddings.find((e) => e.key === p.embedding);
  const dimOptions = Array.from({ length: emb?.dims ?? 2 }, (_, i) => ({ value: String(i), label: `${i + 1}` }));
  const colourGroups: OptionGroup[] = [
    {
      label: 'Categories',
      options: m.obs
        .filter((c) => colourable(c) && c.kind !== 'numeric')
        .map((c) => ({
          value: c.name,
          label: c.name,
          hint: c.kind === 'boolean' ? 'yes/no' : c.kind === 'string' ? 'text' : `${c.categories?.length}`,
        })),
    },
    { label: 'Numbers', options: m.obs.filter((c) => c.kind === 'numeric').map((c) => ({ value: c.name, label: c.name })) },
  ].filter((g) => g.options.length);
  const colorColumn = m.obs.find((c) => c.name === p.colorBy);

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
          value={p.colorBy}
          allowNone="None"
          onChange={(v) => update(spec.id, { colorBy: v, hidden: [] })}
          groups={colourGroups}
        />
      </Field>
      {colorColumn?.kind === 'numeric' && (
        <Field label="Colour map">
          <Select
            label="Colour map"
            value={p.colormap}
            onChange={(v) => v && update(spec.id, { colormap: v })}
            groups={[{ label: 'maps', options: COLORMAPS.map((c) => ({ value: c, label: c })) }]}
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
  const lut = useMemo(() => colormapLut(colormap), [colormap]);
  const stops = Array.from({ length: 11 }, (_, i) => {
    const k = Math.round((i / 10) * 255) * 3;
    return `rgb(${lut[k]},${lut[k + 1]},${lut[k + 2]}) ${i * 10}%`;
  });
  const fmt = (v: number) =>
    Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0) ? v.toExponential(1) : v.toPrecision(3);
  return (
    <div className="colourbar">
      <div className="colourbar-ramp" style={{ background: `linear-gradient(to right, ${stops.join(',')})` }} />
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
      colorBy: defaultColumn(meta),
      pointSize: defaultPointSize(meta.n_obs),
      opacity: 0.85,
      hidden: [],
      colormap: 'viridis',
    };
  },
  normalise: normaliseParams,
  useView: useEmbeddingView,
  Panel: EmbeddingPanel,
  Legend: EmbeddingLegend,
});
