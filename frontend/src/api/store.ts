// Reading a converted dataset's store (contract/SPEC.md) straight from the server's static
// store route: the manifest, and arrays through zarrita with HTTP range requests (only the
// chunks needed). Images are read by Viv (views/spatial).
//
// Reads bypass the browser's HTTP cache (`no-store`): with many concurrent ranges of the same
// shard file it returned empty bodies (review S6, B-B2). Instead this module keeps its own:
// - opened arrays per store version (so `zarr.json` and shard indexes are read once);
// - decoded chunks of row-chunked arrays, by bytes (least recently used dropped first), so
//   panning re-reads only chunks it has not seen;
// - whole small arrays (centroids, rows, boxes), read with one request per shard file.
// A read that fails or comes back short is retried once; failures are counted and reported
// (`storeStats`, telemetry `store_reads`). Reads take an AbortSignal, so stale reads stop.

import { useQuery } from '@tanstack/react-query';
import * as zarr from 'zarrita';

import { event, timeAsync } from '../telemetry/telemetry';

export type Affine = number[][]; // 3×3, row-major

interface Spatial {
  path: string;
  source_name?: string | null;
  transforms: Record<string, Affine>;
}

export interface ImageEntry extends Spatial {
  axes: string[];
  dtype: string;
  levels: number[][];
  channels: { name: string; window: [number, number]; color: string | null }[];
  rgb: boolean;
}

export interface ShapesEntry extends Spatial {
  kind: 'polygons' | 'circles';
  n: number;
  n_vertices: number | null;
  table: string | null;
}

export interface PointsEntry extends Spatial {
  n: number;
  n_features: number;
  feature_key: string;
  has_cell: boolean;
  has_z: boolean;
  z_planes: number[] | null;
  bbox: [number, number, number, number];
  n_overview: number;
  lod_cap: number;
  table: string | null;
}

export interface Manifest {
  contract_version: string;
  coordinate_systems: string[];
  units: Record<string, string>;
  tables: Record<string, { path: string; source_name?: string | null; n_obs: number; region: string[] }>;
  images: Record<string, ImageEntry>;
  labels: Record<string, Spatial & { table: string | null }>;
  shapes: Record<string, ShapesEntry>;
  points: Record<string, PointsEntry>;
}

/** The store's URL (absolute: zarrita and Viv resolve paths against it). */
export const storeUrl = (version: string) => new URL(`/api/stores/${version}/c0`, window.location.origin).href;

export const useManifest = (version: string | undefined, enabled = true) =>
  useQuery({
    queryKey: ['manifest', version],
    queryFn: async () => {
      const r = await fetch(`${storeUrl(version!)}/manifest.json`);
      if (!r.ok) throw new Error(`The converted copy could not be read (${r.status}).`);
      return (await r.json()) as Manifest;
    },
    enabled: !!version && enabled,
    staleTime: Infinity,
    retry: false,
  });

// ---------------------------------------------------------------- counters

const counters = { requests: 0, bytes: 0, failures: 0, retries: 0, chunkHits: 0, chunkMisses: 0, aborted: 0 };

/** Store reads since the page opened (for telemetry and tests). */
export const storeStats = () => ({ ...counters });

let reported = { ...counters };
/** Report what was read since the last report (one `store_reads` event; nothing if idle). */
export function reportStoreReads() {
  const d = Object.fromEntries(Object.entries(counters).map(([k, v]) => [k, v - reported[k as keyof typeof counters]]));
  if (!d.requests && !d.failures && !d.chunkHits) return;
  reported = { ...counters };
  event('store_reads', null, d);
}
if (typeof window !== 'undefined') {
  setInterval(reportStoreReads, 10_000);
  (window as unknown as { __canopyStoreStats?: unknown }).__canopyStoreStats = storeStats; // tests, debugging
}

// ---------------------------------------------------------------- the store

type RangeQuery = { offset: number; length: number } | { suffixLength: number };
type Opts = { signal?: AbortSignal };

export class StoreReadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'StoreReadError';
  }
}

const isAbort = (e: unknown) => (e as { name?: string })?.name === 'AbortError';

/** One request (no browser cache), checked for a complete body; retried once. */
async function fetchBytes(url: string, range: string | null, want: number | null, opts: Opts): Promise<Uint8Array | undefined> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      counters.requests += 1;
      const r = await fetch(url, {
        cache: 'no-store',
        signal: opts.signal,
        headers: range ? { Range: range } : undefined,
      });
      if (r.status === 404) return undefined;
      if (r.status !== 200 && r.status !== 206) throw new StoreReadError(`status ${r.status}`);
      const b = new Uint8Array(await r.arrayBuffer());
      counters.bytes += b.length;
      if ((want !== null && b.length !== want) || (range && b.length === 0)) {
        throw new StoreReadError(`short reply (${b.length} of ${want ?? '?'} bytes)`);
      }
      return b;
    } catch (e) {
      if (isAbort(e)) {
        counters.aborted += 1;
        throw e;
      }
      if (attempt >= 1) {
        counters.failures += 1;
        throw e instanceof StoreReadError ? e : new StoreReadError((e as Error).message);
      }
      counters.retries += 1;
    }
  }
}

