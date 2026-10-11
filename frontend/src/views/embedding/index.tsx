// Embedding view (F11a): cells as points in an obsm embedding (UMAP, PCA, …), coloured by an
// obs column (categories with the dataset's or a default palette; numbers with a colormap).
//
// GPU uploads: positions and colours are separate binary attributes with stable identities, so
// a colour change uploads only colours, and point size and opacity (uniforms) upload nothing.

import { ScatterplotLayer, type ScatterplotLayerProps } from '@deck.gl/layers';
import { useCallback, useEffect, useMemo, useRef } from 'react';

import { useEmbedding, useGenes, useMeta, useObs, useSubset } from '../../api/queries';
import type { ColumnMeta, DatasetMeta, GeneList } from '../../api/types';
import { constantColouring } from '../../colour/colouring';
import { type ValueColour, ValueColourExtension } from '../../colour/gpu';
import { event } from '../../telemetry/telemetry';
import { Field, formatCount, Help, Select, Slider } from '../../ui/controls';
import { useWorkspace, type ViewSpec } from '../../state/workspace';
import {
  BASE_RGB,
  ColourFields,
  ColourLegend,
  type ColourLegendData,
  type ColourParams,
  defaultColourParams,
  effectiveColouring,
  NATURAL,
  normaliseColourParams,
  useColouring,
} from '../colouring';
import { CANVAS_BUDGET, drawCount, REORDER_ABOVE, representativeOrder, SUBSET_SIZE, useLod } from '../lod';
import { type Bounds, MAIN_PANEL, type PanelOutput, panelLayerId, registerView, type ViewOutput } from '../registry';

// Kept here for existing imports (tests, other modules).
export { defaultColumn, formatValue, geneLabel, geneRange } from '../colouring';
export type { ColourBy } from '../colouring';

export interface EmbeddingParams extends ColourParams {
  embedding: string;
  dims: [number, number];
  pointSize: number;
  opacity: number;
  allCells: boolean; // large datasets: always draw every cell (else a subset when zoomed out)
  splitBy: string | null; // a category column: one panel per category (small multiples)
  sameScale: boolean; // split: all panels at one scale (else each fills its panel)
}

const VALUE_COLOUR = new ValueColourExtension();
// At most this many panels when splitting by a column.
export const MAX_PANELS = 64;

/** Columns a view can be split by: categories (with at most MAX_PANELS) and booleans. */
export function splittable(c: ColumnMeta): boolean {
  if (c.kind === 'boolean') return true;
  return c.kind === 'categorical' && !!c.categories && c.categories.length > 1 && c.categories.length <= MAX_PANELS;
}

