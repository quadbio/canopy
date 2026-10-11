// Spatial view (F11b): a converted SpatialData dataset in one coordinate system: an image (Viv,
// tiled from its zoom levels, per-channel colour and contrast), the cells (outlines filled or
// drawn, coloured like the embedding by the table's columns or genes, on the GPU), and
// transcripts of chosen genes. Everything is read from the store (contract/SPEC.md §5–§8);
// each element is placed with its affine for the chosen coordinate system.
//
// Reading: the image tile by tile (only what is on screen); cell outlines whole up to
// MAX_VERTICES, else cell centres (outlines in view come in S6c); transcripts per gene: their
// range of the gene-sorted store, or the even sample of genes above the store's cap.

import { type Layer } from '@deck.gl/core';
import { PathLayer, ScatterplotLayer, type ScatterplotLayerProps, SolidPolygonLayer } from '@deck.gl/layers';
import { loadOmeZarr, MultiscaleImageLayer } from '@hms-dbmi/viv';
import { Matrix4 } from '@math.gl/core';
import { useQueries, useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo } from 'react';

import { useMeta } from '../../api/queries';
import {
  type Affine,
  type Manifest,
  readArray,
  readAttrs,
  slice,
  storeUrl,
  useManifest,
  useStoreArray,
} from '../../api/store';
import type { DatasetMeta, GeneList } from '../../api/types';
import { MISSING_VALUE, type ValueColour, ValueColourExtension } from '../../colour/gpu';
import { hexToRgb } from '../../colour/palettes';
import { event } from '../../telemetry/telemetry';
import { Field, formatCount, Help, RangeSlider, Select, Slider } from '../../ui/controls';
import { GeneSearch } from '../../ui/GeneSearch';
import { useWorkspace, type ViewSpec } from '../../state/workspace';
import {
  BASE_RGB,
  ColourFields,
  ColourLegend,
  type ColourParams,
  defaultColourParams,
  effectiveColouring,
  normaliseColourParams,
  useColouring,
} from '../colouring';
import { type Bounds, MAIN_PANEL, panelKey, panelLayerId, registerView, type ViewOutput } from '../registry';

export interface ChannelSetting {
  on: boolean;
  colour: string; // "#rrggbb"
  window: [number, number];
}

export interface SpatialParams extends ColourParams {
  system: string | null; // null: the store's first
  image: string | null; // null: the first image; '': none
  channels: Record<string, ChannelSetting[]>; // per image
  cells: string | null; // shapes element; null: the one annotated by this table; '': none
  cellStyle: 'fill' | 'outline';
  cellOpacity: number;
  transcripts: string | null; // points element; null: the first; '': none
  genes: string[]; // transcripts shown
  pointSize: number;
}

// Cell outlines are read whole up to this many vertices (8 bytes each), else cell centres.
export const MAX_VERTICES = 4_000_000;
export const MAX_GENES = 6;
const MAX_CHANNELS = 6; // Viv's limit
// Distinct colours for transcripts of up to MAX_GENES genes (bright on dark images).
export const GENE_COLOURS = ['#ff4d4d', '#4dd2ff', '#ffd24d', '#7dff4d', '#ff4dff', '#ffffff'];
const VALUE_COLOUR = new ValueColourExtension();

/** A 3×3 affine (row-major) as a deck.gl model matrix. */
export function modelMatrix(a: Affine): Matrix4 {
  return new Matrix4([a[0][0], a[1][0], 0, 0, a[0][1], a[1][1], 0, 0, 0, 0, 1, 0, a[0][2], a[1][2], 0, 1]);
}

/** Bounds of `box` (intrinsic x0, y0, x1, y1) after the affine. */
export function transformBox(a: Affine, box: Bounds): Bounds {
  const xs: number[] = [];
  const ys: number[] = [];
  for (const [x, y] of [
    [box[0], box[1]],
    [box[2], box[1]],
    [box[0], box[3]],
    [box[2], box[3]],
  ]) {
    xs.push(a[0][0] * x + a[0][1] * y + a[0][2]);
    ys.push(a[1][0] * x + a[1][1] * y + a[1][2]);
  }
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}