function keyUrl(base: string, key: string) {
  return `${base}/${key.replace(/^\//, '')}`;
}

// Stores are zarr v3: readers that probe v2 metadata first get "absent" without a request (no
// 404s in the console, which hid real errors).
const V2_METADATA = /(^|\/)\.(zgroup|zattrs|zarray|zmetadata)$/;

/** Range reads of a store (zarrita's AsyncReadable). */
export class CanopyStore {
  constructor(readonly base: string) {}

  get(key: string, opts: Opts = {}) {
    if (V2_METADATA.test(key)) return Promise.resolve(undefined);
    return fetchBytes(keyUrl(this.base, key), null, null, opts);
  }

  getRange(key: string, range: RangeQuery, opts: Opts = {}) {
    if ('suffixLength' in range) {
      return fetchBytes(keyUrl(this.base, key), `bytes=-${range.suffixLength}`, range.suffixLength, opts);
    }
    const { offset, length } = range;
    return fetchBytes(keyUrl(this.base, key), `bytes=${offset}-${offset + length - 1}`, length, opts);
  }
}

/** Whole files, each fetched once (for reading whole arrays: one request per shard file). */
class WholeFileStore {
  private files = new Map<string, Promise<Uint8Array | undefined>>();

  constructor(readonly base: string) {}

  get(key: string, opts: Opts = {}) {
    if (V2_METADATA.test(key)) return Promise.resolve(undefined);
    let f = this.files.get(key);
    if (!f) {
      f = fetchBytes(keyUrl(this.base, key), null, null, opts);
      this.files.set(key, f);
    }
    return f;
  }

  async getRange(key: string, range: RangeQuery, opts: Opts = {}) {
    const f = await this.get(key, opts);
    if (!f) return undefined;
    // copies: decoders view the bytes as wider types, which needs an aligned start
    return 'suffixLength' in range ? f.slice(f.length - range.suffixLength) : f.slice(range.offset, range.offset + range.length);
  }
}

// ---------------------------------------------------------------- arrays

type Arr = zarr.Array<zarr.DataType, CanopyStore>;
const stores = new Map<string, CanopyStore>();
const arrays = new Map<string, Promise<Arr>>();

function store(version: string) {
  let s = stores.get(version);
  if (!s) {
    s = new CanopyStore(storeUrl(version));
    stores.set(version, s);
  }
  return s;
}

/** A store rooted at one element (e.g. an image for Viv). */
export const elementStore = (version: string, path: string) => new CanopyStore(`${storeUrl(version)}/${path}`);

/** An opened array, cached per version (its metadata and shard indexes are read once). */
function open(version: string, path: string): Promise<Arr> {
  const k = `${version}|${path}`;
  let a = arrays.get(k);
  if (!a) {
    a = zarr.open.v3(zarr.root(store(version)).resolve(path), { kind: 'array' }) as Promise<Arr>;
    a.catch(() => arrays.delete(k)); // a failed open is tried again next time
    arrays.set(k, a);
  }
  return a;
}

/** An array (or part of it) of the store. */
export interface Chunk {
  data: ArrayLike<number> & { buffer: ArrayBufferLike };
  shape: number[];
}

type Sel = (zarr.Slice | number | null)[] | undefined;

/** A selection of an array (ranged reads of the chunks it touches). */
export async function readArray(version: string, path: string, selection?: Sel, opts: Opts = {}): Promise<Chunk> {
  const arr = await open(version, path);
  return empty(arr) ?? ((await zarr.get(arr, selection as never, { opts })) as unknown as Chunk);
}

/** A whole array, with one request per (shard) file instead of one per chunk. */
export async function readWhole(version: string, path: string, opts: Opts = {}): Promise<Chunk> {
  const arr = await zarr.open.v3(zarr.root(new WholeFileStore(storeUrl(version))).resolve(path), { kind: 'array' });
  return empty(arr) ?? ((await zarr.get(arr, undefined as never, { opts })) as unknown as Chunk);
}

/** zarrita refuses to read an array with no elements ("Input contains an empty iterator"); an
 * element with no rows is valid, so it reads as empty. */
function empty(arr: { shape: number[]; dtype: unknown }): Chunk | null {
  if (!arr.shape.includes(0)) return null;
  const Ctor = TYPED[arr.dtype as string] ?? Float32Array;
  return { data: new Ctor(0), shape: arr.shape.slice() };
}

