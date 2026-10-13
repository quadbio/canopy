// Spatial view (F11b): a converted SpatialData dataset in one coordinate system: an image (Viv,
// tiled from its zoom levels, per-channel colour and contrast), the cells (outlines filled or
// drawn, coloured like the embedding by the table's columns or genes, on the GPU), and
// transcripts of chosen genes. Everything is read from the store (contract/SPEC.md §5–§8);
// each element is placed with its affine for the chosen coordinate system.
//
// Reading: the image tile by tile (only what is on screen). Cell outlines whole up to
// MAX_VERTICES; for more, cell centres, and the outlines of the blocks in view once at most
// OUTLINE_BUDGET cells are in view. Transcripts per gene: its range of the gene-sorted store; a
// gene above the store's cap shows its even sample, and when zoomed in (its chunks in view hold
// at most IN_VIEW_POINTS) the chunks of its range in view. Chunks are cached by api/store.ts, so
// panning reads only new ones; stale reads are cancelled.
//
// Drawing: each polygon source (all cells, or one block) keeps one data object with its colour
// values while they do not change, so other settings (opacity, sizes, transcripts) never make
// deck.gl cut the polygons into triangles again. Filled and outlined cells are different layer
// types with different ids.

import { type Layer } from '@deck.gl/core';
import { PathLayer, ScatterplotLayer, type ScatterplotLayerProps, SolidPolygonLayer } from '@deck.gl/layers';
import { loadOmeZarrFromStore, MultiscaleImageLayer } from '@hms-dbmi/viv';
import { Matrix4 } from '@math.gl/core';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef } from 'react';

import { useMeta } from '../../api/queries';
import {
  type Affine,
  elementStore,
  type Manifest,
  readAttrs,
  readRows,
  readWhole,
  useManifest,
  useStoreArray,
} from '../../api/store';
import type { DatasetMeta, GeneList } from '../../api/types';
import { hiddenBy } from '../../colour/colouring';
import { MISSING_VALUE, type ValueColour, ValueColourExtension } from '../../colour/gpu';
import { hexToRgb } from '../../colour/palettes';
import { event } from '../../telemetry/telemetry';
import { ColourInput, Field, formatCount, Help, RangeSlider, Select, Slider } from '../../ui/controls';
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
import { PlotCompanionField } from '../plot';
import { useLod } from '../lod';
import { type Bounds, MAIN_PANEL, panelKey, panelLayerId, registerView, type ViewOutput } from '../registry';
import { lassoInto, type Polygon } from '../selection';

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
  geneColours: Record<string, string>; // gene → its colour (kept while shown)
  pointSize: number;
}

function override(name: string): number | null {
  try {
    const v = new URLSearchParams(window.location.search).get(name);
    return v && /^\d+$/.test(v) ? Number(v) : null;
  } catch {
    return null;
  }
}

// Cell outlines are read whole up to this many vertices (8 bytes each), else by blocks in view
// (`?outlines=<n>` in the page address changes it, for tests).
export const MAX_VERTICES = override('outlines') ?? 4_000_000;
// Outlines are drawn when at most this many cells are in view (else their centres;
// `?cellsInView=<n>` changes it).
export const OUTLINE_BUDGET = override('cellsInView') ?? 300_000;
// A gene above the store's sample cap is read in full in view when about this many fit.
export const IN_VIEW_POINTS = 1_500_000;
const POLY_BLOCK = 4_096; // polygons per block (contract)
const POINT_CHUNK = 16_384; // transcript rows per chunk (contract)
export const MAX_GENES = 6;
const MAX_CHANNELS = 6; // Viv's limit
// Distinct colours for transcripts of up to MAX_GENES genes (bright on dark images, and visible
// on the light canvas when no image is shown). A gene keeps its colour while it is shown.
export const GENE_COLOURS = ['#ff4d4d', '#4dd2ff', '#ffd24d', '#7dff4d', '#ff4dff', '#ff9f40'];

/** Each shown gene's colour: the saved one, else the first not taken (in the order added). */
export function geneColours(genes: string[], saved: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  const taken = new Set<string>();
  for (const g of genes) {
    const c = saved[g];
    if (c && GENE_COLOURS.includes(c) && !taken.has(c)) {
      out[g] = c;
      taken.add(c);
    }
  }
  for (const g of genes) {
    if (out[g]) continue;
    const c = GENE_COLOURS.find((x) => !taken.has(x)) ?? GENE_COLOURS[0];
    out[g] = c;
    taken.add(c);
  }
  return out;
}
const VALUE_COLOUR = new ValueColourExtension();

