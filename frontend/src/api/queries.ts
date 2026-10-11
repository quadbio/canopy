// TanStack Query hooks: caching, de-duplication and loading states for every backend read.

import { keepPreviousData, useQuery } from '@tanstack/react-query';

import { catalog, datasets } from './endpoints';
import { timeAsync } from '../telemetry/telemetry';

const MINUTE = 60_000;

export const useRoots = () => useQuery({ queryKey: ['roots'], queryFn: catalog.roots, staleTime: MINUTE });

export const useFolder = (root: string, path: string, enabled = true) =>
  useQuery({ queryKey: ['folder', root, path], queryFn: () => catalog.folder(root, path), enabled, staleTime: MINUTE });

export const useSearch = (q: string) =>
  useQuery({
    queryKey: ['search', q],
    queryFn: ({ signal }) => catalog.search(q, signal),
    enabled: q.trim().length > 0,
    placeholderData: keepPreviousData,
    staleTime: MINUTE,
  });

export const useDatasetDetail = (id: string | null) =>
  useQuery({ queryKey: ['dataset', id], queryFn: () => catalog.dataset(id!), enabled: !!id, staleTime: MINUTE });

export const useCatalogStatus = () =>
  useQuery({
    queryKey: ['catalog-status'],
    queryFn: catalog.status,
    refetchInterval: (q) => (q.state.data?.scanning ? 1000 : 15_000),
  });

/** Query options for a dataset's metadata (shared by `useMeta` and the file browser's Open). */
export const metaQuery = (id: string, table: string | null) => ({
  queryKey: ['meta', id, table],
  queryFn: () => timeAsync('dataset_meta', () => datasets.meta(id, table)),
  staleTime: 10 * MINUTE,
  retry: false,
});

export const useMeta = (id: string, table: string | null) => useQuery(metaQuery(id, table));

// Arrays (10–40 MB each at 10 M cells) are dropped 30 s after no view uses them, and keyed by the
// dataset version, so a rewritten file is never answered from an old copy.
const ARRAY_GC_MS = 30_000;

export const useObs = (id: string, table: string | null, column: string | null, version: string | undefined) =>
  useQuery({
    queryKey: ['obs', id, version, table, column],
    queryFn: ({ signal }) =>
      timeAsync('obs_loaded', () => datasets.obs(id, table, column!, signal), (a) => ({ n: a.data.length })),
    enabled: !!column && !!version,
    staleTime: Infinity,
    gcTime: ARRAY_GC_MS,
    retry: false,
  });

export const useObsLabels = (id: string, table: string | null, column: string | null, version: string | undefined) =>
  useQuery({
    queryKey: ['obs-labels', id, version, table, column],
    queryFn: () => datasets.labels(id, table, column!),
    enabled: !!column && !!version,
    staleTime: Infinity,
    retry: false,
  });

export const useEmbedding = (
  id: string,
  table: string | null,
  key: string | null,
  dims: number[],
  version: string | undefined,
) =>
  useQuery({
    queryKey: ['obsm', id, version, table, key, dims.join(',')],
    queryFn: ({ signal }) =>
      timeAsync('embedding_loaded', () => datasets.obsm(id, table, key!, dims, signal), (a) => ({ n: a.shape[0] })),
    enabled: !!key && !!version,
    staleTime: Infinity,
    gcTime: ARRAY_GC_MS,
    retry: false,
  });

export const useGenes = (id: string, table: string | null, version: string | undefined, enabled = true) =>
  useQuery({
    queryKey: ['genes', id, version, table],
    queryFn: () => timeAsync('genes_loaded', () => datasets.genes(id, table), (g) => ({ n: g.names.length })),
    enabled: enabled && !!version,
    staleTime: Infinity,
    retry: false,
  });

export const useGene = (
  id: string,
  table: string | null,
  index: number | null,
  matrix: string,
  version: string | undefined,
) =>
  useQuery({
    queryKey: ['gene', id, version, table, matrix, index],
    queryFn: ({ signal }) =>
      timeAsync('gene_loaded', () => datasets.gene(id, table, index!, matrix, signal), (g) => ({
        n: g.values.length,
        nnz: g.nnz,
      })),
    enabled: index !== null && index >= 0 && !!version,
    staleTime: Infinity,
    gcTime: ARRAY_GC_MS,
    retry: false,
  });