export async function readAttrs(version: string, path: string): Promise<Record<string, unknown>> {
  const g = await zarr.open.v3(zarr.root(store(version)).resolve(path), { kind: 'group' });
  return g.attrs as Record<string, unknown>;
}

export const slice = zarr.slice;

// ---------------------------------------------------------------- decoded chunks (LRU by bytes)

type Typed = ArrayLike<number> & { buffer: ArrayBufferLike; set: (a: ArrayLike<number>, o: number) => void };
const TYPED: Record<string, new (n: number) => Typed> = {
  float32: Float32Array,
  float64: Float64Array,
  int8: Int8Array,
  uint8: Uint8Array,
  int16: Int16Array,
  uint16: Uint16Array,
  int32: Int32Array,
  uint32: Uint32Array,
  int64: BigInt64Array as never,
  uint64: BigUint64Array as never,
};

const CHUNK_CACHE_BYTES = 256 * 2 ** 20;
const chunks = new Map<string, Promise<ArrayLike<number> & { buffer: ArrayBufferLike; byteLength: number }>>();
const sizes = new Map<string, number>();
let cached = 0;

function remember(key: string, bytes: number) {
  sizes.set(key, bytes);
  cached += bytes;
  for (const k of chunks.keys()) {
    if (cached <= CHUNK_CACHE_BYTES) break;
    if (k === key) continue;
    chunks.delete(k);
    cached -= sizes.get(k) ?? 0;
    sizes.delete(k);
  }
}

async function chunk(version: string, path: string, k: number, width: number, opts: Opts) {
  const key = `${version}|${path}|${k}`;
  let c = chunks.get(key);
  if (c) {
    chunks.delete(key); // most recently used last
    chunks.set(key, c);
    counters.chunkHits += 1;
    return c;
  }
  counters.chunkMisses += 1;
  c = (async () => {
    const arr = await open(version, path);
    // a chunk read is shared: one caller's abort must not fail the others
    const coords = [k, ...Array(arr.shape.length - 1).fill(0)];
    const got = await arr.getChunk(coords);
    return got.data as unknown as ArrayLike<number> & { buffer: ArrayBufferLike; byteLength: number };
  })();
  chunks.set(key, c);
  c.then(
    (d) => remember(key, d.byteLength ?? (d.length * 4 * width)),
    () => chunks.delete(key),
  );
  if (opts.signal) {
    // the caller stops waiting; the read itself completes into the cache
    return new Promise<Awaited<typeof c>>((resolve, reject) => {
      const onAbort = () => reject(new DOMException('aborted', 'AbortError'));
      if (opts.signal!.aborted) return onAbort();
      opts.signal!.addEventListener('abort', onAbort, { once: true });
      c!.then(resolve, reject);
    });
  }
  return c;
}

/** Rows lo..hi (exclusive) of a row-chunked array (1-D, or 2-D with `width` columns), from
 * cached decoded chunks. */
export async function readRows(
  version: string,
  path: string,
  lo: number,
  hi: number,
  opts: Opts = {},
): Promise<Chunk> {
  const arr = await open(version, path);
  const rows = arr.chunks[0];
  const width = arr.shape.length > 1 ? arr.shape[1] : 1;
  const n = Math.max(0, hi - lo);
  const first = Math.floor(lo / rows);
  const last = n ? Math.ceil(hi / rows) - 1 : first - 1;
  const parts = await Promise.all(
    Array.from({ length: Math.max(0, last - first + 1) }, (_, i) => chunk(version, path, first + i, width, opts)),
  );
  const Ctor = TYPED[arr.dtype as string];
  if (!Ctor) throw new StoreReadError(`unsupported dtype ${arr.dtype}`);
  const out = new Ctor(n * width);
  parts.forEach((d, i) => {
    const k = first + i;
    const from = Math.max(lo, k * rows) - k * rows;
    const to = Math.min(hi, (k + 1) * rows) - k * rows;
    const src = (d as unknown as { subarray: (a: number, b: number) => ArrayLike<number> }).subarray(from * width, to * width);
    out.set(src, (Math.max(lo, k * rows) - lo) * width);
  });
  return { data: out, shape: width > 1 ? [n, width] : [n] };
}

/** The whole of a (small or medium) array, kept a minute after no view uses it. */
export const useStoreArray = (version: string | undefined, path: string | null) =>
  useQuery({
    queryKey: ['store-array', version, path],
    queryFn: ({ signal }) =>
      timeAsync('store_array', () => readWhole(version!, path!, { signal }), (a) => ({ n: a.data.length })),
    enabled: !!version && !!path,
    staleTime: Infinity,
    gcTime: 60_000,
    retry: 1,
  });
