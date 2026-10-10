// Shapes of the backend's JSON replies (see backend/dbx/catalog/api.py, backend/dbx/data/api.py).

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

export interface DatasetMeta {
  id: string;
  version: string;
  name: string;
  kind: string;
  table: string | null;
  tables: string[];
  n_obs: number;
  n_vars: number;
  mode: 'lazy' | 'memory';
  obs: ColumnMeta[];
  embeddings: { key: string; dims: number }[];
  warnings: string[];
}

export type TypedArray = Float32Array | Int8Array | Int16Array | Int32Array | Uint8Array;

export interface BinaryArray<T extends TypedArray = TypedArray> {
  data: T;
  shape: number[];
  dtype: string;
  kind: string | null;
}
