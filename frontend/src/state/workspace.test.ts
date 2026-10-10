import { describe, expect, it } from 'vitest';

import type { DatasetMeta } from '../api/types';
import { effectiveParams, normaliseParams } from '../views/embedding';
import { registerView, viewType } from '../views/registry';
import { restoreViews } from './workspace';

describe('restoreViews', () => {
  it('drops unknown view types and repairs parameters from older builds', () => {
    const restored = restoreViews([
      { id: 'a', type: 'spatial', datasetId: 'd', table: null, title: 'x', params: {} }, // unknown type
      { id: 'b', type: 'embedding', datasetId: 'd', table: null, title: 'old', params: { embedding: 'X_umap' } },
      { id: 'c', type: 'embedding', datasetId: 'd', params: { embedding: 42 } }, // unusable params
      'garbage',
      null,
    ]);
    expect(restored.map((v) => v.id)).toEqual(['b']);
    expect(restored[0].params).toMatchObject({ embedding: 'X_umap', dims: [0, 1], hidden: [], colorBy: null });
  });

  it('accepts anything without throwing', () => {
    expect(restoreViews(undefined)).toEqual([]);
    expect(restoreViews({ views: 1 })).toEqual([]);
  });
});

describe('embedding parameters', () => {
  it('clamps and fills', () => {
    const p = normaliseParams({ embedding: 'X_pca', dims: [3, 3], pointSize: 99, opacity: 'x', hidden: [1, 'a'] });
    expect(p).toMatchObject({ dims: [0, 1], pointSize: 10, opacity: 0.85, hidden: ['a'], colormap: 'viridis' });
  });

  it('falls back with a notice when a saved column or embedding is gone', () => {
    const meta = {
      embeddings: [{ key: 'X_umap', dims: 2 }],
      obs: [{ name: 'leiden', kind: 'categorical' }],
    } as unknown as DatasetMeta;
    const saved = normaliseParams({ embedding: 'X_tsne', colorBy: 'gone', hidden: ['a'] })!;
    const { p, notice } = effectiveParams(saved, meta);
    expect(p.embedding).toBe('X_umap');
    expect(p.colorBy).toBeNull();
    expect(notice).toMatch(/X_tsne.*gone/);
    expect(effectiveParams(p, meta).notice).toBeUndefined();
  });
});

describe('view registry', () => {
  it('takes a new view type from one module', () => {
    registerView<{ n: number }>({
      type: 'test-dummy',
      label: 'Dummy',
      create: () => ({ n: 1 }),
      normalise: (raw) => (raw && typeof raw === 'object' ? { n: 1 } : null),
      useView: () => ({ layers: [], bounds: null, status: 'ready' }),
      Panel: () => null,
    });
    expect(viewType('test-dummy')?.label).toBe('Dummy');
    expect(restoreViews([{ id: 'z', type: 'test-dummy', datasetId: 'd', params: {} }])).toHaveLength(1);
  });
});