/** The inverse of a 3×3 affine (row-major). */
export function invert(a: Affine): Affine {
  const [[p, q, x], [r, t, y]] = a;
  const det = p * t - q * r || 1e-12;
  const ip = t / det;
  const iq = -q / det;
  const ir = -r / det;
  const it = p / det;
  return [
    [ip, iq, -(ip * x + iq * y)],
    [ir, it, -(ir * x + it * y)],
    [0, 0, 1],
  ];
}

const meets = (a: ArrayLike<number>, k: number, r: Bounds) =>
  a[4 * k] <= r[2] && a[4 * k + 2] >= r[0] && a[4 * k + 1] <= r[3] && a[4 * k + 3] >= r[1];

/** Blocks (or chunks) whose boxes (rows of 4: x0, y0, x1, y1) meet `rect`. */
export function boxesInView(boxes: ArrayLike<number>, rect: Bounds, from = 0, to = boxes.length / 4): number[] {
  const out: number[] = [];
  for (let k = from; k < to; k += 1) if (meets(boxes, k, rect)) out.push(k);
  return out;
}

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
  const out = img.channels.map((c) => ({
    on: c.window[1] - c.window[0] > 1 && shown++ < 3,
    colour: c.color ?? '#ffffff',
    window: c.window,
  }));
  // one channel on its own in pure blue or red is nearly black: grey scale reads better
  if (shown === 1) for (const c of out) if (c.on) c.colour = '#ffffff';
  return out;
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
    geneColours:
      r.geneColours && typeof r.geneColours === 'object'
        ? Object.fromEntries(
            Object.entries(r.geneColours as Record<string, unknown>).filter(
              (e): e is [string, string] => typeof e[1] === 'string' && GENE_COLOURS.includes(e[1]),
            ),
          )
        : {},
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
    queryFn: async ({ signal }): Promise<Polygons> => {
      const t0 = performance.now();
      const [starts, vertices, row] = await Promise.all([
        readWhole(version!, `${path}/starts`, { signal }),
        readWhole(version!, `${path}/vertices`, { signal }),
        readWhole(version!, `${path}/row`, { signal }),
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
    retry: 1,
  });
}