/** Coordinates whose y grows downwards (image pixels, as scanpy and squidpy store `spatial`). */
export const yDown = (embedding: string) => /spatial/i.test(embedding);

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
    ...normaliseColourParams(r),
    pointSize: num(r.pointSize, 0.5, 10, 2),
    opacity: num(r.opacity, 0.05, 1, 0.85),
    allCells: typeof r.allCells === 'boolean' ? r.allCells : false,
    splitBy: typeof r.splitBy === 'string' && r.splitBy ? r.splitBy : null,
    sameScale: typeof r.sameScale === 'boolean' ? r.sameScale : true,
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
  const colouring = effectiveColouring(out, meta, genes);
  out = colouring.p;
  notes.push(...colouring.notes);
  if (p.splitBy) {
    const c = meta.obs.find((x) => x.name === p.splitBy);
    if (!c || !splittable(c)) {
      notes.push(c ? `“${p.splitBy}” has too many categories to split by` : `column “${p.splitBy}” not found`);
      out = { ...out, splitBy: null };
    }
  }
  return { p: out, notice: notes.length ? `Saved settings changed: ${notes.join('; ')}.` : undefined };
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
  const colouring = useColouring(spec.datasetId, spec.table, p, n);
  const { coloured, valueColour, legend, tooltip } = colouring;
  const splitCol = (p.splitBy && meta.data?.obs.find((c) => c.name === p.splitBy)) || null;
  const splitQ = useObs(spec.datasetId, spec.table, splitCol ? splitCol.name : null, version);

  // Keep the corrected parameters, so the notice is shown only once.
  useEffect(() => {
    if (notice) update(spec.id, p);
  }, [notice]); // eslint-disable-line react-hooks/exhaustive-deps

  const bounds = useMemo(() => (positions ? computeBounds(positions) : null), [positions]);
  // Stable attribute objects: deck.gl uploads an attribute only when its object changes.
  const posAttr = useMemo(() => (positions ? { value: positions, size: 2 } : null), [positions]);
  const valueAttr = colouring.values;
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

  // What is drawn: the current colouring when ready; while a new one loads (or if it failed),
  // the last one shown, else the points in the base colour. The plot never goes blank.
  const fatal = meta.error ?? emb.error;
  const colourError = colouring.error ?? splitQ.error;
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
  // Panels: one per category of the split column (its cells in the draw order, so a prefix is
  // still a fair sample), or one with every cell.
  const splitValues = splitQ.data && splitQ.data.data.length === total ? splitQ.data : undefined;
  const splitLabels = useMemo(
    () => (splitCol ? (splitCol.kind === 'boolean' ? ['false', 'true'] : splitCol.categories) : null),
    [splitCol],
  );
  const groups = useMemo(
    () => (splitValues && splitLabels && positions ? splitGroups(splitValues.data, splitLabels, drawOrder, positions) : null),
    [splitValues, splitLabels, drawOrder, positions],
  );
  const waitingForSplit = !!splitCol && !groups && !splitQ.error;
  const notHidden = 1 - hiddenShare;
  const count = drawOrder ? drawCount(total, share, allowance, notHidden) : total;
  const shownData = shown?.data;
  const drawData = useMemo(
    () => (shownData && drawOrder ? prefixOf(shownData as PointData, drawOrder, count) : shownData),
    [shownData, drawOrder, count],
  );
  // Fewer points look lighter: grow them a little (at most 1.5×) to keep the density similar.
  const sizeBoost = count < total ? Math.min(1.5, (total / count) ** 0.25) : 1;

  const valueColourShown = shown?.valueColour;
  const pointsLayer = useCallback(
    (panel: string, data: object, boost: number) =>
      new ScatterplotLayer({
        id: panelLayerId(spec.id, panel, 'points'),
        data,
        getFillColor: [...BASE_RGB, 255],
        getRadius: 1,
        radiusScale: p.pointSize * boost,
        radiusUnits: 'pixels',
        opacity: p.opacity,
        stroked: false,
        pickable: true,
        extensions: [VALUE_COLOUR],
        valueColour: valueColourShown,
        // Only this layer uses depth (for "higher values on top"); every other layer
        // draws without it (the canvas default), so nothing drawn later is hidden.
        parameters: valueColourShown?.sortByValue
          ? { depthCompare: 'less-equal', depthWriteEnabled: true }
          : { depthCompare: 'always', depthWriteEnabled: false },
      } as ScatterplotLayerProps & { valueColour: ValueColour }),
    [spec.id, p.pointSize, p.opacity, valueColourShown],
  );

  const layers = useMemo(
    () => (drawData ? [pointsLayer(MAIN_PANEL, drawData, sizeBoost)] : []),
    [drawData, sizeBoost, pointsLayer],
  );

  // Split: each panel gets its share of the view's allowance and draws a prefix of its cells.
  // (a key, so zooming without a change in counts keeps the same layers)
  const countsKey =
    groups
      ?.map((g) =>
        drawOrder && Number.isFinite(allowance)
          ? drawCount(g.cells.length, share, (allowance * g.cells.length) / Math.max(total, 1), notHidden)
          : g.cells.length,
      )
      .join(',') ?? null;
  const panelCounts = useMemo(() => (countsKey === null ? null : countsKey.split(',').map(Number)), [countsKey]);
  const shownTooltip = shown?.tooltip;
  const panels = useMemo<PanelOutput[] | undefined>(() => {
    if (!groups || !panelCounts || !shownData || !shownTooltip) return undefined;
    return groups.map((g, j) => {
      const c = panelCounts[j];
      const boost = c < g.cells.length ? Math.min(1.5, (g.cells.length / c) ** 0.25) : 1;
      return {
        id: g.id,
        title: `${g.label} · ${formatCount(g.cells.length)}`,
        layers: [pointsLayer(g.id, prefixOf(shownData as PointData, g.cells, c), boost)],
        bounds: g.bounds,
        tooltip: (i: number) => shownTooltip(g.cells[i]),
      };
    });
  }, [groups, panelCounts, shownData, shownTooltip, pointsLayer]);
  const drawnSplit = panelCounts?.reduce((a, b) => a + b, 0) ?? 0;

  // Until the order of a large dataset is known nothing is drawn (drawing all of it first would
  // upload and draw every cell once for nothing).
  const waitingForOrder = reorder && !drawOrder && !sub.error;
  const status: ViewOutput['status'] = fatal
    ? 'error'
    : (current || (colourError && shown)) && !waitingForOrder && !waitingForSplit
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
    colouring.retry();
    if (splitQ.error) void splitQ.refetch();
  };

  const split = !!panels && !waitingForOrder;
  const drawnNow = split ? drawnSplit : count;
  return {
    layers: waitingForOrder || waitingForSplit || split ? [] : layers,
    bounds,
    panels: split ? panels : undefined,
    sameScale: p.sameScale,
    flipY: yDown(p.embedding),
    status,
    error: fatal ? String((fatal as Error).message) : undefined,
    notice,
    problem: colourError
      ? { message: `Colouring failed: ${(colourError as Error).message}`, retry }
      : undefined,
    tooltip: shown ? (drawOrder ? (i: number) => shown.tooltip(drawOrder[i]) : shown.tooltip) : () => null,
    caption: drawOrder && drawnNow < total ? `${formatCount(drawnNow)} of ${formatCount(total)} cells` : undefined,
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

interface Group {
  id: string;
  label: string;
  cells: Int32Array; // in draw order
  bounds: Bounds | null;
}

/** Cells per category of `codes` (in `order` when given), in the labels' natural order; missing
 * codes form an "NA" group. Empty categories get no panel. */
export function splitGroups(
  codes: ArrayLike<number>,
  labels: string[],
  order: Int32Array | null,
  positions: Float32Array,
): Group[] {
  const k = labels.length;
  const counts = new Int32Array(k + 1); // last: missing
  const n = codes.length;
  const code = (i: number) => {
    const v = codes[i];
    return v >= 0 && v < k ? v : k;
  };
  for (let i = 0; i < n; i += 1) counts[code(i)] += 1;
  const cells = Array.from(counts, (c) => new Int32Array(c));
  const fill = new Int32Array(k + 1);
  for (let j = 0; j < n; j += 1) {
    const i = order ? order[j] : j;
    const c = code(i);
    cells[c][fill[c]++] = i;
  }
  const byName = labels.map((_, i) => i).sort((a, b) => NATURAL.compare(labels[a], labels[b]));
  const out: Group[] = [];
  for (const c of [...byName, k]) {
    if (!counts[c]) continue;
    out.push({ id: c === k ? 'na' : `c${c}`, label: c === k ? 'NA' : labels[c], cells: cells[c], bounds: boundsOf(positions, cells[c]) });
  }
  return out;
}

function boundsOf(pos: Float32Array, cells: Int32Array): Bounds | null {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const i of cells) {
    const x = pos[2 * i];
    const y = pos[2 * i + 1];
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    if (x < x0) x0 = x;
    if (x > x1) x1 = x;
    if (y < y0) y0 = y;
    if (y > y1) y1 = y;
  }
  return Number.isFinite(x0) ? [x0, y0, x1, y1] : null;
}