function union(boxes: (Bounds | null)[]): Bounds | null {
  const b = boxes.filter((x): x is Bounds => !!x);
  if (!b.length) return null;
  return [Math.min(...b.map((x) => x[0])), Math.min(...b.map((x) => x[1])), Math.max(...b.map((x) => x[2])), Math.max(...b.map((x) => x[3]))];
}

/** The store's name of a table given by its source name. */
function storeTable(m: Manifest, table: string | null): string | null {
  const names = Object.keys(m.tables);
  return names.find((n) => (m.tables[n].source_name ?? n) === table) ?? names[0] ?? null;
}

/** Choices that are "automatic" (null) resolved against the store; invalid ones dropped. */
export function resolveElements(p: SpatialParams, m: Manifest, table: string | null) {
  const system = p.system && m.coordinate_systems.includes(p.system) ? p.system : (m.coordinate_systems[0] ?? null);
  const placed = (e: { transforms: Record<string, Affine> }) => !!system && !!e.transforms[system];
  const pick = <T extends { transforms: Record<string, Affine> }>(
    els: Record<string, T>,
    chosen: string | null,
    auto: () => string | null,
  ): string | null => {
    if (chosen === '') return null;
    if (chosen && els[chosen] && placed(els[chosen])) return chosen;
    return auto();
  };
  const t = storeTable(m, table);
  const image = pick(m.images, p.image, () => Object.keys(m.images).find((k) => placed(m.images[k])) ?? null);
  const cells = pick(m.shapes, p.cells, () => {
    const names = Object.keys(m.shapes).filter((k) => placed(m.shapes[k]));
    return names.find((k) => m.shapes[k].table === t) ?? null;
  });
  const transcripts = pick(m.points, p.transcripts, () => Object.keys(m.points).find((k) => placed(m.points[k])) ?? null);
  return { system, image, cells, transcripts, table: t };
}

/** Channel settings of an image: saved ones, else from the store (first three on). */
export function channelSettings(p: SpatialParams, m: Manifest, image: string): ChannelSetting[] {
  const img = m.images[image];
  const saved = p.channels[image];
  if (saved && saved.length === img.channels.length) return saved;
  // the first three channels with any signal (an empty channel's window spans one value)
  let shown = 0;
  return img.channels.map((c) => ({
    on: c.window[1] - c.window[0] > 1 && shown++ < 3,
    colour: c.color ?? '#ffffff',
    window: c.window,
  }));
}

function normaliseParams(raw: unknown): SpatialParams | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === 'string' ? v : null);
  const num = (v: unknown, lo: number, hi: number, d: number) =>
    typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d;
  const channels: Record<string, ChannelSetting[]> = {};
  if (r.channels && typeof r.channels === 'object') {
    for (const [k, v] of Object.entries(r.channels as Record<string, unknown>)) {
      if (!Array.isArray(v)) continue;
      const ok = v.filter(
        (c): c is ChannelSetting =>
          !!c &&
          typeof c.on === 'boolean' &&
          typeof c.colour === 'string' &&
          /^#[0-9a-f]{6}$/i.test(c.colour) &&
          Array.isArray(c.window) &&
          c.window.length === 2 &&
          c.window.every((x: unknown) => typeof x === 'number' && Number.isFinite(x)),
      );
      if (ok.length === v.length) channels[k] = ok;
    }
  }
  return {
    ...normaliseColourParams(r),
    system: str(r.system),
    image: str(r.image),
    channels,
    cells: str(r.cells),
    cellStyle: r.cellStyle === 'outline' ? 'outline' : 'fill',
    cellOpacity: num(r.cellOpacity, 0, 1, 0.8),
    transcripts: str(r.transcripts),
    genes: Array.isArray(r.genes) ? r.genes.filter((g): g is string => typeof g === 'string').slice(0, MAX_GENES) : [],
    pointSize: num(r.pointSize, 0.5, 10, 1.5),
    legendCollapsed: typeof r.legendCollapsed === 'boolean' ? r.legendCollapsed : undefined,
  };
}

// ---------------------------------------------------------------- data

interface Polygons {
  starts: Uint32Array;
  vertices: Float32Array;
  row: Int32Array;
  box: Bounds;
}

