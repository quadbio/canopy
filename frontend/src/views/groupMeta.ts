// A linked group's settings offer what any of its members has (review full-2 C-H1): columns as
// the union of the members' columns, each marked "in k of N" when only some have it, and genes
// as the union of their gene lists. A member without the chosen column or gene shows NA.

import { useQueries } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useShallow } from 'zustand/react/shallow';

import { datasets } from '../api/endpoints';
import { metaQuery } from '../api/queries';
import type { ColumnMeta, DatasetMeta, GeneList } from '../api/types';
import { type ViewSpec, useWorkspace } from '../state/workspace';

/** Total panels a linked group may draw with Split by (members × categories, review C-H3). */
export const GROUP_PANEL_CAP = 96;

export interface GroupColumn {
  column: ColumnMeta;
  /** members that have it */
  k: number;
}

/** The members of `spec`'s group (just `spec` outside a group), as dataset and table. */
export function useMembers(spec: ViewSpec): { datasetId: string; table: string | null }[] {
  return useWorkspace(
    useShallow((s) =>
      spec.group
        ? s.views.filter((v) => v.group === spec.group).map((v) => `${v.datasetId}\u0000${v.table ?? ''}`)
        : [`${spec.datasetId}\u0000${spec.table ?? ''}`],
    ),
  ).map((k) => {
    const [datasetId, table] = k.split('\u0000');
    return { datasetId, table: table || null };
  });
}

/** Every member's metadata (undefined while loading). */
export function useGroupMetas(spec: ViewSpec): (DatasetMeta | undefined)[] {
  const members = useMembers(spec);
  return useQueries({ queries: members.map((m) => metaQuery(m.datasetId, m.table)) }).map((q) => q.data);
}

/** The members' gene lists, merged (undefined until all are loaded). */
export function useGroupGenes(spec: ViewSpec, metas: (DatasetMeta | undefined)[]): GeneList | undefined {
  const members = useMembers(spec);
  const lists = useQueries({
    queries: members.map((m, i) => ({
      queryKey: ['genes', m.datasetId, metas[i]?.version, m.table],
      queryFn: () => datasets.genes(m.datasetId, m.table),
      enabled: !!metas[i]?.version && (metas[i]?.matrices.some((x) => x.access !== 'needs_conversion') ?? false),
      staleTime: Infinity,
      retry: false,
    })),
  }).map((q) => q.data);
  const ready = lists.filter((l): l is GeneList => !!l);
  const key = ready.map((l) => l.names.length).join(',');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => (ready.length ? mergeGeneLists(ready) : undefined), [key]);
}

/** Columns of all members, in the first member's order then the others', with how many have each. */
export function unionColumns(metas: (DatasetMeta | undefined)[]): GroupColumn[] {
  const out = new Map<string, GroupColumn>();
  for (const m of metas) {
    for (const c of m?.obs ?? []) {
      const had = out.get(c.name);
      if (had) had.k += 1;
      else out.set(c.name, { column: c, k: 1 });
    }
  }
  return [...out.values()];
}

/** One gene list for a group: every member's genes once (by their index name). */
export function mergeGeneLists(lists: GeneList[]): GeneList {
  if (lists.length === 1) return lists[0];
  const names: string[] = [];
  const symbols: string[] = [];
  const seen = new Set<string>();
  let anySymbols = false;
  for (const l of lists) {
    anySymbols ||= !!l.symbols;
    l.names.forEach((n, i) => {
      if (seen.has(n)) return;
      seen.add(n);
      names.push(n);
      symbols.push(l.symbols?.[i] ?? n);
    });
  }
  return {
    names,
    symbols: anySymbols ? symbols : null,
    symbol_column: lists.find((l) => l.symbol_column)?.symbol_column ?? null,
    duplicates: 0,
    matrices: lists[0].matrices,
  };
}