interface Shown {
  data: object;
  valueColour: ValueColour;
  legend: ColourLegendData | undefined;
  tooltip: (i: number) => string | null;
  n: number;
}

const BASE_COLOURING = constantColouring(BASE_RGB);

function EmbeddingPanel({ spec }: { spec: ViewSpec<EmbeddingParams> }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const p = spec.params;
  if (!meta.data) return <p className="muted">{meta.error ? String(meta.error.message) : 'Loading…'}</p>;
  const m = meta.data;
  const emb = m.embeddings.find((e) => e.key === p.embedding);
  const dimOptions = Array.from({ length: emb?.dims ?? 2 }, (_, i) => ({ value: String(i), label: `${i + 1}` }));

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
      <ColourFields spec={spec} />
      {m.obs.some(splittable) && (
        <Field
          label="Split by"
          help={
            <>
              <p>
                One panel per category of a column, e.g. per sample, section or condition (columns with up to{' '}
                {MAX_PANELS} categories). The panels share the colours and the legend, and pan and zoom together at the
                same scale, so sizes and densities compare.
              </p>
              <p>The cells drawn are shared among the panels, as for several views.</p>
            </>
          }
        >
          <Select
            label="Split by"
            testId="split-select"
            value={p.splitBy}
            allowNone="None"
            onChange={(v) => update(spec.id, { splitBy: v })}
            groups={[
              {
                label: 'Columns',
                options: m.obs.filter(splittable).map((c) => ({
                  value: c.name,
                  label: c.name,
                  hint: c.kind === 'boolean' ? 'yes/no' : `${c.categories!.length}`,
                })),
              },
            ]}
          />
        </Field>
      )}
      {p.splitBy && (
        <label className="check" data-testid="same-scale">
          <input type="checkbox" checked={p.sameScale} onChange={(e) => update(spec.id, { sameScale: e.target.checked })} />
          Same scale in all panels
          <Help label="About “Same scale in all panels”">
            <p>
              Ticked, every panel uses one scale, so sizes compare (a small section looks small). Unticked, each panel is
              zoomed to fill its space. Either way, zooming or panning one panel moves the others along.
            </p>
          </Help>
        </label>
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

registerView<EmbeddingParams>({
  type: 'embedding',
  label: 'Embedding',
  create: (meta) => {
    const first = meta.embeddings[0];
    if (!first) return null;
    return {
      embedding: first.key,
      dims: [0, 1],
      ...defaultColourParams(meta),
      pointSize: defaultPointSize(meta.n_obs),
      opacity: 0.85,
      allCells: false,
      splitBy: null,
      sameScale: true,
    };
  },
  normalise: normaliseParams,
  useView: useEmbeddingView,
  Panel: EmbeddingPanel,
  Legend: ColourLegend,
});
