// Reading a converted dataset's store (contract/SPEC.md) straight from the server's static
// store route: the manifest, and arrays through zarrita with HTTP range requests (only the
// chunks needed). Images are read by Viv (views/spatial).

import { useQuery } from '@tanstack/react-query';
import * as zarr from 'zarrita';

import { timeAsync } from '../telemetry/telemetry';

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

const roots = new Map<string, zarr.Location<zarr.FetchStore>>();

function root(version: string) {
  let r = roots.get(version);
  if (!r) {
    // suffix requests read shard indexes with one `bytes=-N` request (see scripts/read-store.mjs)
    r = zarr.root(new zarr.FetchStore(storeUrl(version), { useSuffixRequest: true }));
    roots.set(version, r);
  }
  return r;
}

type Sel = (zarr.Slice | number | null)[] | undefined;

/** An array (or part of it) of the store. */
export interface Chunk {
  data: ArrayLike<number> & { buffer: ArrayBufferLike };
  shape: number[];
}

export async function readArray(version: string, path: string, selection?: Sel): Promise<Chunk> {
  const arr = await zarr.open.v3(root(version).resolve(path), { kind: 'array' });
  return (await zarr.get(arr, selection as never)) as unknown as Chunk;
}

export async function readAttrs(version: string, path: string): Promise<Record<string, unknown>> {
  const g = await zarr.open.v3(root(version).resolve(path), { kind: 'group' });
  return g.attrs as Record<string, unknown>;
}

export const slice = zarr.slice;

/** The whole of a (small or medium) array, cached for the session. */
export const useStoreArray = (version: string | undefined, path: string | null) =>
  useQuery({
    queryKey: ['store-array', version, path],
    queryFn: () =>
      timeAsync('store_array', () => readArray(version!, path!), (a) => ({ n: a.data.length })),
    enabled: !!version && !!path,
    staleTime: Infinity,
    gcTime: 60_000,
    retry: false,
  });
