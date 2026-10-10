// Embedding view (F11a): cells as points in an obsm embedding (UMAP, PCA, …), coloured by an
// obs column (categories with the dataset's or a default palette; numbers with a colormap).

import { ScatterplotLayer } from '@deck.gl/layers';
import { useEffect, useMemo, useRef, useState } from 'react';

import { useEmbedding, useMeta, useObs } from '../../api/queries';
import type { ColumnMeta, DatasetMeta } from '../../api/types';
import { computeColoursAsync } from '../../colour/client';
import { type ColourSpec, robustRange } from '../../colour/compute';
import { categoryColors, colormapLut, type ColormapName } from '../../colour/palettes';
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

const BASE_RGB: [number, number, number] = [80, 110, 160];
// Legend order: 2 before 10, "cluster 9" before "cluster 10" (codes keep the stored order).
const NATURAL = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
// Columns with more categories than this are listed but not chosen by default.
const DEFAULT_MAX_CATEGORIES = 60;

function colourable(c: ColumnMeta): boolean {
  if (c.kind === 'numeric' || c.kind === 'boolean') return true;
  return (c.kind === 'categorical' || c.kind === 'string') && c.categories !== null;
}

// Names that usually hold the annotation people want to see first.
const PREFERRED = /cell.?type|annotation|cluster|leiden|louvain/i;

/** Default colouring: a categorical column, preferring cell-type/cluster names, then columns
 * that come with colours, then any with few enough categories. */
export function defaultColumn(meta: DatasetMeta): string | null {
  const cats = meta.obs.filter(
    (c) => c.kind === 'categorical' && c.categories && c.categories.length > 1 && c.categories.length <= DEFAULT_MAX_CATEGORIES,
  );
  const rank = (c: ColumnMeta) => (PREFERRED.test(c.name) ? 0 : 2) + (c.colors ? 0 : 1);
  return [...cats].sort((a, b) => rank(a) - rank(b))[0]?.name ?? null;
}

function defaultPointSize(n: number): number {
  return n < 20_000 ? 3 : n < 200_000 ? 2 : 1.2;
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

function useColours(spec: ViewSpec<EmbeddingParams>, meta: DatasetMeta | undefined, n: number | null) {
  const p = spec.params;
  const column = meta?.obs.find((c) => c.name === p.colorBy) ?? null;
  const obs = useObs(spec.datasetId, spec.table, column ? p.colorBy : null);
  const [result, setResult] = useState<{ rgba: Uint8Array; range: [number, number] | null; key: string } | null>(null);
  const hiddenKey = p.hidden.join('\u0000');

  const colourSpec = useMemo<ColourSpec | null>(() => {
    if (n === null) return null;
    if (!column) return { kind: 'constant', n, rgb: BASE_RGB, opacity: p.opacity };
    const values = obs.data;
    if (!values || values.data.length !== n) return null;
    if (values.kind === 'numeric') {
      return { kind: 'continuous', values: values.data as Float32Array, colormap: p.colormap, range: null, opacity: p.opacity };
    }
    const cats = values.kind === 'boolean' ? ['false', 'true'] : (column.categories ?? []);
    const hidden = new Set(hiddenKey ? hiddenKey.split('\u0000') : []);
    return {
      kind: 'categorical',
      codes: values.data as Int8Array,
      colors: categoryColors(cats.length, column.colors),
      hidden: cats.map((c) => hidden.has(c)),
      opacity: p.opacity,
    };
  }, [n, column, obs.data, p.opacity, p.colormap, hiddenKey]);

  const key = `${p.colorBy}|${p.opacity}|${p.colormap}|${hiddenKey}|${n}`;
  useEffect(() => {
    if (!colourSpec) return;
    let live = true;
    computeColoursAsync(colourSpec).then((r) => live && setResult({ ...r, key }));
    return () => {
      live = false;
    };
  }, [colourSpec, key]);

  // While new colours are computed the previous ones stay on screen (no flash of grey).
  return { column, obs, result };
}

function useEmbeddingView(spec: ViewSpec<EmbeddingParams>): ViewOutput {
  const p = spec.params;
  const meta = useMeta(spec.datasetId, spec.table);
  const emb = useEmbedding(spec.datasetId, spec.table, p.embedding, p.dims);
  const positions = emb.data?.data as Float32Array | undefined;
  const n = positions ? positions.length / 2 : null;
  const { column, obs, result } = useColours(spec, meta.data, n);
  const bounds = useMemo(() => (positions ? computeBounds(positions) : null), [positions]);
  const t0 = useRef(performance.now());
  const reported = useRef(false);

  const layers = useMemo(() => {
    if (!positions || n === null) return [];
    const rgba = result && result.rgba.length === n * 4 ? result.rgba : null;
    return [
      new ScatterplotLayer({
        id: `${spec.id}:points`,
        data: {
          length: n,
          attributes: {
            getPosition: { value: positions, size: 2 },
            ...(rgba ? { getFillColor: { value: rgba, size: 4, normalized: true } } : {}),
          },
        },
        getFillColor: [...BASE_RGB, 160],
        getRadius: p.pointSize,
        radiusUnits: 'pixels',
        stroked: false,
        pickable: true,
        updateTriggers: { getFillColor: rgba },
      }),
    ];
  }, [spec.id, positions, n, result, p.pointSize]);

  const error = meta.error ?? emb.error ?? obs.error;
  const status: ViewOutput['status'] = error ? 'error' : positions && result ? 'ready' : 'loading';

  useEffect(() => {
    if (status === 'ready' && !reported.current) {
      reported.current = true;
      event('view_ready', performance.now() - t0.current, { type: 'embedding', n: n ?? 0, mode: meta.data?.mode ?? '' });
    }
  }, [status, n, meta.data?.mode]);

  const values = obs.data;
  const tooltip = (i: number): string | null => {
    if (!column || !values) return null;
    const v = values.data[i];
    if (values.kind === 'numeric') return `${column.name}: ${Number.isFinite(v) ? Number(v).toPrecision(4) : 'NA'}`;
    if (values.kind === 'boolean') return `${column.name}: ${v ? 'true' : 'false'}`;
    return `${column.name}: ${v >= 0 ? (column.categories?.[v] ?? v) : 'NA'}`;
  };

  return { layers, bounds, status, error: error ? String((error as Error).message) : undefined, tooltip, n: n ?? 0 };
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
        .map((c) => ({ value: c.name, label: c.name, hint: c.kind === 'boolean' ? 'yes/no' : `${c.categories?.length}` })),
    },
    { label: 'Numbers', options: m.obs.filter((c) => c.kind === 'numeric').map((c) => ({ value: c.name, label: c.name })) },
  ].filter((g) => g.options.length);
  const colorColumn = m.obs.find((c) => c.name === p.colorBy);

  return (
    <div className="panel-fields">
      <Field label="Embedding">
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
            groups={[{ label: 'maps', options: [{ value: 'viridis', label: 'viridis' }, { value: 'magma', label: 'magma' }] }]}
          />
        </Field>
      )}
      <Field label="Point size">
        <Slider label="Point size" value={p.pointSize} min={0.5} max={10} step={0.1} onChange={(v) => update(spec.id, { pointSize: v })} format={(v) => `${v.toFixed(1)} px`} />
      </Field>
      <Field label="Opacity">
        <Slider label="Opacity" value={p.opacity} min={0.05} max={1} step={0.05} onChange={(v) => update(spec.id, { opacity: v })} format={(v) => `${Math.round(v * 100)}%`} />
      </Field>
      <p className="muted small">
        {m.n_obs.toLocaleString()} cells · {m.n_vars.toLocaleString()} genes · {m.mode === 'memory' ? 'loaded in memory' : 'read on demand'}
      </p>
    </div>
  );
}

