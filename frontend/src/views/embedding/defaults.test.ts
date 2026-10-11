import { describe, expect, it } from 'vitest';

import type { ColumnMeta, DatasetMeta } from '../../api/types';
import { defaultColumn, normaliseParams } from '.';

const col = (name: string, n: number, colors = false): ColumnMeta => ({
  name,
  kind: 'categorical',
  dtype: 'category',
  n_categories: n,
  ordered: false,
  categories: Array.from({ length: n }, (_, i) => `c${i}`),
  colors: colors ? Array.from({ length: n }, () => '#000000') : null,
});

const meta = (obs: ColumnMeta[]) => ({ obs }) as DatasetMeta;

describe('defaultColumn', () => {
  it('prefers cell-type columns with colours, then any coloured column', () => {
    expect(defaultColumn(meta([col('embryo', 3, true), col('celltype_mapped', 20, true)]))).toBe('celltype_mapped');
    expect(defaultColumn(meta([col('embryo', 3, true), col('celltype', 20)]))).toBe('celltype');
    expect(defaultColumn(meta([col('batch', 3), col('embryo', 3, true)]))).toBe('embryo');
  });

  it('skips single-category and huge columns', () => {
    expect(defaultColumn(meta([col('one', 1), col('barcode', 5000)]))).toBeNull();
  });
});

describe('hand-picked colours in saved settings', () => {
  it('keeps well-formed colours only', () => {
    const p = normaliseParams({
      embedding: 'X_umap',
      customColours: { cell_type: { 'T cell': '#FF0000', B: 'red', C: 3 }, bad: 'x', empty: {} },
    });
    expect(p?.customColours).toEqual({ cell_type: { 'T cell': '#ff0000' } });
    expect(normaliseParams({ embedding: 'X_umap' })?.customColours).toEqual({});
  });
});