function useCells(version: string | undefined, path: string | null, load: boolean) {
  return useQuery({
    queryKey: ['store-polygons', version, path],
    queryFn: async (): Promise<Polygons> => {
      const t0 = performance.now();
      const [starts, vertices, row] = await Promise.all([
        readArray(version!, `${path}/starts`),
        readArray(version!, `${path}/vertices`),
        readArray(version!, `${path}/row`),
      ]);
      const v = vertices.data as Float32Array;
      let x0 = Infinity;
      let y0 = Infinity;
      let x1 = -Infinity;
      let y1 = -Infinity;
      for (let i = 0; i < v.length; i += 2) {
        if (v[i] < x0) x0 = v[i];
        if (v[i] > x1) x1 = v[i];
        if (v[i + 1] < y0) y0 = v[i + 1];
        if (v[i + 1] > y1) y1 = v[i + 1];
      }
      event('cells_loaded', performance.now() - t0, { n: (row.data as Int32Array).length, vertices: v.length / 2 });
      return {
        starts: new Uint32Array(starts.data as ArrayLike<number>),
        vertices: v,
        row: row.data as Int32Array,
        box: [x0, y0, x1, y1],
      };
    },
    enabled: !!version && !!path && load,
    staleTime: Infinity,
    gcTime: 60_000,
    retry: false,
  });
}

/** Per-vertex colour values: each vertex takes its polygon's table row's value. */
function vertexValues(p: Polygons, values: Float32Array | null): Float32Array {
  const out = new Float32Array(p.vertices.length / 2);
  const n = p.row.length;
  for (let k = 0; k < n; k += 1) {
    const r = p.row[k];
    const v = values && r >= 0 && r < values.length ? values[r] : MISSING_VALUE;
    out.fill(v, p.starts[k], p.starts[k + 1]);
  }
  return out;
}

function gatherByRow(rows: Int32Array, values: Float32Array | null): Float32Array {
  const out = new Float32Array(rows.length);
  for (let i = 0; i < rows.length; i += 1) {
    const r = rows[i];
    out[i] = values && r >= 0 && r < values.length ? values[r] : MISSING_VALUE;
  }
  return out;
}

function useFeatures(version: string | undefined, path: string | null) {
  return useQuery({
    queryKey: ['store-features', version, path],
    queryFn: async () => {
      const attrs = await readAttrs(version!, path!);
      const names = ((attrs.dbx as { features?: string[] } | undefined)?.features ?? []).map(String);
      const offsets = await readArray(version!, `${path}/offsets`);
      const lod = await readArray(version!, `${path}/lod/offsets`);
      return { names, offsets: offsets.data as ArrayLike<number>, lod: lod.data as ArrayLike<number> };
    },
    enabled: !!version && !!path,
    staleTime: Infinity,
    retry: false,
  });
}

// ---------------------------------------------------------------- the view