async function readBlock(version: string, path: string, n: number, b: number, signal?: AbortSignal): Promise<Polygons> {
  const s0 = b * POLY_BLOCK;
  const s1 = Math.min(n, s0 + POLY_BLOCK);
  const starts = (await readRows(version, `${path}/starts`, s0, s1 + 1, { signal })).data;
  const v0 = Number(starts[0]);
  const v1 = Number(starts[s1 - s0]);
  const [verts, row] = await Promise.all([
    readRows(version, `${path}/vertices`, v0, v1, { signal }),
    readRows(version, `${path}/row`, s0, s1, { signal }),
  ]);
  const local = new Uint32Array(s1 - s0 + 1);
  for (let i = 0; i < local.length; i += 1) local[i] = Number(starts[i]) - v0;
  return { starts: local, vertices: verts.data as Float32Array, row: row.data as Int32Array, box: [0, 0, 0, 0] };
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

type Attr = { value: Float32Array; size: number };
/** deck.gl binary data of one polygon source, for filled (`fill`) and outlined (`line`) cells. */
interface PolyData {
  poly: Polygons;
  values: Float32Array | null;
  attr: Attr;
  fill: unknown;
  line: unknown;
}

/** One data object per polygon source and colour values (deck.gl compares data by identity:
 * a new object makes it triangulate every polygon again). */
function polyData(poly: Polygons, values: Float32Array | null, prev: PolyData | undefined): PolyData {
  if (prev && prev.poly === poly && prev.values === values) return prev;
  const attr = { value: vertexValues(poly, values), size: 1 };
  const base = { length: poly.row.length, startIndices: poly.starts };
  return {
    poly,
    values,
    attr,
    fill: { ...base, attributes: { getPolygon: { value: poly.vertices, size: 2 }, getColourValue: attr } },
    line: { ...base, attributes: { getPath: { value: poly.vertices, size: 2 }, getColourValue: attr } },
  };
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
    queryFn: async ({ signal }) => {
      const attrs = await readAttrs(version!, path!);
      const names = ((attrs.canopy as { features?: string[] } | undefined)?.features ?? []).map(String);
      const offsets = await readWhole(version!, `${path}/offsets`, { signal });
      const lod = await readWhole(version!, `${path}/lod/offsets`, { signal });
      return { names, offsets: offsets.data as ArrayLike<number>, lod: lod.data as ArrayLike<number> };
    },
    enabled: !!version && !!path,
    staleTime: Infinity,
    retry: 1,
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
    const c = effectiveColouring(spec.params, meta.data, undefined, !!spec.group);
    return { p: c.p, notice: c.notes.length ? `Saved settings changed: ${c.notes.join('; ')}.` : undefined };
  }, [spec.params, meta.data, spec.group]);
  useEffect(() => {
    if (notice) update(spec.id, p);
  }, [notice]); // eslint-disable-line react-hooks/exhaustive-deps
  const el = useMemo(() => (m ? resolveElements(p, m, spec.table) : null), [p, m, spec.table]);
  const system = el?.system ?? null;
  const n = meta.data?.n_obs ?? null;
  const link = useMemo(() => (spec.group ? { group: spec.group, view: spec.id } : undefined), [spec.group, spec.id]);
  const colouring = useColouring(spec.datasetId, spec.table, p, n, link);

  // ---- image
  const imageEntry = m && el?.image ? m.images[el.image] : null;
  const loader = useQuery({
    queryKey: ['viv', version, el?.image],
    queryFn: async () => {
      const t0 = performance.now();
      // our store: no browser cache (B-B2), retries, counted reads, no zarr v2 probes
      const r = await loadOmeZarrFromStore(elementStore(version!, imageEntry!.path) as never);
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
  const blocky = !!shapes && shapes.kind === 'polygons' && !whole;
  const centroids = useStoreArray(version, blocky ? `${shapes!.path}/centroid` : null);
  const blockBox = useStoreArray(version, blocky ? `${shapes!.path}/block_bbox` : null);
  // the area in view, in the cells' own coordinates
  const extent = useLod((s) => s.extent[spec.id]);
  const affine = shapes && system ? shapes.transforms[system] : null;
  const rect = useMemo(() => (extent && affine ? transformBox(invert(affine), extent) : null), [extent, affine]);
  const centreXY = centroids.data?.data as Float32Array | undefined;
  const blocksInView = useMemo(() => {
    if (!blocky || !rect || !centreXY || !blockBox.data) return [];
    let inView = 0;
    for (let i = 0; i < centreXY.length; i += 2) {
      const x = centreXY[i];
      const y = centreXY[i + 1];
      if (x >= rect[0] && x <= rect[2] && y >= rect[1] && y <= rect[3] && ++inView > OUTLINE_BUDGET) return [];
    }
    return boxesInView(blockBox.data.data, rect);
  }, [blocky, rect, centreXY, blockBox.data]);
  const blocks = useQueries({
    queries: blocksInView.map((b) => ({
      queryKey: ['store-block', version, shapes?.path, b],
      queryFn: ({ signal }: { signal: AbortSignal }) => readBlock(version!, shapes!.path, shapes!.n, b, signal),
      staleTime: Infinity,
      gcTime: 30_000,
      retry: 1,
    })),
  });
  const centroidRows = useStoreArray(version, shapes && !whole ? `${shapes.path}/row` : null);
  const circles = useStoreArray(version, shapes?.kind === 'circles' ? `${shapes.path}/center` : null);
  const radius = useStoreArray(version, shapes?.kind === 'circles' ? `${shapes.path}/radius` : null);
  // cells colour only when their table is the view's table
  const colourCells = !!shapes && shapes.table === el?.table;
  const values = colourCells ? (colouring.values?.value ?? null) : null;
  // one data object per polygon source while its colour values stay the same
  const polyCache = useRef(new Map<string, PolyData>());
  const wholeData = useMemo(() => {
    if (!polys.data) return null;
    const d = polyData(polys.data, values, polyCache.current.get('cells'));
    polyCache.current.set('cells', d);
    return d;
  }, [polys.data, values]);
  const blocksKey = blocks.map((q) => q.dataUpdatedAt).join(',');
  const blockData = useMemo(() => {
    const next = new Map<string, PolyData>();
    const out = blocks.map((q, k) => {
      if (!q.data) return null;
      const key = `b${blocksInView[k]}`;
      const d = polyData(q.data, values, polyCache.current.get(key));
      next.set(key, d);
      return d;
    });
    if (wholeData) next.set('cells', wholeData);
    polyCache.current = next;
    return out;
  }, [blocksKey, values, wholeData]); // eslint-disable-line react-hooks/exhaustive-deps
  const failedBlocks = blocks.filter((q) => q.error).length;
  const loadedBlocks = blocks.filter((q) => q.data).length;
  const outlinesReady = blocksInView.length > 0 && loadedBlocks + failedBlocks === blocksInView.length && loadedBlocks > 0;
  const pointRows = centroidRows.data?.data as Int32Array | undefined;
  const pointAttr = useMemo(() => (pointRows ? { value: gatherByRow(pointRows, values), size: 1 } : null), [pointRows, values]);
  useEffect(() => {
    if (failedBlocks) event('spatial_blocks_failed', null, { failed: failedBlocks, of: blocksInView.length });
  }, [failedBlocks]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- transcripts
  const points = m && el?.transcripts ? m.points[el.transcripts] : null;
  const features = useFeatures(version, points?.path ?? null);
  const chunkBox = useStoreArray(version, points ? `${points.path}/chunk_bbox` : null);
  const pAffine = points && system ? points.transforms[system] : null;
  const pRect = useMemo(() => (extent && pAffine ? transformBox(invert(pAffine), extent) : null), [extent, pAffine]);
  const colours = geneColours(p.genes, p.geneColours ?? {});
  const geneReads = useQueries({
    queries: p.genes.map((g) => {
      const f = features.data ? features.data.names.indexOf(g) : -1;
      const lo = f >= 0 ? Number(features.data!.offsets[f]) : 0;
      const hi = f >= 0 ? Number(features.data!.offsets[f + 1]) : 0;
      const big = !!points && hi - lo > points.lod_cap;
      // a common gene zoomed in: its chunks in view, when they hold few enough transcripts
      const parts =
        big && pRect && chunkBox.data
          ? boxesInView(chunkBox.data.data, pRect, Math.floor(lo / POINT_CHUNK), Math.ceil(hi / POINT_CHUNK))
          : null;
      const inView = !!parts && parts.length * POINT_CHUNK <= IN_VIEW_POINTS;
      return {
        queryKey: ['store-transcripts', version, points?.path, g, inView ? parts!.join(',') : null],
        queryFn: async ({ signal }: { signal: AbortSignal }) => {
          if (f < 0) return null;
          const t0 = performance.now();
          let xy: Float32Array;
          let mode: 'all' | 'sample' | 'in view';
          const path = `${points!.path}/xy`;
          if (inView) {
            const reads = await Promise.all(
              parts!.map((k) =>
                readRows(version!, path, Math.max(lo, k * POINT_CHUNK), Math.min(hi, (k + 1) * POINT_CHUNK), { signal }),
              ),
            );
            xy = new Float32Array(reads.reduce((a, r) => a + r.data.length, 0));
            let o = 0;
            for (const r of reads) {
              xy.set(r.data as Float32Array, o);
              o += r.data.length;
            }
            mode = 'in view';
          } else if (big) {
            const [a, b] = [Number(features.data!.lod[f]), Number(features.data!.lod[f + 1])];
            xy = (await readRows(version!, `${points!.path}/lod/xy`, a, b, { signal })).data as Float32Array;
            mode = 'sample';
          } else {
            xy = (await readRows(version!, path, lo, hi, { signal })).data as Float32Array;
            mode = 'all';
          }
          event('transcripts_loaded', performance.now() - t0, { n: xy.length / 2, mode, chunks: parts?.length ?? 0 });
          return { gene: g, xy, n: hi - lo, mode };
        },
        // the previous read stays on screen while the next loads, but only for the same gene
        // (after a removal, index k is another gene)
        placeholderData: (prev: unknown) => ((prev as { gene?: string } | undefined)?.gene === g ? prev : undefined) as never,
        enabled: !!features.data && !!points,
        staleTime: Infinity,
        gcTime: 30_000,
        retry: 1,
      };
    }),
  });

  // ---- layers
  const id = (name: string) => panelLayerId(spec.id, MAIN_PANEL, name);
  const valueColour = colouring.valueColour;

  // Lasso: the cells whose centre is inside (all cells of the element, read once), when the
  // cells are the rows of the view's table; hidden categories left out
  const qc = useQueryClient();
  const lasso = useMemo<ViewOutput['lasso']>(() => {
    if (!shapes || !affine || !version || !colourCells || n === null) return undefined;
    const read = (path: string) =>
      qc.fetchQuery({
        queryKey: ['store-array', version, path],
        queryFn: ({ signal }) => readWhole(version, path, { signal }),
        staleTime: Infinity,
      });
    return {
      key: `${version}|${system}|${shapes.path}`,
      n,
      select: async (_panel: string, poly: Polygon, mask: Uint8Array) => {
        const [xy, rows] = await Promise.all([
          read(`${shapes.path}/${shapes.kind === 'circles' ? 'center' : 'centroid'}`),
          read(`${shapes.path}/row`),
        ]);
        const inv = invert(affine);
        const own = poly.map(([x, y]) => [inv[0][0] * x + inv[0][1] * y + inv[0][2], inv[1][0] * x + inv[1][1] * y + inv[1][2]] as [number, number]);
        return lassoInto(mask, xy.data as Float32Array, own, null, hiddenBy(valueColour, values), rows.data as Int32Array);
      },
    };
  }, [shapes, affine, version, colourCells, n, qc, system, valueColour, values]);
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
      const cells = (name: string, d: PolyData) => {
        if (p.cellStyle === 'fill') {
          out.push(
            new SolidPolygonLayer({
              id: id(`${name}-fill`),
              data: d.fill as never,
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
              id: id(`${name}-line`),
              data: d.line as never,
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
      };
      const centres = (name: string, pos: Float32Array, attr: Attr, r: Float32Array | null) =>
        new ScatterplotLayer({
          id: id(name),
          data: {
            length: pos.length / 2,
            attributes: {
              getPosition: { value: pos, size: 2 },
              getColourValue: attr,
              ...(r ? { getRadius: { value: r, size: 1 } } : {}),
            },
          },
          radiusUnits: r ? 'common' : 'pixels',
          getRadius: 2,
          getFillColor: [...BASE_RGB, 255],
          opacity: p.cellOpacity,
          pickable: true,
          modelMatrix: mm,
          extensions: [VALUE_COLOUR],
          valueColour: vc,
        } as ScatterplotLayerProps & { valueColour: ValueColour });
      if (wholeData) cells('cells', wholeData);
      else if (circles.data && pointAttr) {
        out.push(centres('cell-centres', circles.data.data as Float32Array, pointAttr, (radius.data?.data as Float32Array) ?? null));
      } else if (centroids.data && pointAttr && !outlinesReady) {
        const pos = centroids.data.data as Float32Array;
        if (!loadedBlocks) out.push(centres('cell-centres', pos, pointAttr, null));
        else {
          // while blocks load: centres only where outlines are not drawn yet
          blocks.forEach((q, k) => {
            if (q.data) return;
            const b = blocksInView[k];
            const [a, z] = [b * POLY_BLOCK, Math.min(shapes.n, (b + 1) * POLY_BLOCK)];
            const sub = { value: pointAttr.value.subarray(a, z), size: 1 };
            out.push(centres(`cell-centres-b${b}`, pos.subarray(2 * a, 2 * z), sub, null));
          });
        }
      }
      blockData.forEach((d, k) => {
        if (d) cells(`cells-b${blocksInView[k]}`, d);
      });
    }
    if (points && points.transforms[system]) {
      const mm = modelMatrix(points.transforms[system]);
      geneReads.forEach((q, k) => {
        const d = q.data;
        if (!d) return;
        const rgb = hexToRgb(colours[d.gene] ?? GENE_COLOURS[k % GENE_COLOURS.length]);
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
  }, [m, system, imageEntry, loader.data, JSON.stringify(shownChannels), shapes, wholeData, centroids.data, circles.data, radius.data, pointAttr, blockData, outlinesReady, loadedBlocks, p.cellStyle, p.cellOpacity, p.pointSize, colourCells, valueColour, points, JSON.stringify(colours), geneReads.map((q) => q.dataUpdatedAt).join(',')]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- bounds: everything placed in the system
  const bounds = useMemo<Bounds | null>(() => {
    if (!m || !system) return null;
    const boxes: (Bounds | null)[] = [];
    if (imageEntry) {
      const [, h, w] = imageEntry.levels[0];
      boxes.push(transformBox(imageEntry.transforms[system], [0, 0, w, h]));
    }
    if (shapes && polys.data) boxes.push(transformBox(shapes.transforms[system], polys.data.box));
    const bb = blockBox.data?.data;
    if (shapes && bb && bb.length) {
      let box: Bounds = [bb[0], bb[1], bb[2], bb[3]];
      for (let k = 4; k < bb.length; k += 4) {
        box = [Math.min(box[0], bb[k]), Math.min(box[1], bb[k + 1]), Math.max(box[2], bb[k + 2]), Math.max(box[3], bb[k + 3])];
      }
      boxes.push(transformBox(shapes.transforms[system], box));
    }
    if (points?.transforms[system]) boxes.push(transformBox(points.transforms[system], points.bbox));
    return union(boxes);
  }, [m, system, imageEntry, shapes, polys.data, blockBox.data, points]);

  const tooltip = useCallback(
    (i: number, layer: string) => {
      if (!colourCells) return null;
      const name = layer.slice(layer.lastIndexOf(':') + 1).replace(/-(fill|line)$/, '');
      let rows: ArrayLike<number> | undefined;
      let at = i;
      if (name === 'cells') rows = polys.data?.row;
      else if (name === 'cell-centres') rows = pointRows;
      else if (name.startsWith('cell-centres-b')) {
        rows = pointRows;
        at = Number(name.slice(14)) * POLY_BLOCK + i;
      } else if (name.startsWith('cells-b')) rows = blockData[blocksInView.indexOf(Number(name.slice(7)))]?.poly.row;
      return rows && rows[at] >= 0 ? colouring.tooltip(rows[at]) : null;
    },
    [colourCells, polys.data, pointRows, blockData, blocksInView, colouring],
  );
  const loading = manifest.isLoading || loader.isLoading || polys.isLoading || features.isLoading;
  const fatal = meta.error ?? manifest.error;
  const failedGenes = geneReads.filter((q) => q.error).length;
  const failedParts = [loader, polys, features, centroids, centroidRows, blockBox, chunkBox, circles, radius].filter((q) => q.error);
  const problem =
    failedParts[0]?.error ??
    colouring.error ??
    (failedBlocks ? new Error(`${failedBlocks} of ${blocksInView.length} outline blocks did not load`) : null) ??
    (failedGenes ? new Error(`transcripts of ${failedGenes} gene${failedGenes > 1 ? 's' : ''} did not load`) : null);
  const retry = () => {
    for (const q of [...failedParts, ...blocks, ...geneReads]) if (q.error) void q.refetch();
    colouring.retry();
  };
  // without its converted copy (changed source, pruned cache, a fresh install) the view cannot
  // load: it says so and offers the conversion, instead of waiting forever
  const unconverted = !!meta.data && meta.data.mode !== 'converted';
  const status: ViewOutput['status'] = fatal || unconverted ? 'error' : bounds && !loading ? 'ready' : 'loading';
  const t0 = useRef(performance.now());
  const reported = useRef(false);
  useEffect(() => {
    if (status === 'ready' && !reported.current) {
      reported.current = true;
      event('view_ready', performance.now() - t0.current, {
        type: 'spatial',
        view: spec.id,
        dataset: spec.datasetId,
        cells: shapes?.n ?? 0,
        whole: whole,
        image_levels: imageEntry?.levels.length ?? 0,
        genes: p.genes.length,
      });
    }
  }, [status]); // eslint-disable-line react-hooks/exhaustive-deps
  return {
    layers,
    bounds,
    // refit when the frame changes (another system), not when parts arrive after the user moved
    fitKey: `${version}|${system}`,
    flipY: true,
    status,
    error: fatal
      ? String((fatal as Error).message)
      : unconverted
        ? 'The spatial view needs the converted copy of this dataset (the source changed, or it was never converted here).'
        : undefined,
    needsConversion: unconverted ? spec.datasetId : undefined,
    notice,
    problem: problem ? { message: `Part of the view failed: ${(problem as Error).message}.`, retry } : undefined,
    tooltip,
    caption: shapes
      ? `${formatCount(shapes.n)} cells${whole || shapes.kind === 'circles' || outlinesReady ? '' : loadedBlocks ? ' (loading outlines…)' : ' (centres; zoom in for outlines)'}${colouring.missingHere && p.colorBy ? ` · no “${p.colorBy.name}”: NA` : ''}`
      : undefined,
    n: shapes?.n ?? 0,
    // drawn cells count against the canvas's shared point budget (outlines or centres)
    total: shapes?.n ?? 0,
    legend: colourCells ? colouring.legend : undefined,
    lasso: spec.group ? undefined : lasso,
  };
}

const CELL_BASE: ValueColour = {
  lut: new Uint8Array([...BASE_RGB, 255]),
  mode: 'categories',
  missing: [...BASE_RGB, 255] as [number, number, number, number],
};

// ---------------------------------------------------------------- settings

/** The slider's range for a channel: from the converter's window (a few times its top, so the
 * track does not move while dragging), within the data type; never smaller than the setting. */
export function contrastRange(stored: [number, number], current: [number, number], dtype: string) {
  const cap = ({ uint8: 255, uint16: 65535, int16: 32767, uint32: 2 ** 32 - 1 } as Record<string, number>)[dtype];
  const top = Math.max(stored[1] * 4, stored[0] + 1, current[1]);
  const max = cap !== undefined ? Math.min(cap, niceCeil(top)) : niceCeil(top);
  const min = Math.min(0, stored[0], current[0]);
  const integer = cap !== undefined;
  const step = integer ? Math.max(1, Math.round((max - min) / 1000)) : (max - min) / 1000;
  return { min, max: Math.max(max, current[1]), step };
}

function niceCeil(v: number): number {
  if (v <= 0) return 1;
  const e = 10 ** Math.floor(Math.log10(v));
  return Math.ceil(v / e) * e;
}

function ContrastSlider({
  name,
  value,
  stored,
  dtype,
  onChange,
}: {
  name: string;
  value: [number, number];
  stored: [number, number];
  dtype: string;
  onChange: (w: [number, number]) => void;
}) {
  // the range is fixed while the pointer is down (it follows the setting only between drags)
  const range = useRef<ReturnType<typeof contrastRange> | null>(null);
  const r = contrastRange(stored, value, dtype);
  if (!range.current || value[1] > range.current.max || value[0] < range.current.min) range.current = r;
  const { min, max, step } = range.current;
  const auto = value[0] === stored[0] && value[1] === stored[1];
  return (
    <div className="contrast">
      <RangeSlider label={`Contrast of ${name}`} value={value} min={min} max={max} step={step} onChange={onChange} format={(v) => formatCount(v)} />
      {!auto && (
        <button className="link small" onClick={() => onChange([stored[0], stored[1]])} title="Back to the range found in the data">
          auto
        </button>
      )}
    </div>
  );
}

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
            <div className="channel" key={i}>
              <label className="check">
                <input type="checkbox" checked={c.on} onChange={(e) => setChannel(i, { on: e.target.checked })} />
                <ColourInput label={`Colour of channel ${img.channels[i].name}`} value={c.colour} onChange={(hex) => setChannel(i, { colour: hex })} />
                {img.channels[i].name}
              </label>
              {c.on && (
                <ContrastSlider
                  name={img.channels[i].name}
                  value={c.window}
                  stored={img.channels[i].window}
                  dtype={img.dtype}
                  onChange={(w) => setChannel(i, { window: w })}
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
          <>
            <ColourFields spec={spec} />
            <PlotCompanionField spec={spec} />
          </>
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
            an even sample of {formatCount(points?.lod_cap ?? 100_000)} of them when zoomed out, and all of their
            transcripts in view once you zoom in far enough.
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
              onPick={(g) => {
                if (p.genes.includes(g)) return;
                const genes = [...p.genes, g];
                update(spec.id, { genes, geneColours: geneColours(genes, p.geneColours ?? {}) });
              }}
              testId="transcript-search"
            />
          )}
          <ul className="gene-chips" data-testid="transcript-genes">
            {p.genes.map((g) => (
              <li key={g}>
                <span className="swatch" style={{ background: geneColours(p.genes, p.geneColours ?? {})[g] }} />
                {g}
                <button
                  className="icon-button"
                  aria-label={`Remove ${g}`}
                  onClick={() => {
                    const genes = p.genes.filter((x) => x !== g);
                    update(spec.id, { genes, geneColours: geneColours(genes, p.geneColours ?? {}) });
                  }}
                >
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
      geneColours: {},
      pointSize: 1.5,
    };
  },
  normalise: normaliseParams,
  useView: useSpatialView,
  Panel: SpatialPanel,
  Legend: ColourLegend as never,
});
