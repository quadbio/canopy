// One function per backend endpoint.

import { getBinary, getGene, getJson, postJson, url } from './client';
import type {
  CatalogStatus,
  Conversion,
  DatasetEntry,
  DatasetMeta,
  FolderListing,
  GeneList,
  Job,
  PlotSummary,
  RootEntry,
  SummaryQuery,
} from './types';

export interface DatasetDetail extends DatasetEntry {
  fingerprint: Record<string, unknown> | null;
  conversion: Conversion | null;
}

export const catalog = {
  roots: () => getJson<RootEntry[]>('/api/catalog/roots'),
  folder: (root: string, path: string) => getJson<FolderListing>(url('/api/catalog/tree', { root, path })),
  search: (q: string, signal?: AbortSignal) =>
    getJson<DatasetEntry[]>(url('/api/catalog/search', { q, limit: 100 }), fetch, { signal }),
  dataset: (id: string) => getJson<DatasetDetail>(`/api/catalog/datasets/${encodeURIComponent(id)}`),
  status: () => getJson<CatalogStatus>('/api/catalog/status'),
  refresh: (full = false) => postJson<{ started: boolean }>(url('/api/catalog/refresh', { full })),
};

export const datasets = {
  meta: (id: string, table: string | null) =>
    getJson<DatasetMeta>(url(`/api/datasets/${encodeURIComponent(id)}/meta`, { table })),
  obs: (id: string, table: string | null, column: string, signal?: AbortSignal) =>
    getBinary(url(`/api/datasets/${encodeURIComponent(id)}/obs`, { column, table }), fetch, { signal }),
  labels: (id: string, table: string | null, column: string) =>
    getJson<{ kind: string; categories: string[] | null; colors?: string[] | null }>(
      url(`/api/datasets/${encodeURIComponent(id)}/obs/labels`, { column, table }),
    ),
  /** Embedding coordinates, sent as uint16 per column (`q=16`): half the bytes over a slow
   * link, still finer than a screen pixel. */
  obsm: (id: string, table: string | null, key: string, dims: number[], signal?: AbortSignal) =>
    getBinary(url(`/api/datasets/${encodeURIComponent(id)}/obsm`, { key, dims: dims.join(','), table, q: 16 }), fetch, {
      signal,
    }),
  subset: (id: string, table: string | null, n: number, signal?: AbortSignal) =>
    getBinary(url(`/api/datasets/${encodeURIComponent(id)}/subset`, { n, table }), fetch, { signal }),
  /** A plot's summary: per-category histograms, a count table or a 2-D histogram. */
  summary: (id: string, table: string | null, q: SummaryQuery, signal?: AbortSignal) =>
    getJson<PlotSummary>(url(`/api/datasets/${encodeURIComponent(id)}/summary`, { ...q, table }), fetch, { signal }),
  genes: (id: string, table: string | null) =>
    getJson<GeneList>(url(`/api/datasets/${encodeURIComponent(id)}/genes`, { table })),
  /** Gene `i` (position in the gene list); values sent as uint16 (`q=16`): half the bytes, and
   * finer than the 256 colours of a colour map. */
  gene: (id: string, table: string | null, i: number, matrix: string, signal?: AbortSignal) =>
    getGene(url(`/api/datasets/${encodeURIComponent(id)}/gene`, { i, matrix, q: 16, table }), { signal }),
};

export const jobs = {
  convert: (id: string, xSource = 'X', force = false, featureKey?: string) =>
    postJson<Job>(`/api/datasets/${encodeURIComponent(id)}/convert`, {
      x_source: xSource,
      force,
      ...(featureKey ? { feature_key: featureKey } : {}),
    }) as Promise<Job>,
  active: () => getJson<Job[]>('/api/jobs?active=true'),
  get: (job: number) => getJson<Job>(`/api/jobs/${job}`),
  cancel: (job: number) => postJson<Job>(`/api/jobs/${job}/cancel`, {}) as Promise<Job>,
};