function useSpatialView(spec: ViewSpec<SpatialParams>): ViewOutput {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const version = meta.data?.version;
  const manifest = useManifest(version, meta.data?.mode === 'converted');
  const m = manifest.data;
  const { p, notice } = useMemo(() => {
    if (!meta.data) return { p: spec.params, notice: undefined };
    const c = effectiveColouring(spec.params, meta.data);
    return { p: c.p, notice: c.notes.length ? `Saved settings changed: ${c.notes.join('; ')}.` : undefined };
  }, [spec.params, meta.data]);
  useEffect(() => {
    if (notice) update(spec.id, p);
  }, [notice]); // eslint-disable-line react-hooks/exhaustive-deps
  const el = useMemo(() => (m ? resolveElements(p, m, spec.table) : null), [p, m, spec.table]);
  const system = el?.system ?? null;
  const n = meta.data?.n_obs ?? null;
  const colouring = useColouring(spec.datasetId, spec.table, p, n);

  // ---- image
  const imageEntry = m && el?.image ? m.images[el.image] : null;
  const loader = useQuery({
    queryKey: ['viv', version, el?.image],
    queryFn: async () => {
      const t0 = performance.now();
      const r = await loadOmeZarr(`${storeUrl(version!)}/${imageEntry!.path}`, { type: 'multiscales' });
      event('image_opened', performance.now() - t0, { levels: r.data.length });
      return r.data;
    },
    enabled: !!imageEntry && !!version,
    staleTime: Infinity,
    retry: false,
  });
  const channels = m && el?.image ? channelSettings(p, m, el.image) : [];
  const shownChannels = channels.map((c, i) => ({ ...c, i })).filter((c) => c.on).slice(0, MAX_CHANNELS);

  // ---- cells
  const shapes = m && el?.cells ? m.shapes[el.cells] : null;
  const whole = !!shapes && shapes.kind === 'polygons' && (shapes.n_vertices ?? 0) <= MAX_VERTICES;
  const polys = useCells(version, shapes?.path ?? null, whole);
  const centroids = useStoreArray(version, shapes && !whole && shapes.kind === 'polygons' ? `${shapes.path}/centroid` : null);
  const centroidRows = useStoreArray(version, shapes && !whole ? `${shapes.path}/row` : null);
  const circles = useStoreArray(version, shapes?.kind === 'circles' ? `${shapes.path}/center` : null);
  const radius = useStoreArray(version, shapes?.kind === 'circles' ? `${shapes.path}/radius` : null);
  // cells colour only when their table is the view's table
  const colourCells = !!shapes && shapes.table === el?.table;
  const values = colourCells ? (colouring.values?.value ?? null) : null;
  const vertexAttr = useMemo(
    () => (polys.data ? { value: vertexValues(polys.data, values), size: 1 } : null),
    [polys.data, values],
  );
  const pointRows = centroidRows.data?.data as Int32Array | undefined;
  const pointAttr = useMemo(() => (pointRows ? { value: gatherByRow(pointRows, values), size: 1 } : null), [pointRows, values]);

  // ---- transcripts
  const points = m && el?.transcripts ? m.points[el.transcripts] : null;
  const features = useFeatures(version, points?.path ?? null);
  const geneReads = useQueries({
    queries: p.genes.map((g) => ({
      queryKey: ['store-transcripts', version, points?.path, g],
      queryFn: async () => {
        const f = features.data!.names.indexOf(g);
        if (f < 0) return null;
        const t0 = performance.now();
        const lo = Number(features.data!.offsets[f]);
        const hi = Number(features.data!.offsets[f + 1]);
        const sampled = hi - lo > points!.lod_cap;
        const [a, b] = sampled ? [Number(features.data!.lod[f]), Number(features.data!.lod[f + 1])] : [lo, hi];
        const xy = await readArray(version!, `${points!.path}/${sampled ? 'lod/xy' : 'xy'}`, [slice(a, b), null]);
        event('transcripts_loaded', performance.now() - t0, { n: b - a, sampled: sampled ? 1 : 0 });
        return { gene: g, xy: xy.data as Float32Array, n: hi - lo, sampled };
      },
      enabled: !!features.data && !!points,
      staleTime: Infinity,
      gcTime: 30_000,
      retry: false,
    })),
  });

  // ---- layers
  const id = (name: string) => panelLayerId(spec.id, MAIN_PANEL, name);
  const valueColour = colouring.valueColour;
  const layers = useMemo(() => {
    const out: Layer[] = [];
    if (!m || !system) return out;
    if (imageEntry && loader.data && shownChannels.length) {
      out.push(
        new MultiscaleImageLayer({
          id: id('image'),
          loader: loader.data,
          selections: shownChannels.map((c) => ({ c: c.i })),
          colors: shownChannels.map((c) => hexToRgb(c.colour)),
          contrastLimits: shownChannels.map((c) => c.window),
          channelsVisible: shownChannels.map(() => true),
          modelMatrix: modelMatrix(imageEntry.transforms[system]),
          viewportId: panelKey(spec.id, MAIN_PANEL),
          pickable: false,
        } as never) as unknown as Layer,
      );
    }
    if (shapes && shapes.transforms[system]) {
      const mm = modelMatrix(shapes.transforms[system]);
      const vc = (colourCells ? valueColour : null) ?? CELL_BASE;
      if (polys.data && vertexAttr) {
        const data = {
          length: polys.data.row.length,
          startIndices: polys.data.starts,
          attributes: { getPolygon: { value: polys.data.vertices, size: 2 }, getColourValue: vertexAttr },
        };
        if (p.cellStyle === 'fill') {
          out.push(
            new SolidPolygonLayer({
              id: id('cells'),
              data: data as never,
              _normalize: false,
              filled: true,
              getFillColor: [...BASE_RGB, 255],
              opacity: p.cellOpacity,
              pickable: true,
              modelMatrix: mm,
              extensions: [VALUE_COLOUR],
              valueColour: vc,
            } as never),
          );
        } else {
          out.push(
            new PathLayer({
              id: id('cells'),
              data: { ...data, attributes: { getPath: data.attributes.getPolygon, getColourValue: vertexAttr } } as never,
              _pathType: 'open',
              getColor: [...BASE_RGB, 255],
              getWidth: 1.2,
              widthUnits: 'pixels',
              opacity: p.cellOpacity,
              pickable: true,
              modelMatrix: mm,
              extensions: [VALUE_COLOUR],
              valueColour: vc,
            } as never),
          );
        }
      } else if ((centroids.data || circles.data) && pointAttr) {
        const pos = (circles.data ?? centroids.data)!.data as Float32Array;
        out.push(
          new ScatterplotLayer({
            id: id('cells'),
            data: {
              length: pos.length / 2,
              attributes: {
                getPosition: { value: pos, size: 2 },
                getColourValue: pointAttr,
                ...(radius.data ? { getRadius: { value: radius.data.data as Float32Array, size: 1 } } : {}),
              },
            },
            radiusUnits: radius.data ? 'common' : 'pixels',
            getRadius: 2,
            getFillColor: [...BASE_RGB, 255],
            opacity: p.cellOpacity,
            pickable: true,
            modelMatrix: mm,
            extensions: [VALUE_COLOUR],
            valueColour: vc,
          } as ScatterplotLayerProps & { valueColour: ValueColour }),
        );
      }
    }
    if (points && points.transforms[system]) {
      const mm = modelMatrix(points.transforms[system]);
      geneReads.forEach((q, k) => {
        const d = q.data;
        if (!d) return;
        const rgb = hexToRgb(GENE_COLOURS[k % GENE_COLOURS.length]);
        out.push(
          new ScatterplotLayer({
            id: id(`tx-${k}`),
            data: { length: d.xy.length / 2, attributes: { getPosition: { value: d.xy, size: 2 } } },
            getFillColor: [...rgb, 230],
            getRadius: p.pointSize,
            radiusUnits: 'pixels',
            stroked: false,
            pickable: false,
            modelMatrix: mm,
          }),
        );
      });
    }
    return out;
  }, [m, system, imageEntry, loader.data, JSON.stringify(shownChannels), shapes, polys.data, vertexAttr, centroids.data, circles.data, radius.data, pointAttr, p.cellStyle, p.cellOpacity, p.pointSize, colourCells, valueColour, points, geneReads.map((q) => q.dataUpdatedAt).join(',')]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- bounds: everything placed in the system
  const bounds = useMemo<Bounds | null>(() => {
    if (!m || !system) return null;
    const boxes: (Bounds | null)[] = [];
    if (imageEntry) {
      const [, h, w] = imageEntry.levels[0];
      boxes.push(transformBox(imageEntry.transforms[system], [0, 0, w, h]));
    }
    if (shapes && polys.data) boxes.push(transformBox(shapes.transforms[system], polys.data.box));
    if (points?.transforms[system]) boxes.push(transformBox(points.transforms[system], points.bbox));
    return union(boxes);
  }, [m, system, imageEntry, shapes, polys.data, points]);

  const rows = polys.data?.row ?? pointRows;
  const tooltip = useCallback(
    (i: number) => (colourCells && rows && rows[i] >= 0 ? colouring.tooltip(rows[i]) : null),
    [colourCells, rows, colouring],
  );
  const loading = manifest.isLoading || loader.isLoading || polys.isLoading || features.isLoading;
  const fatal = meta.error ?? manifest.error;
  const problem = loader.error ?? polys.error ?? features.error ?? colouring.error ?? geneReads.find((q) => q.error)?.error;
  return {
    layers,
    bounds,
    flipY: true,
    status: fatal ? 'error' : bounds && !loading ? 'ready' : 'loading',
    error: fatal
      ? String((fatal as Error).message)
      : meta.data && meta.data.mode !== 'converted'
        ? 'The spatial view needs the converted copy of this dataset.'
        : undefined,
    notice,
    problem: problem ? { message: `Part of the view failed: ${(problem as Error).message}`, retry: colouring.retry } : undefined,
    tooltip,
    caption: shapes ? `${formatCount(shapes.n)} cells${whole || shapes.kind === 'circles' ? '' : ' (centres)'}` : undefined,
    n: shapes?.n ?? 0,
    legend: colourCells ? colouring.legend : undefined,
  };
}

const CELL_BASE: ValueColour = {
  lut: new Uint8Array([...BASE_RGB, 255]),
  mode: 'categories',
  missing: [...BASE_RGB, 255] as [number, number, number, number],
};

// ---------------------------------------------------------------- settings

function SpatialPanel({ spec }: { spec: ViewSpec<SpatialParams> }) {
  const meta = useMeta(spec.datasetId, spec.table);
  const update = useWorkspace((s) => s.updateParams);
  const manifest = useManifest(meta.data?.version, meta.data?.mode === 'converted');
  const p = spec.params;
  const m = manifest.data;
  const el = m ? resolveElements(p, m, spec.table) : null;
  const points = m && el?.transcripts ? m.points[el.transcripts] : null;
  const features = useFeatures(meta.data?.version, points?.path ?? null);
  const featureList = useMemo<GeneList | null>(
    () => (features.data ? { names: features.data.names, symbols: null, symbol_column: null, duplicates: 0, matrices: [] } as unknown as GeneList : null),
    [features.data],
  );
  if (!meta.data || !m || !el) return <p className="muted">{manifest.error ? String(manifest.error.message) : 'Loading…'}</p>;
  const channels = el.image ? channelSettings(p, m, el.image) : [];
  const img = el.image ? m.images[el.image] : null;
  const setChannel = (i: number, patch: Partial<ChannelSetting>) => {
    if (!el.image) return;
    const next = channels.map((c, k) => (k === i ? { ...c, ...patch } : c));
    update(spec.id, { channels: { ...p.channels, [el.image]: next } });
  };
  const dtypeMax = img ? ({ uint8: 255, uint16: 65535 } as Record<string, number>)[img.dtype] : undefined;
  // "None" is the Select's own empty choice (null), saved as '' (none, not automatic)
  const elementOptions = (els: Record<string, unknown>) =>
    Object.keys(els)
      .filter((k) => !!(els[k] as { transforms: Record<string, Affine> }).transforms[el.system ?? ''])
      .map((k) => ({ value: k, label: k }));
  const shapes = el.cells ? m.shapes[el.cells] : null;
  return (
    <div className="panel-fields">
      {m.coordinate_systems.length > 1 && (
        <Field
          label="Coordinate system"
          help={<p>The frame the elements are placed in, as defined in the dataset (e.g. the microscope’s, or one aligned to another sample).</p>}
        >
          <Select
            label="Coordinate system"
            testId="system-select"
            value={el.system}
            onChange={(v) => v && update(spec.id, { system: v })}
            groups={[{ label: 'systems', options: m.coordinate_systems.map((s) => ({ value: s, label: s })) }]}
          />
        </Field>
      )}
      <Field label="Image">
        <Select
          label="Image"
          testId="image-select"
          value={el.image}
          allowNone="None"
          onChange={(v) => update(spec.id, { image: v ?? '' })}
          groups={[{ label: 'images', options: elementOptions(m.images) }]}
        />
      </Field>
      {img && (
        <div className="channels" data-testid="channels">
          {channels.map((c, i) => (
            <div className="channel" key={img.channels[i].name}>
              <label className="check">
                <input type="checkbox" checked={c.on} onChange={(e) => setChannel(i, { on: e.target.checked })} />
                <span className="swatch-pick" title="Change the colour">
                  <input
                    type="color"
                    aria-label={`Colour of channel ${img.channels[i].name}`}
                    value={c.colour}
                    onChange={(e) => setChannel(i, { colour: e.target.value })}
                  />
                  <span className="swatch" style={{ background: c.colour }} />
                </span>
                {img.channels[i].name}
              </label>
              {c.on && (
                <RangeSlider
                  label={`Contrast of ${img.channels[i].name}`}
                  value={c.window}
                  min={0}
                  max={dtypeMax ?? Math.max(c.window[1] * 2, 1)}
                  step={dtypeMax && dtypeMax > 255 ? 16 : 1}
                  onChange={(w) => setChannel(i, { window: w })}
                  format={(v) => formatCount(v)}
                />
              )}
            </div>
          ))}
          {channels.filter((c) => c.on).length > MAX_CHANNELS && (
            <p className="muted small">At most {MAX_CHANNELS} channels are shown at once.</p>
          )}
        </div>
      )}
      <Field
        label="Cells"
        help={<p>The cell outlines (segmentation). They are coloured by the column or gene chosen below, from the table that describes them.</p>}
      >
        <Select
          label="Cells"
          testId="cells-select"
          value={el.cells}
          allowNone="None"
          onChange={(v) => update(spec.id, { cells: v ?? '' })}
          groups={[{ label: 'cells', options: elementOptions(m.shapes) }]}
        />
      </Field>
      {shapes && (
        <>
          <div className="field-row">
            <Field label="Draw">
              <Select
                label="Draw"
                testId="cell-style"
                value={p.cellStyle}
                onChange={(v) => v && update(spec.id, { cellStyle: v })}
                groups={[{ label: 'style', options: [{ value: 'fill', label: 'Filled' }, { value: 'outline', label: 'Outlines' }] }]}
              />
            </Field>
          </div>
          <Field label="Cell opacity">
            <Slider label="Cell opacity" value={p.cellOpacity} min={0} max={1} step={0.05} onChange={(v) => update(spec.id, { cellOpacity: v })} format={(v) => `${Math.round(v * 100)}%`} />
          </Field>
          {shapes.table === el.table ? (
            <ColourFields spec={spec} />
          ) : (
            <p className="muted small">These cells are described by another table ({shapes.table ?? 'none'}): open that table to colour them.</p>
          )}
        </>
      )}
      <Field
        label="Transcripts"
        help={
          <p>
            Detected molecules of chosen genes (up to {MAX_GENES}), one colour per gene. Genes with very many transcripts show
            an even sample of {formatCount(points?.lod_cap ?? 100_000)} of them.
          </p>
        }
      >
        <Select
          label="Transcripts"
          testId="points-select"
          value={el.transcripts}
          allowNone="None"
          onChange={(v) => update(spec.id, { transcripts: v ?? '' })}
          groups={[{ label: 'points', options: elementOptions(m.points) }]}
        />
      </Field>
      {points && featureList && (
        <>
          {p.genes.length < MAX_GENES && (
            <GeneSearch
              genes={featureList}
              onPick={(g) => !p.genes.includes(g) && update(spec.id, { genes: [...p.genes, g] })}
              testId="transcript-search"
            />
          )}
          <ul className="gene-chips" data-testid="transcript-genes">
            {p.genes.map((g, k) => (
              <li key={g}>
                <span className="swatch" style={{ background: GENE_COLOURS[k % GENE_COLOURS.length] }} />
                {g}
                <button className="icon-button" aria-label={`Remove ${g}`} onClick={() => update(spec.id, { genes: p.genes.filter((x) => x !== g) })}>
                  ×
                </button>
              </li>
            ))}
          </ul>
          <Field label="Transcript size">
            <Slider label="Transcript size" value={p.pointSize} min={0.5} max={6} step={0.25} onChange={(v) => update(spec.id, { pointSize: v })} format={(v) => `${v.toFixed(1)} px`} />
          </Field>
        </>
      )}
      <p className="muted small">
        {formatCount(meta.data.n_obs)} cells in this table · {m.coordinate_systems.length} coordinate system
        {m.coordinate_systems.length > 1 ? 's' : ''}
        <Help label="About the spatial view">
          <p>
            Reads the converted copy: the image tile by tile at the zoom level on screen, the cell outlines, and the
            transcripts of the genes you choose. Units follow the dataset’s coordinate system.
          </p>
        </Help>
      </p>
    </div>
  );
}

registerView<SpatialParams>({
  type: 'spatial',
  label: 'Spatial',
  rank: 0,
  create: (meta: DatasetMeta) => {
    if (meta.kind !== 'spatialdata_zarr' || meta.mode !== 'converted') return null;
    return {
      ...defaultColourParams(meta),
      system: null,
      image: null,
      channels: {},
      cells: null,
      cellStyle: 'fill',
      cellOpacity: 0.8,
      transcripts: null,
      genes: [],
      pointSize: 1.5,
    };
  },
  normalise: normaliseParams,
  useView: useSpatialView,
  Panel: SpatialPanel,
  Legend: ColourLegend as never,
});