function ColourBar({ colormap, range }: { colormap: ColormapName; range: [number, number] }) {
  const lut = colormapLut(colormap);
  const stops = Array.from({ length: 11 }, (_, i) => {
    const k = Math.round((i / 10) * 255) * 3;
    return `rgb(${lut[k]},${lut[k + 1]},${lut[k + 2]}) ${i * 10}%`;
  });
  const fmt = (v: number) => (Math.abs(v) >= 1000 || (Math.abs(v) < 0.01 && v !== 0) ? v.toExponential(1) : v.toPrecision(3));
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

function EmbeddingLegend({ spec }: { spec: ViewSpec<EmbeddingParams> }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const obs = useObs(spec.datasetId, spec.table, spec.params.colorBy);
  const update = useWorkspace((s) => s.updateParams);
  const p = spec.params;
  const column = meta.data?.obs.find((c) => c.name === p.colorBy);
  const counts = useMemo(() => {
    if (!obs.data || obs.data.kind === 'numeric') return null;
    const c = new Map<number, number>();
    for (const v of obs.data.data) c.set(v, (c.get(v) ?? 0) + 1);
    return c;
  }, [obs.data]);
  if (!column || !obs.data) return null;
  if (obs.data.kind === 'numeric') {
    return <NumericLegend spec={spec} name={column.name} values={obs.data.data as Float32Array} />;
  }
  const cats = obs.data.kind === 'boolean' ? ['false', 'true'] : (column.categories ?? []);
  const colours = categoryColors(cats.length, column.colors);
  const hidden = new Set(p.hidden);
  const toggle = (c: string) =>
    update(spec.id, { hidden: hidden.has(c) ? p.hidden.filter((h) => h !== c) : [...p.hidden, c] });
  const order = cats.map((_, i) => i).sort((a, b) => NATURAL.compare(cats[a], cats[b]));
  return (
    <div className={`legend${p.legendCollapsed ? ' collapsed' : ''}`} data-testid="legend">
      <div className="legend-title">
        <button className="legend-toggle" onClick={() => update(spec.id, { legendCollapsed: !p.legendCollapsed })} title="Collapse or expand">
          {p.legendCollapsed ? '▸' : '▾'} {column.name}
        </button>
        {!p.legendCollapsed && (
          <button className="link" onClick={() => update(spec.id, { hidden: hidden.size ? [] : cats })}>
            {hidden.size ? 'show all' : 'hide all'}
          </button>
        )}
      </div>
      {!p.legendCollapsed && <ul className="legend-list">
        {order.map((i) => [cats[i], i] as const).map(([c, i]) =>
          (counts?.get(i) ?? 0) > 0 ? (
            <li key={c}>
              <button className={`legend-item${hidden.has(c) ? ' off' : ''}`} onClick={() => toggle(c)} title="Show or hide">
                <span className="swatch" style={{ background: `rgb(${colours[i].join(',')})` }} />
                <span className="legend-name">{c}</span>
                <span className="legend-count">{counts?.get(i)?.toLocaleString()}</span>
              </button>
            </li>
          ) : null,
        )}
      </ul>}
    </div>
  );
}

function NumericLegend({ spec, name, values }: { spec: ViewSpec<EmbeddingParams>; name: string; values: Float32Array }) {
  const range = useMemo(() => robustRange(values), [values]); // the colour worker's rule
  return (
    <div className="legend" data-testid="legend">
      <div className="legend-title">
        <span>{name}</span>
      </div>
      <ColourBar colormap={spec.params.colormap} range={range} />
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
  useView: useEmbeddingView,
  Panel: EmbeddingPanel,
  Legend: EmbeddingLegend,
});
