// Shapes of the backend's JSON replies (see backend/canopy/catalog/api.py, backend/canopy/data/api.py).

export interface DatasetSummary {
  n_obs?: number;
  n_vars?: number;
  x_encoding?: string | null;
  n_obs_columns?: number;
  embeddings?: string[];
  n_images?: number;
  n_labels?: number;
  n_points?: number;
  n_shapes?: number;
  n_tables?: number;
  n_warnings?: number;
  n_errors?: number;
  warnings?: string[];
}

export type DatasetState = 'inspected' | 'unsupported' | 'failed' | 'discovered';

export interface DatasetEntry {
  id: string;
  root: string;
  path: string;
  name: string;
  kind: 'h5ad' | 'anndata_zarr' | 'spatialdata_zarr' | null;
  state: DatasetState;
  reason: string | null;
  size_bytes: number | null;
  mtime: number | null;
  version: string | null;
  summary: DatasetSummary;
}

export interface FolderEntry {
  name: string;
  path: string;
  n_datasets: number;
}

export interface FolderListing {
  root: string;
  path: string;
  folders: FolderEntry[];
  datasets: DatasetEntry[];
}

export interface RootEntry {
  name: string;
  n_datasets: number;
  states: Record<string, number>;
}

export interface ScanRecord {
  id: number;
  trigger: string;
  full: boolean;
  started: number;
  finished: number | null;
  status: 'running' | 'done' | 'failed';
  counts: Record<string, unknown> | null;
  error: string | null;
}

export interface CatalogStatus {
  scanning: boolean;
  last_scan: ScanRecord | null;
}

export type ObsKind = 'categorical' | 'numeric' | 'boolean' | 'string' | 'other';

export interface ColumnMeta {
  name: string;
  kind: ObsKind;
  dtype: string;
  n_categories: number | null;
  ordered: boolean | null;
  categories: string[] | null;
  colors: string[] | null;
}

export interface MatrixInfo {
  name: string; // "X" or a layer name
  encoding: string; // csr | csc | dense
  dtype: string;
  /** How genes are read: directly, after loading the matrix once, or not before conversion. */
  access: 'direct' | 'memory' | 'needs_conversion';
}

export interface GeneList {
  names: string[]; // the var index (used in requests and saved settings)
  symbols: string[] | null; // readable symbols when the index holds ids
  symbol_column: string | null;
  duplicates: number; // var names made unique by the server (`A`, `A-1`)
  matrices: MatrixInfo[];
}

/** One gene across all cells, dense, with statistics from the server. */
export interface GeneVector {
  values: Float32Array; // MISSING_VALUE (colour/missing.ts) = missing
  min: number;
  max: number;
  p99: number; // 99th percentile of the non-zero values
  nnz: number;
}

export interface DatasetMeta {
  id: string;
  version: string;
  name: string;
  kind: string;
  table: string | null;
  tables: string[];
  n_obs: number;
  n_vars: number;
  mode: 'lazy' | 'memory' | 'converted';
  obs: ColumnMeta[];
  matrices: MatrixInfo[];
  embeddings: { key: string; dims: number }[];
  warnings: string[];
}

export type TypedArray = Float32Array | Float64Array | Int8Array | Int16Array | Int32Array | Uint8Array;

export interface BinaryArray<T extends TypedArray = TypedArray> {
  data: T;
  shape: number[];
  dtype: string;
  kind: string | null;
}

export type JobState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'interrupted';

export interface Job {
  id: number;
  kind: string;
  dataset: string;
  version: string;
  options: Record<string, unknown>;
  state: JobState;
  stage: string | null; // count | reorder | write | finish | waiting_for_memory | …
  progress: number; // 0..1
  created: number;
  started: number | null;
  finished: number | null;
  error: string | null;
  result: Record<string, unknown> | null;
  info: Record<string, unknown> | null; // e.g. memory numbers while waiting
}

export interface Conversion {
  converted: boolean;
  x_source: string | null; // which matrix the converted copy holds
  job: Job | null;
}
