// One function per backend endpoint.

import { getBinary, getJson, postJson, url } from './client';
import type { CatalogStatus, DatasetEntry, DatasetMeta, FolderListing, RootEntry } from './types';

export interface DatasetDetail extends DatasetEntry {
  fingerprint: Record<string, unknown> | null;
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
    getJson<{ kind: string; categories: string[] | null }>(
      url(`/api/datasets/${encodeURIComponent(id)}/obs/labels`, { column, table }),
    ),
  obsm: (id: string, table: string | null, key: string, dims: number[], signal?: AbortSignal) =>
    getBinary(url(`/api/datasets/${encodeURIComponent(id)}/obsm`, { key, dims: dims.join(','), table }), fetch, {
      signal,
    }),
};
